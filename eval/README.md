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

**Dev split, anthropic/claude-haiku-4.5 via OpenRouter** (`reports/dev.json`, rescored under
scoring v3 in `reports/dev-nocache-v3.json`). This run predates temperature 0.
- **False positives on clean:** 17.1% reported (6/35).
- **Detection:** 86.7% on injected, 60.0% on reverted.
- **Localisation:** 19/19 of detected bugs.
- **Parse errors:** 0.

**The reported false-positive rate overstates the agent's mistakes. 3 of the 6 flags were
genuine bugs:**
- `rich-2635` and `dateutil-681` have matching upstream fixes.
- `werkzeug-324` is a real, older defect that upstream later fixed.

**The corrected false-positive rate is 8.6% (3/35), or 11.4% (4/35) if `werkzeug-324` isn't
credited.** Of the 6 misses, 2 reported nothing and 4 flagged something else that was wrong. That
second kind is the worse failure. See [`reports/dev-case-review.md`](reports/dev-case-review.md)
for every verdict, the claim checked, and the upstream fix commits. The metric itself is not
adjusted: "clean" means merged upstream, and read the flagged clean cases before tuning the FP
rate towards zero.

## Rules for interpreting numbers

- **`injected` is a regression harness; `reverted` is the quality number.** Injected bugs are
  far more uniform than real ones — a model can learn the shape of "comparison flipped" in a way
  it cannot learn a real bug. Use `injected` to catch prompt changes that break something;
  quote `reverted` (real historical bugs, re-introduced) when asked how good the agent is.
- **Detection rate alone is a vanity metric.** An agent that reports eight findings per PR
  catches most bugs and is unusable. Detection is only meaningful next to the FP rate.
- **If the agent does not clearly beat the baseline, the LLM is not earning its cost.** The
  baseline flags every changed file, whole file, as `medium`. By construction it has **100%
  detection, 100% FP on `clean`, and 0% localisation** (`eval/reports/baseline.json`). Detection
  alone can never beat it. The agent earns its cost only through a far lower FP rate *and* a
  localisation rate well above 0% — otherwise `git diff --stat` does the same job for free.
- `clean` means "merged upstream", not "verified bug-free". Some clean PRs contain real bugs
  later fixed; a small FP rate floor is expected. Read the flagged clean cases before tuning to
  zero.

## Why tolerance = 5 lines

Findings are compared on file + line-range overlap, widened by 5 lines each side.
Tolerance **0** measures line-number formatting (off-by-one in how the model counts, a range
that starts at the `if` rather than the comparison) instead of whether it found the bug.
Tolerance **50** would let a nearby shrug count. 5 lines is roughly "the same statement or its
immediate neighbours". Tolerance only governs *detection*, the overlap test. A wide finding
overlaps anything, which is why whole-file shrugs are handled separately by the narrow-range test
in *localisation*, not by tightening tolerance. All three thresholds are flags, so you can check a
result is not an artefact of the choice, but reports are only comparable at the same values.

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

The model sees the `LLM_KEEP_TOOL_RESULTS` most recent tool results in full (default 2). Older
ones become a stub naming the tool, its arguments and the result size. The brief, with every
patch, is never trimmed. Graph state and `agent_steps` keep the full history. This changes what
the model sees, so it is recorded as `meta.keep_tool_results`.

**First measurement:** the same 6 dev cases at temperature 0, untrimmed-with-cache vs trimmed.
- **Cost:** $0.617 untrimmed, $0.847 trimmed.
- **Mean input:** 133k tokens untrimmed, 146k trimmed.
- **GitHub calls:** 47 untrimmed, 58 trimmed. The model re-reads files after they are stubbed:
  `flask-4580` went from 9 calls to 17.
- **Cache reads:** 53k per case untrimmed, 16k trimmed. See finding 6 above.
- **Findings:** bug cases detected went from 2/4 to 4/4, all localised. On the 2 clean cases,
  one false positive disappeared and another appeared.

At 6 cases this is a direction, not a result. The quality signal is promising, and the cost is
worse.

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
