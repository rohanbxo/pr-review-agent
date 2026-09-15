# Finding: stale tool results appear to hurt review quality

**Status: a direction, not a result.** It rests on 6 dev cases, of which 4 contain a bug, at
temperature 0. It needs a full run with trimming on against the same run with trimming off before
anyone quotes it.

## What was compared

The model was `anthropic/claude-haiku-4.5` via OpenRouter, at temperature 0, on the same 6 dev
cases, 2 per split. The only intended difference was what the model saw on each call:

- **Untrimmed** (`sample6-dev-cache-t0.json`): every tool result stays in the conversation for
  the rest of the review.
- **Trimmed, keep 2** (`sample6-dev-trim-keep2-t0.json`): the 2 most recent tool results are kept
  in full. Older ones become a one-line stub naming the tool, its arguments and the result size,
  and saying the model can call the tool again. The brief with every changed file's patch is never
  trimmed.

Prompt caching differed between the two runs, but caching changes cost, never what the model sees.

## What happened

**Bugs detected and localised: 2 of 4 untrimmed, 4 of 4 trimmed.**
- `flask-4580` (removed `None` guard) went from missed to detected and pinned to the right lines.
- `click-9da1791476` (a real reverted fix) went from missed to detected and pinned.
- `more-itertools-108` and `jinja-e3290ea9f3` were found either way.

**False positives on the 2 clean cases: 1 each way.**
- `marshmallow-321` lost a high-severity false positive.
- `flask-2898` gained a medium one.

Parse errors were 0 in both runs.

**Cost went up.** Trimmed runs made more tool calls (58 vs 47) because the model re-read stubbed
files (`flask-4580`: 9 calls to 17). They also lost most prompt-cache hits: see "Prompt caching on
OpenRouter → Claude" in `eval/README.md`. The 6 cases cost $0.847 trimmed against $0.617 untrimmed.

## Interpretation

A read_file result can be 40k tokens. In the untrimmed runs, that file sits in context for every
remaining analyze call and for synthesize, long after the model has pulled out what it needed. In
the trimmed runs the model worked from the patches, its own earlier notes, and the two most recent
reads, and it re-read a file when it needed it again. It found more of the planted bugs and pinned
them precisely.

The plausible reading is that stale, very large file reads distract the model: attention spent on
code already reviewed is attention not spent on the change. This matches what is generally known
about long-context degradation. It is still only a reading of 4 bug cases, where a two-case swing
is the whole effect.

## What would confirm or refute it

- **A full or dev-sized run with trimming on vs off,** same model, temperature 0 and scoring. The
  comparison rule already treats `meta.keep_tool_results` as part of what must match, so the two
  reports are clearly labelled as different experiments.
- **Other keep values (3, 4)** to see whether the effect is about removing stale context at all, or
  specifically about keeping it short.
- **Re-read counts per case,** to separate "re-read what it needed" from "wandered".

## Why cost and quality point different ways here

Trimming and rolling prompt caching are structurally incompatible, not just badly tuned.
- **Stubbing rewrites history every round.** The kept full results sit at positions that change
  each call, so they can never be read from cache.
- **The stable prefix is small.** The brief plus stubs is often under Haiku 4.5's 4,096-token
  minimum cacheable prefix, so on small PRs nothing is cached at all.

If trimming is confirmed as a quality win, cost has to be solved differently. Batch compaction is
one option: stub only when context passes a threshold, so the prefix stays stable between
compactions.
