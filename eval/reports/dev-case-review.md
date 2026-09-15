# Dev run case review — anthropic/claude-haiku-4.5

Human review of `eval/reports/dev.json`: OpenRouter, 60-case dev split, first run, default
temperature, no prompt caching. Every claim was checked against the code in the case and against
the upstream repository's later history (bare clones in `eval/.cache/repos`). Reviewed 2026-09-16.

## Headline

**3 of the 6 medium+ findings on "clean" PRs are genuine bugs.** Two of them have a matching
upstream fix, and the third was fixed upstream later after it surfaced. "Clean" means *merged
upstream*, not *bug-free*, and the agent found real bugs that shipped.

- **Reported false-positive rate:** 17.1% (6/35).
- **Corrected false-positive rate:** 8.6% (3/35), counting only the flags that are wrong.
- **If `werkzeug-324` is not credited:** 11.4% (4/35). It is a real latent defect, but it predates
  the PR under review.

The metric itself is not adjusted. `eval/reports/dev.json` still says 17.1%, and it should,
because the dataset labels are what they are. This file records why the number overstates the
agent's false-positive rate.

## The 6 flagged clean PRs

| Case | Agent's claim | Verdict | Upstream fix |
|---|---|---|---|
| `rich-2635-clean` | Replacing `try/except` with `if "get_ipython" in globals()` breaks IPython setup in `install()` | **Genuine.** `get_ipython` is an IPython builtin and never a key in the module's `globals()`, so the rich formatter was never installed. The agent's stated mechanism (lost exception handling) was partly off; the lines, the regression and the suggested fix were right. | `Textualize/rich@ea1129af` "fix breakage" (2023-07-29) restores `try: get_ipython() except NameError` |
| `dateutil-681-clean` | Removing the `else: raise ValueError` leaves `tzinfo` unbound for unexpected types | **Genuine.** Any other `tzdata` type hits `return tzinfo` unassigned. | `dateutil/dateutil@d0404c6` "Fix error condition when invalid tzinfos is passed", which "switches the error from UnboundLocalError to TypeError" (2019-03-20) |
| `werkzeug-324-clean` | Byte-replacing `werkzeug.datastructures` inside a pickle corrupts it | **Genuine but latent, and older than the PR.** Replacement is safe for pickle protocols 0–3 and breaks on 4+ (length-prefixed names); this was reproduced. The PR only changed string literals to bytes. It is test code, and "high" is overstated. | `pallets/werkzeug@13218dea` "Fix #502" (2014-03-21), citing PEP 3154 |
| `httpx-3371-clean` | `%` missing from the percent-encoding safe sets, and the tests were changed to match (critical) | **Defensible but wrong.** The WHATWG percent-encode sets deliberately exclude `%`, and `quote()` preserves existing `%xx`. Upstream behaves the same at HEAD. A literal `%41` in a password is ambiguous, but that is a design choice, not a critical bug. | none |
| `click-675-clean` | `if not var:` is inconsistent with the parent's `if metavar is None:` | **Defensible but wrong.** A consistency note: the two fall back to different values on purpose, and the line is unchanged at HEAD. It is style-level, which the prompt forbids. | none |
| `werkzeug-72-clean` | `str()` on encoded bytes produces `"b'...'"` and corrupts multipart bodies (critical) | **Nonsense.** The file is Python 2 (2011: `cStringIO`, `urllib2`). `str()` on a Python 2 byte string is the identity; `b'...'` is Python 3 repr behaviour. | none |

## The 6 missed bugs

**Reported nothing about the bug (2).** These are plain misses: the agent found nothing in the file.

- **`marshmallow-3eff74cee0-reverted`** (`marshmallow/utils.py:299-300`, reverted fix): no findings.
- **`attrs-97f8d17565-reverted`** (`src/attr/_make.py:303-304`, reverted fix): no findings.

**Flagged something else, and that flag was wrong (4).** This is the worse failure: the bug stayed
in, and a reviewer was sent to chase a non-issue.

- **`flask-4580-removed_none_guard`** (`src/flask/app.py:1751-1752`). The agent claimed Werkzeug's
  `MapAdapter.build()` does not accept `url_scheme` (critical). **False:** the argument was added in
  Werkzeug 2.0.0, and this Flask commit requires `Werkzeug >= 2.0`.
- **`more-itertools-423-off_by_one_range_len`** (`more_itertools/more.py:601`). The agent called
  `A[:i - size:-1]  # A[i + 1:][::-1]` a wrong slice at two sites (critical, 7 lines from the bug).
  **False:** it was verified equal to the commented expression for every `i < n <= 11`.
- **`requests-5b4b64c346-reverted`** (`src/requests/utils.py:220-228`). The agent claimed
  `any(_netrc)` rejects a valid netrc tuple with an empty login. **False:**
  `any(('', 'account', 'password'))` is `True`. It also called upstream's deliberate `mkstemp`
  rewrite of `extract_zipped_paths` a resource leak.
- **`requests-47914226c2-reverted`** (`src/requests/utils.py:234-235`). The agent called the same
  deliberate `mkstemp` rewrite a loss of "atomic placement semantics" and flagged an undetected
  partial write. **Wrong:** that change is upstream's later hardening, not the regression. One
  sub-point, a temp file left behind if `os.write` raises, is technically true but trivial.

## Also found during review

**Scoring.** `requests-2115-removed_none_guard` pinned the bug exactly (`443-444`, high) but scored
as not localised: under scoring v2 the 6-line change gave a 1.5-line limit. That led to scoring v3
(3-line floor). Under v3 the run localises 19/19 detected bugs; see `dev-nocache-v3.json`.
