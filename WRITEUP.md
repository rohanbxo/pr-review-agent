# Measuring whether an LLM code reviewer is worth trusting

A small system, and the harness built to find out whether it works. It points a language model at a
pull request and returns a structured review: findings anchored to a file and a line range, or
an empty review when nothing is wrong. The measurement is the interesting part, and most of what it
found is unflattering.

## The problem: a diff is not enough context

An LLM handed a unified diff will always produce review comments. But a diff shows changed lines and
three lines of context, while most real defects depend on what it does not show: the invariant the
caller relies on, whether a value can be `None`, whether a guard exists twenty lines up.

Given too little context, a model does not say it cannot tell. It fills the gap with a plausible
story stated as confidently as a real finding, and each wrong finding costs a reviewer the time to
chase it. A reviewer burned three times stops reading.

So this agent reads beyond the diff: PR metadata and every patch up front, plus read-only tools for
whole files, commits and review comments. Read-only is enforced at the transport, not the prompt — a
client refusing any verb but GET/HEAD and any path off an allowlist, before a socket opens. None of
that establishes the reviews are good. That needs measurement.

## How I measured it

The dataset is 215 pull requests rebuilt from the git history of 15 public Python projects, with no
GitHub API involved, so it is reproducible and survives upstream force-pushes. Three splits:

**`injected` (40 cases).** A real merged PR with one synthetic bug on a line the PR itself changed: a
flipped comparison, a removed `is None` guard, an off-by-one, transposed arguments. Each mutation is
AST-located and checked to still parse and add no linter message — a bug a linter catches measures
nothing.

**`reverted` (25 cases).** A real bug-fix commit, inverted, so the PR re-introduces a bug that
genuinely occurred — buried inside real later development of the same file, with its regression test
excluded.

**`clean` (150 cases).** Real merged PRs, untouched.

Scoring separates two things that are easy to conflate:

- **Detection**: a `medium`+ finding naming the right file, overlapping the bug within five lines.
- **Localisation**: a detecting finding whose range is also *narrow* —
  `max(bug width, 3, min(30, 25% of the changed-line span))`. Thirty lines on a forty-line change is
  a shrug; on a three-thousand-line change it is a real pin.

The split exists because of a baseline that needs no model: flag every changed file, whole file, at
`medium`. It **detects 100% of bugs** — every planted bug is in a changed file — while localising 0%
and flagging every clean PR. It is useless, and it wins on detection rate. Quoting detection alone
ranks `git diff --name-only` above a careful reviewer, so the harness always reports all four metric
groups together, false-positive rate first.

## Results

The reference run is `anthropic/claude-haiku-4.5` via OpenRouter, pinned to one upstream host,
temperature 0, all 215 cases, about $21 and an hour.

| | agent | baseline |
|---|---|---|
| Synthesis parse errors | 0 / 215 | — |
| Flagged a clean PR (149) | 28.9% | 100% |
| Detection, injected (40) | 85.0% | 100% |
| Detection, reverted (25) | 76.0% | 100% |
| Localisation of detected, injected | 88.2% | 0% |
| Localisation of detected, reverted | 100% | 0% |

Then the number that matters. A false-positive rate counts *flagged* clean PRs, not *wrong* flags —
"clean" means merged upstream, not verified bug-free. So I sampled 15 of the 43 flags with a recorded
seed and checked each claim against the code and upstream history.

**11 of 15 flags were wrong: 73%, 95% CI 48–89%.** Ten were defensible but wrong — a real mechanism
that is intended, pre-existing, or unchanged upstream years later — and one was nonsense: it called a
release-notes date a future date when it was in the past. Roughly one clean PR in five gets a wrong
flag. The honest headline: this agent stays quiet on 71% of clean PRs, and when it does speak up it
is wrong about three times out of four.

## The four real bugs

The other quarter were genuine defects in already-merged code, three with upstream fixes:

- **attrs**: a PR stopped `assoc` emitting a deprecation warning but left one test asserting it.
  Upstream later removed exactly that, in a commit that does nothing else.
- **dateutil**: removing an `else: raise ValueError` left a variable unbound for unexpected input.
  Upstream's fix is titled "switches the error from UnboundLocalError to TypeError".
- **werkzeug**: `www_authenticate=None` was wrapped in a one-element tuple, which is truthy, so the
  response carried a literal `WWW-Authenticate: None` header. Current code no longer wraps it.
- **httpx**: in a PR titled "Keep clients in sync", one method gained an optional type and its six
  siblings did not. Still inconsistent today.

Not spectacular, but the kind a reviewer misses at 5pm, found in merged code.

## What I would do next

Precision on clean PRs, not recall: detection is already 85% and 76%, and the wrong-flag rate is what
makes the tool unusable.

The first lever is cheap — **prompt adherence**. The prompt already forbids style and lint-level
notes, and several wrong flags are exactly that: calling a sort "fragile" for relying on Python's
guaranteed stable sort, an `if style:` versus `if style is not None:` nitpick. Two more concerned
documentation, not code. Severity is inflated too — four of the eleven wrong flags were `high` or
`critical`. Before anything sophisticated, test whether a stricter prompt and a severity rubric move
the wrong-flag rate on the dev subset. After that: a confidence threshold dropping findings the model
cannot tie to a mechanism, and repo-wide symbol indexing, so "this function does not accept that
argument" is checked before it is written.

## Engineering findings worth keeping

**Stale context appears to hurt quality, and fixing it fights the cache.** Showing the model only the
two most recent tool results, stubbing older ones, took detection from 2/4 to 4/4 on a six-case probe
and removed a false positive: a 40k-token file read is dead weight three rounds after the model has
taken what it needs. But trimming rewrites history, and OpenRouter's rolling cache holds only while
history stays append-only — so the trimmed configuration cost *more* than no cache at all. An
explicit per-block breakpoint disables that cache too: no error, just higher bills. Six cases is a
direction, not a result, so the default stayed untrimmed.

**A long, paid job must checkpoint per case.** The first attempt was killed by the operating system
at case 65 and lost everything, because results were written only at the end. Appending each case as
it completes — with a resume that refuses to continue if the model, temperature, scoring or dataset
changed — turned three later kills and a credit exhaustion into interruptions costing only the case
in flight.

**Injection containment held; reporting it did not.** Across twenty adversarial cases against the
real model, the agent never obeyed an injected instruction and the transport was never asked for
anything off its allowlist. But it *reported* the attempt in only fifteen: injections in file
contents and diffs were called out 14/14, those in the PR title, body or comments 1/6. Ignored beats
obeyed, but a review that silently drops the attempt never tells the reader someone tried to steer
it.
