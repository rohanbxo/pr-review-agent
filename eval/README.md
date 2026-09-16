# eval/ — does the review agent earn its cost?

SPEC phase 6. Everything here runs offline: each dataset case carries its own patches and file
contents, and the agent is driven through the **real** LangGraph graph and the **real**
read-only GitHub client (allowlist included) over `app.agent.fixtures.mock_transport_for_case`.
No GitHub traffic, no rate limits, reproducible after upstream force-pushes.

```
eval/
  mutations.py       AST-located, text-applied, pyflakes-verified bug injection
  build_dataset.py   git history of public repos -> data/{injected,reverted,clean,v1}.jsonl
  metrics.py         pure scoring functions (FP rate, detection, localisation, cost)
  run_eval.py        runner: agent / --baseline / --llm fake
  data/              the dataset + manifest.json (pinned SHAs, counts, hashes) + README
  reports/           baseline.json, v1.json (real model), smoke-fake.json (plumbing only)
  tests/             pytest eval/tests
```

All commands run from the repo root with the backend venv. `run_eval` puts `backend/` on
`sys.path` itself (it imports the agent as `app.*`).

```bash
PY=backend/.venv/Scripts/python.exe          # Windows; backend/.venv/bin/python elsewhere
uv pip install --python $PY -r eval/requirements.txt   # pyflakes

$PY -m pytest eval/tests -q
```

## Build the dataset

Only `eval/data/manifest.json` (pinned repo SHAs + output hashes) and `eval/data/sample.jsonl`
(10 cases, used by CI) are committed. Regenerate the full splits:

```bash
make eval-data                                 # = $PY -m eval.build_dataset --verify
$PY -m eval.build_dataset --refresh            # fetch upstream and re-pin (changes the dataset hash)
```

`--verify` fails unless every rebuilt file hashes to the value pinned in the manifest, so the
dataset cannot quietly drift. Cases store head `content` + `patch` only. Base versions are derived
from them (`app.agent.fixtures.reverse_apply_patch`) rather than stored.

Repos are bare-cloned over HTTPS into `eval/.cache/repos` (gitignored; `git clone` is not
subject to the GitHub API rate limit). See `eval/data/README.md` for provenance, filters and
caveats.

## Run

```bash
# 1. Baseline first. No LLM: one `medium` finding per changed file, spanning the whole file.
$PY -m eval.run_eval --baseline --dataset eval/data/v1.jsonl --report eval/reports/baseline.json

# 2. The agent. The model comes from settings via app/agent/llm.py, the same factory the app uses.
#    Default provider openai_compatible = OpenRouter; needs LLM_API_KEY and AGENT_MODEL.
#    It fails fast and writes nothing if either is missing.
LLM_API_KEY=... AGENT_MODEL=anthropic/<model> $PY -m eval.run_eval --dataset eval/data/v1.jsonl --report eval/reports/v1.json --concurrency 4

# Direct Anthropic, for a provider comparison (--provider/--model override settings for one run):
ANTHROPIC_API_KEY=... $PY -m eval.run_eval --provider anthropic --model claude-sonnet-5 --report eval/reports/v1-anthropic.json

# 3. Dev subset for "did this change help?" iterations (60 cases; see "Dev subset" below).
$PY -m eval.dev_split --verify          # = make eval-dev
$PY -m eval.run_eval --dataset eval/data/dev.jsonl --report eval/reports/dev.json

# Offline plumbing smoke test: real graph + client + mock transport, stub model returning an
# empty review. Refuses to write v1.json/baseline.json; the report is stamped "llm": "fake".
$PY -m eval.run_eval --llm fake --limit 3 --report eval/reports/smoke-fake.json
```

Other flags: `--splits injected reverted`, `--limit N` (round-robin across splits),
`--case-timeout S`, `--max-tool-rounds N`, and the scoring thresholds `--tolerance N` (default 5),
`--narrow-max-lines N` (default 30), `--narrow-span-fraction F` (default 0.25), `--narrow-min-lines N`
(default 3).

**Runs checkpoint per case.** Every completed case is appended to `<report>.cases.jsonl` and
flushed to disk immediately, so an interrupted run (OOM kill, Ctrl-C, dropped connection) keeps
everything it already paid for.
- `--resume` skips cases already in the checkpoint and finishes the rest. It refuses unless the
  checkpoint's dataset hash, scoring, provider, model, temperature and trimming match this run: a
  report stitched from two configs would be worse than no report.
- `--from-checkpoint` writes a report from whatever is in the checkpoint without running anything.
  A short report is marked `meta.partial_run` and carries a `WARNING`.
- An existing checkpoint is never silently appended to: pass `--resume`, `--from-checkpoint`, or
  delete it.

`--rescore REPORT --report OUT` re-applies the current scoring to an existing report's stored
findings. It calls no model and is exact, since scoring is a pure function of the findings.
Provider, model, dataset hash and cases carry over unchanged. `meta.rescored_from` records the
original scoring. Use it after a scoring change so old runs can be compared under the new rule
without paying for them again.

## Dev subset

`eval/data/dev.jsonl` is 15 injected, 10 reverted and 35 clean cases.
- **Stratified:** injected rotates across the four bug kinds, 4/4/4/3.
- **Chosen by `sha256(seed:case id)`:** membership depends only on a case's own id. Rebuilding or
  reordering the dataset doesn't reshuffle it, and a new case enters only if its own hash ranks in.
- **Pinned:** its hash is in `manifest.json` (`sha256.dev`), checked by `--verify` and `make eval-data`.
- **Its own dataset:** it has its own `dataset_sha256`, so the comparison rule below never lets a
  dev run be compared with a full run.

**35 clean cases give a false-positive rate with roughly ±8% slop.** One standard error is
√(p(1−p)/35): about 5 points at p = 10%, 7 at 20%, 8.5 at 50%. A 95% interval is about twice
that. That is fine for "did this change help". It is not fine for a headline number: quote the
full `v1` run for that.

## Reading the report

**First line: `parse_errors`.** It is a top-level block right after `meta`, and it is the first
line of the stdout summary.
- `cases_with_parse_failure`: cases where the synthesize step's structured output failed to parse
  at least once.
- `repaired_cases`: those that parsed on the retry.
- `unrecovered_cases`: those that never did. They also count as errored cases.
- `failed_attempts`: the total number of failed parses.

If this is not ~0, synthesis is failing and every number below is noise. Fix that first.

Then the four groups, always **in this order** — never quote one without the others:

1. **`false_positive_rate`** — share of `clean` cases with any finding at `medium`+. This decides
   adoption: a reviewer that cries wolf gets muted.
2. **`detection`** — `detection_rate` overall, `by_split`, `by_bug_kind`, as a share of **all**
   bug cases. A finding detects the bug if it is `medium`+, names the expected file, and its line
   range overlaps the expected range within `tolerance_lines` (5).
3. **`localisation`** — `localisation_rate_of_detected` overall, `by_split`, `by_bug_kind`, as a
   share of **detected** bugs (the denominator is `detected`; `localised_share_of_all_bugs` is
   also given). A detecting finding localises the bug if its range is also narrow:
   `width <= max(bug_width, narrow_min_lines, min(narrow_max_lines, narrow_span_fraction × changed_line_span))`
   (scoring v3), where `changed_line_span` is the head-line span of the lines the expected file's
   patch changes. Thirty lines on a 40-line change is a shrug; on a 3000-line change it is a real
   pin. The two floors:
   - **The bug's own width:** a finding that exactly covers the bug always counts.
   - **3 lines:** v2 lacked this floor. There, a 2-line pin on the bug in a 6-line change had a
     limit of 1.5, so the correct answer scored as a shrug.
4. **`cost`** — mean and p95 seconds per review, mean tokens (total / input / output).

Plus `errors` (an errored case counts as a *miss* on bug splits and as a *false positive* on
`clean` — a review that did not complete is not a clean pass) and `cases` (per-case findings,
usage, GitHub call count, **blocked calls**, parse failures, error).

`meta` records:
- `llm` (`configured` | `fake` | `baseline`), **`provider`** (`openai_compatible` | `anthropic` |
  `fake` | `none`), **`model`** and `base_url`. The key is never written.
- `dataset_sha256`, split counts, git SHA (if any) and timestamp.
- **`meta.scoring`**: every threshold and rule that produced the numbers, with a `scoring_version`.

### Comparison rule

**Two agent reports are comparable only if all five match: `dataset_sha256`, `meta.scoring`,
`meta.provider`, `meta.model` and `meta.temperature`.** A Haiku run and a Sonnet run, an OpenRouter
run and a direct Anthropic run, a dev run and a full run, or a temperature-1 run and a
temperature-0 run are different experiments. Temperature defaults to 0: at non-zero temperature,
case-level differences between two runs are sampling noise. Even at 0, Anthropic models are not
bit-deterministic, so read a one- or two-case swing as noise. `meta.prompt_cache` is recorded too.
It changes cost, never what the model sees, so it is not part of the rule. Put them side by side as
such, never as a before/after of one change. The baseline is the fixed reference rather than a
run. Hold an agent report against it when `dataset_sha256` and `meta.scoring` match; its provider
is `none`.

## Results so far

**Reference run (PARTIAL): `anthropic/claude-haiku-4.5` via OpenRouter, pinned to the Anthropic
host, temperature 0, rolling prompt cache, no trimming, scoring v3.**
`reports/v1-partial.json`, with the matching baseline over the same cases in
`reports/v1-partial-baseline.json`.

**76 of 215 cases: all 40 injected, all 25 reverted, 11 of 150 clean.** The machine ran out of
memory repeatedly and the run was killed; per-case checkpointing kept everything paid for
(`reports/v1.cases.jsonl`, resume with `--resume`). Every bug-split number below is complete. The
false-positive rate is over 11 clean cases and is NOT quotable.

- **Parse errors: 0/76.** No synthesis failure, repaired or otherwise.
- **Detection (share of all bugs):** injected 85.0% (34/40), reverted 76.0% (19/25). By kind:
  transposed_args 100%, flipped_comparison 90%, off_by_one_range_len 80%, removed_none_guard 70%.
- **Localisation (share of detected):** 92.5% overall; reverted 100%, injected 88.2%.
- **False positives on clean: 7 of 11 (63.6%)** — far above the 17.1% seen on the 60-case dev run,
  which was at default temperature. Whether that is the temperature change, these particular clean
  cases, or noise at n=11 is unresolved; the remaining 139 cases decide it.
- **Cost:** mean 129k tokens/case (of which 53k cache reads), $0.097/case, $7.38 for 76 cases.
  Mean 23.6s per case, p95 45.4s.

Against the baseline over the same 76 cases: the baseline detects 100% and localises 0%, with a
100% false-positive rate. The agent gives up some detection (85% / 76%) to localise 92.5% of what
it finds, with far fewer false positives.

**3 of 6 flags on clean PRs in the dev run were genuine upstream bugs**, two with matching upstream
fixes, so a measured false-positive rate overstates the agent's mistakes. Every verdict is in
[`reports/dev-case-review.md`](reports/dev-case-review.md).

## Prompt caching on OpenRouter → Claude: measured, not assumed

All numbers are from small real probes on OpenRouter → `anthropic/claude-haiku-4.5` (served by
Amazon Bedrock), 2026-09-16. None of this is enforced by a type checker, and several of these
behaviours are undocumented. Re-probe before relying on them for another model or provider.

Prices: cache reads cost 0.1× input and cache writes 1.25× (5-minute TTL). After a cached
request, OpenRouter keeps routing the model to the same provider, so the cache stays warm.

1. **A top-level `cache_control` field rolls with the conversation.** On its own it puts the
   breakpoint on the last cacheable block. A second call extending the first read all 23,159
   earlier tokens from cache and wrote only the 1,866 new ones.
2. **Top-level plus any explicit block breakpoint silently disables the top-level one.** With an
   explicit marker on the brief as well, only the brief was cached (14,093 tokens), and nothing
   after it was ever written or read. No error, no warning: the requests simply cost more.
3. **A different tool list reads nothing.** Synthesize binds only `ReviewResult`, and a
   synthesize-shaped request on an identical conversation read 0 cached tokens. So synthesize
   carries no breakpoints: a write there is never read.
4. **Rewriting history breaks the rolling breakpoint.** With old tool results replaced by stubs
   (keep 2), a top-level breakpoint never hit again after trimming started. Six simulated rounds
   cost $0.249, above the $0.224 uncached price, because every call re-wrote everything at 1.25×.
5. **Explicit breakpoints on stable positions survive trimming.** Stubbing is monotonic, so the
   prefix ending at the newest stub is identical in the next request. The provider checks earlier
   block boundaries for an existing entry, so the next request still hits.
   - **Brief + newest stub:** $0.164.
   - **Brief + newest stub + last message:** $0.184. The kept full results follow a position that
     changes every round, so caching them only pays the write premium.
6. **Nothing below 4,096 tokens is cached.** That is Haiku 4.5's minimum prefix. On small PRs the
   brief plus stubs is 2–3k tokens, so trimmed runs cached nothing at all
   (`click-9da1791476`: 8 calls, 0 read, 0 written).

What this means for the agent: prompt caching and context trimming pull against each other.
Trimming rewrites the part of the conversation the cache would be reading. See the context
hygiene section below for the measured trade-off.

## Context hygiene (trimming old tool results)

Trimming is **off by default**. With `LLM_KEEP_TOOL_RESULTS=N`, the model sees the N most recent
tool results in full, and older ones become a stub naming the tool, its arguments and the result
size. The brief, with every patch, is never trimmed. Graph state and `agent_steps` keep the full
history. Trimming changes what the model sees, so it is recorded as `meta.keep_tool_results`
(`"off"` or N) and is part of the comparison rule.

**Finding, direction only (6 cases):** with keep 2 on Haiku at temperature 0, bugs detected went
from 2/4 to 4/4, all localised, and one false positive disappeared while another appeared. Stale
40k-token file reads appear to distract the model after it has extracted what it needed. Cost went
up ($0.617 to $0.847), from re-reads and lost cache hits: trimming and rolling caching are
structurally incompatible. Full write-up, and what would confirm or refute it:
[`reports/finding-context-trimming.md`](reports/finding-context-trimming.md).

## How the pieces keep the eval honest

- **Mutations survive a linter.** Each is located with `ast`, applied as a byte-exact text
  splice, then verified: the result parses, `pyflakes` reports no new message, and the AST
  actually changed. A mutation that breaks the parser measures nothing.
- **Bugs sit inside the reviewed change.** Only `+` lines of the PR's diff are eligible; the
  file's patch and head content are regenerated after mutation and `expected.lines` are
  head-file line numbers.
- **Reverted cases do not leak the answer, and are realistic in size.** A bare inverse of a
  small fix is a few-line diff: a reviewer is handed exactly the bug and nothing else, which is
  not what real PRs look like. Each case therefore carries the fixed file forward through real
  later development (10–300 changed lines) and undoes the fix inside it. (This was first
  justified by a baseline score of 25/25 → 4/25. That drop was mostly an artefact of the old
  first-hunk baseline, not of difficulty. The padding stays on realism grounds.)
  Only the source file is included (not the fix's regression test or changelog), the title is a
  neutral `Update <file>`, and the fix commit is recorded under `source`, which the mock
  transport never serves.
