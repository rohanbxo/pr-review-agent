# eval/data — dataset v1

Built by `python -m eval.build_dataset` (see `eval/README.md`). Format: one JSON object per line,
CONTRACTS.md § Dataset case format. `manifest.json` records the seed, the pinned commit SHA of
every source repository, per-split counts and the sha256 of every file — a report's
`meta.dataset_sha256` must match `manifest.json` → `sha256.v1` for numbers to be comparable.

| File | Cases | What |
|---|---|---|
| `injected.jsonl` | 40 | real merged PR + one synthetic, linter-surviving bug; 10 per kind |
| `reverted.jsonl` | 25 | real bug-fix commit, inverted (the "PR" re-introduces the bug) |
| `clean.jsonl` | 150 | real merged PRs, untouched |
| `v1.jsonl` | 215 | concatenation, in that order |
| `sample.jsonl` | 10 | first injected case of each kind, 2 reverted, 4 clean — **committed**, for CI |

**Only `manifest.json` and `sample.jsonl` are committed.** The four full files above are
regenerated from git history with `make eval-data` (or
`backend/.venv/Scripts/python.exe -m eval.build_dataset --verify`). The build is deterministic
and `--verify` fails unless every output file hashes to the value pinned in `manifest.json`.
The first run clones the source repos into `eval/.cache/`; after that no network is needed.

## Provenance

Source repositories (public, cloned over HTTPS; SHAs pinned in `manifest.json`):
psf/requests, pallets/flask, pallets/click, encode/httpx, python-attrs/attrs, pypa/packaging,
tiangolo/typer, marshmallow-code/marshmallow, pallets/werkzeug, pallets/jinja,
more-itertools/more-itertools, dateutil/dateutil, Textualize/rich, pyparsing/pyparsing,
networkx/networkx. The last five were added mainly because `range(len(...))` is rare in modern
code and the `off_by_one_range_len` kind needed sites.

No GitHub API was used. A **merged PR** is a first-parent commit on the default branch whose
message is either `Merge pull request #N from …` (title = first body line, body = the rest) or a
squash commit `Title (#N)` (body = commit body). The diff is first parent → commit
(`git diff --no-renames -U3`), per-file `patch` is the hunk text GitHub would show, `content` is
the full file at head. Base versions are **not stored**. A patch contains every changed line, so
base is fully determined by head + patch, and the mock transport derives it with
`app.agent.fixtures.reverse_apply_patch` when the agent calls `read_file(ref=base)`. Before the
stored copies were dropped, that derivation was checked to reproduce all 487 stored base files
byte for byte. `source` records the upstream repo URL, commit, parents and date; it
is never served to the agent.

Reviewable-PR filter: 1–8 files, ≤ 600 changed lines, only `.py` plus doc files
(`.rst/.md/.txt`, e.g. changelogs), at least one non-test, non-docs Python source file, no binary
or vendored/generated paths (`vendor/`, `_vendor/`, `extern/`, `requests/packages/`, `*_pb2.py`),
no bot authors, no reverts / version bumps / dependency updates, every file ≤ 200 KB
(the agent client's fetch cap — no case hands the agent a truncated file).

### injected

A PR from the reviewable pool, one mutation from `eval/mutations.py` applied to a **line the PR
added** in a non-test source file. The mutation is verified to parse, add no pyflakes message
and change the AST; the file's `patch`, `content`, `additions`/`deletions` are regenerated
(difflib, 3 lines of context) and `expected = {bug_kind, file, lines}` uses head-file line
numbers. For `removed_none_guard` of a whole statement, `lines` is the two-line window where the
guard used to be. Selection: seeded shuffle per repo, round-robin across repos, rare kinds
searched first; each PR is used at most once and never also appears in `clean`.
`source.mutation` describes the edit.

### reverted

Candidate fix commits: single-parent, subject matching `fix|fixes|fixed|bug|bugfix|regression`
(excluding typo/docs/lint/typing/CI/deprecation/formatting/revert subjects), touching exactly one
non-test source file with 1–40 changed lines there, with a real (non-docstring) AST change;
commits that also touch tests are preferred.

A bare inverse of such a fix is a diff of a handful of lines: the reviewer is handed the bug and
nothing else, which real PRs never do. So each case is **padded with real later development**: take the
file at the first later first-parent commit L where F..L changed 10–300 lines, undo the fix
inside L (each fix block with 2 lines of context must still occur exactly once), and require the
result to parse and add no pyflakes message. The case is then `base = fix commit F`,
`head = L with the fix undone`: genuine surrounding changes plus a regression. Only the source
file is included — the fix's regression test and changelog are not (they would give it away) —
and the title is a neutral `Update <file>` with an empty body. `pr_number` is synthetic
(100000+). `expected.lines` = the head lines the undo touched. `source.fix_commit`,
`source.fix_subject` and `source.padding_commit` record where it came from.

### clean

The remaining reviewable PRs, seeded shuffle per repo, round-robin across repos (≈10 per repo).

## Caveats

- **`clean` means "merged upstream", not "verified bug-free".** Some of these PRs shipped real
  bugs that were fixed later. A non-zero false-positive rate floor is expected; read flagged
  clean cases before concluding the agent is wrong.
- **`injected` bugs are uniform.** Use the split as a regression harness for prompt changes;
  quote `reverted` as the quality number.
- `reverted` "PRs" never existed as PRs; the surrounding changes are real but were not written
  together with the regression.
- Kind balance: `off_by_one_range_len` sites are scarce (the builder redistributes a short kind's
  slots to the others if it ever runs out; v1 reached 10/10/10/10).
- Size: ~34 MB for v1, almost all of it full `content` + `base_content` (≈16 MB each; patches
  <1 MB). Both are kept on purpose: `read_file` at head and base must work offline, and the
  agent must never see a file the real API would have served differently.
- Rebuilding with `--refresh` re-pins to upstream HEAD and yields a different dataset (and hash).
