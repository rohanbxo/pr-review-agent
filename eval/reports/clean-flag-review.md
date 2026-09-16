# Clean-flag review — reference run (`v1.json`)

Sibling of [`dev-case-review.md`](dev-case-review.md), same method: each claim checked against the
code in the case and against the upstream repository's later history (bare clones in
`eval/.cache/repos`). Reviewed 2026-09-17.

## What was sampled

The reference run (`anthropic/claude-haiku-4.5`, temperature 0, scoring v3) flagged **43 of 149
clean PRs** with at least one `medium`+ finding. Fifteen were drawn at random:

```
python eval/reports/sample_flags.py     # seed 20260917, random.Random(SEED).sample(sorted(flagged), 15)
```

The seed is `20260917` and the draw is over the sorted list of flagged case ids, so it reproduces
exactly. One clean case (`click-1801-clean`) timed out and never produced a review; it is an
infrastructure failure, excluded from the 149 and from this sample.

## Result

| | count | share |
|---|---|---|
| Genuine bug | 4 | 26.7% (95% CI 11–52%) |
| Defensible but wrong | 10 | |
| Nonsense | 1 | |
| **Wrong flags (the two together)** | **11** | **73.3% (95% CI 48–89%)** |

**28.9% is the share of clean PRs flagged, not the share of wrong flags.** Combining the two:
about **21% of clean PRs get a wrong flag** (the 28.9% flag rate times the 73% wrong share; range
roughly 14–26%), and about 8% get a flag that turns out to be a real bug. At 15 sampled flags the
interval is wide; it says "most flags are wrong, but a quarter are real", not more.

Three of the four genuine findings have an upstream commit fixing exactly what was flagged.

## The four genuine ones

| Case | Claim | Evidence |
|---|---|---|
| `attrs-1119-clean` | The PR stops `assoc` warning, but `TestAssoc::test_unknown` still wraps it in `pytest.deprecated_call()` | Upstream `b9084fa` "Remove pytest.deprecated_call() in TestAssoc::test_unknown" (#1249) does exactly that |
| `dateutil-681-clean` | Removing the `else: raise ValueError` leaves `tzinfo` unbound for unexpected types | Upstream `d0404c6`: "switches the error from UnboundLocalError to TypeError" |
| `httpx-3120-clean` | In a PR titled "Keep clients in sync", only `Client.get` gained `auth: ... | None`; the other sync verbs did not | Verified at the commit and still inconsistent at HEAD: `get` has `| None`, `post`/`put`/`patch`/`delete`/`head`/`options` do not. Typing-only |
| `werkzeug-1319-clean` | `www_authenticate=None` is wrapped as `(None,)`, which is truthy, so a literal `WWW-Authenticate: None` header is emitted | Confirmed in `get_headers` at that commit; at HEAD only real `WWWAuthenticate` objects are wrapped |

## The ten defensible-but-wrong

Each describes a real mechanism but not a defect: the behaviour is intended, pre-existing, or
unchanged upstream years later. Severity is usually overstated.

- **`dateutil-483-clean`** (medium): `could_be_day()` does not check the value is a whole number. Unchanged at HEAD nine years on; callers pass integer tokens.
- **`flask-3918-clean`** (high): indexing `error_handler_spec` (a `defaultdict`) inserts keys. True, but the keys are bounded and the line is unchanged at HEAD.
- **`httpx-42-clean`** (high): `Request.__init__` rebuilds the `URL`, losing `allow_relative`. Mechanism real, pre-existing, no failure shown.
- **`httpx-3367-clean`** (critical): the test now asserts `str(response.json())` instead of the dict. Weaker, intentional, unchanged at HEAD.
- **`marshmallow-1218-clean`** (medium): moving `*args` before `localtime` is a breaking signature change — which is the PR's stated purpose ("Use keyword-only arguments").
- **`more-itertools-796-clean`** (high): "missing level check before iterating nodes". The `while` loop checks the level each time it pops a group; structure unchanged at HEAD.
- **`networkx-3427-clean`** (medium): disputes a docstring, not code. Its technical point looks right (`G.degree(w)` counts a self-loop twice), but upstream kept the wording and the prompt asks for code defects.
- **`packaging-63-clean`** (medium): `|` → `^` changes MatchFirst to longest-match. That *is* the fix; it survived until `LegacySpecifier` was dropped.
- **`rich-1335-clean`** (high): calls the new sort "fragile" for relying on stable sort. Python guarantees stability; unchanged at HEAD.
- **`rich-3061-clean`** (medium): `if style is not None:` → `if style:` skips empty-string styles. Real, harmless (an empty-style span is a no-op), unchanged at HEAD.

## The one nonsense

- **`typer-1809-clean`** (high): flags the release-notes date `0.26.4 (2026-05-30)` as "in the
  future". It is in the past, and the file is `docs/release-notes.md`.

## Reading this next to the metric

- The eval counts a clean PR as a false positive when the agent raises any `medium`+ finding. It
  cannot tell a real bug from a wrong one, and 4 of 15 sampled flags were real bugs in merged code.
- The prompt forbids style and lint-level notes. Several wrong flags are exactly that (`rich-1335`,
  `rich-3061`, `dateutil-483`), and two are about documentation rather than code (`networkx-3427`,
  `typer-1809`). Severity is inflated across the board: four wrong flags were `high` or `critical`.
- The obvious lever is precision on clean PRs, not recall: detection is already 85% / 76%.
