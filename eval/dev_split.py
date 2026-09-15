"""The dev split: a small, stratified, stable subset of v1 for "did this change help?" runs.

    python -m eval.dev_split            # eval/data/v1.jsonl -> eval/data/dev.jsonl
    python -m eval.dev_split --verify   # ...and fail unless it matches manifest.json sha256.dev

15 injected (round-robin across the four bug kinds: 4/4/4/3), 10 reverted, 35 clean.

Selection is by ``sha256(DEV_SEED + ":" + case id)``, not by position or RNG state, so a case's
membership depends only on its own id: rebuilding the dataset, reordering it, or adding cases to
other splits does not reshuffle dev. dev.jsonl is its own file with its own dataset_sha256, so
the report comparison rule refuses to compare a dev run against a full run.

A 35-case clean split measures the false-positive rate coarsely (see eval/README.md): good for
"did this change help", not for a headline number.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
DEV_SEED = "pr-review-eval-dev-v1"
DEV_TARGETS = {"injected": 15, "reverted": 10, "clean": 35}
INJECTED_KINDS = ("flipped_comparison", "removed_none_guard", "off_by_one_range_len", "transposed_args")


def _key(case: dict) -> str:
    return hashlib.sha256(f"{DEV_SEED}:{case['id']}".encode("utf-8")).hexdigest()


def select_dev(cases: list[dict], targets: dict[str, int] = DEV_TARGETS) -> list[dict]:
    """Deterministic, id-hash-ordered stratified subset. Output order: injected, reverted, clean."""
    out: list[dict] = []
    for split, n in targets.items():
        pool = sorted((c for c in cases if c["split"] == split), key=_key)
        if len(pool) < n:
            raise ValueError(f"dev split wants {n} {split} cases, dataset has {len(pool)}")
        if split == "injected":
            by_kind = {k: [c for c in pool if (c.get("expected") or {}).get("bug_kind") == k] for k in INJECTED_KINDS}
            picked: list[dict] = []
            i = 0
            while len(picked) < n:
                progressed = False
                for k in INJECTED_KINDS:
                    if i < len(by_kind[k]) and len(picked) < n:
                        picked.append(by_kind[k][i])
                        progressed = True
                if not progressed:
                    raise ValueError("not enough injected cases across kinds for the dev split")
                i += 1
            out.extend(sorted(picked, key=_key))
        else:
            out.extend(pool[:n])
    return out


def to_jsonl(cases: list[dict]) -> str:
    return "".join(json.dumps(c, ensure_ascii=False, sort_keys=False) + "\n" for c in cases)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=DATA / "v1.jsonl")
    ap.add_argument("--out", type=Path, default=DATA / "dev.jsonl")
    ap.add_argument("--verify", action="store_true", help="exit 1 unless the output matches manifest.json sha256.dev")
    args = ap.parse_args(argv)
    if not args.source.exists():
        print(f"error: {args.source} not found; build the dataset first (make eval-data)", file=sys.stderr)
        return 2
    cases = [json.loads(line) for line in args.source.read_text(encoding="utf-8").splitlines() if line.strip()]
    text = to_jsonl(select_dev(cases))
    args.out.write_text(text, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    print(f"wrote {args.out} ({sum(DEV_TARGETS.values())} cases) sha256={digest}", file=sys.stderr)
    if args.verify:
        pinned = json.loads((DATA / "manifest.json").read_text(encoding="utf-8")).get("sha256", {}).get("dev")
        if pinned != digest:
            print(f"VERIFY FAILED: manifest sha256.dev={pinned} rebuilt={digest}", file=sys.stderr)
            return 1
        print("verify: dev.jsonl matches manifest.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
