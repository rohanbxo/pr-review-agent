"""Per-case checkpointing for eval runs.

A run spends real money over an hour or more. Before this, nothing was persisted until the last
line, so any interruption -- OOM kill, dropped connection, Ctrl-C -- threw away every completed
case and everything it cost. Each case result is now appended to a JSONL and flushed to disk as it
completes, so a run can resume and pay only for what it had not reached.

Line 1 is a header holding the config the results were produced under. ``--resume`` refuses unless
the current run matches it exactly: a report stitched from two different configs (model,
temperature, scoring, dataset, trimming) would be worse than no report at all.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

HEADER_KEY = "__checkpoint__"


def fingerprint(*, mode: str, dataset_sha: str, provider: str, model: str, temperature: float | None,
                keep_tool_results: Any, scoring: dict) -> dict:
    """Everything that must match for two case results to belong in one report."""
    return {"mode": mode, "dataset_sha256": dataset_sha, "provider": provider, "model": model,
            "temperature": temperature, "keep_tool_results": keep_tool_results, "scoring": scoring}


class CheckpointMismatch(RuntimeError):
    """The checkpoint on disk was produced under a different configuration."""


class Checkpoint:
    """Append-only case log. Immediately flushed and fsynced: a kill -9 keeps what completed."""

    def __init__(self, path: Path, config: dict):
        self.path = path
        self.config = config
        self._fh = None

    # -- reading -------------------------------------------------------------------------
    def read(self) -> tuple[dict | None, list[dict]]:
        if not self.path.exists():
            return None, []
        header: dict | None = None
        results: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn final line from a hard kill: ignore it
            if HEADER_KEY in obj:
                header = obj[HEADER_KEY]
            else:
                results.append(obj)
        return header, results

    def load_for_resume(self) -> list[dict]:
        header, results = self.read()
        if header is None:
            raise CheckpointMismatch(f"{self.path} has no checkpoint header; delete it or pick another --report")
        if header != self.config:
            diff = {k: (header.get(k), self.config.get(k)) for k in set(header) | set(self.config)
                    if header.get(k) != self.config.get(k)}
            raise CheckpointMismatch(
                f"{self.path} was written under a different configuration; refusing to mix runs.\n"
                f"  differences (checkpoint -> now): {json.dumps(diff, default=str)}")
        return results

    # -- writing -------------------------------------------------------------------------
    def open(self, *, resuming: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not (resuming and self.path.exists())
        self._fh = self.path.open("a" if not new else "w", encoding="utf-8", newline="\n")
        if new:
            self._write({HEADER_KEY: self.config})

    def _write(self, obj: dict) -> None:
        assert self._fh is not None
        self._fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())  # survive a kill, not just a clean exit

    def append(self, result: dict) -> None:
        self._write(result)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "Checkpoint":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
