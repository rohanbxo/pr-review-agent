# DESIGN.md — frontend design reference

The frontend's job is the product's job: **to be trusted**. Every design decision below serves
one of three promises:

1. **Findings are anchored to real lines.** A finding without a file and a line range is not
   shown as a finding. The location is the most prominent thing after the severity.
2. **An empty review is a good outcome.** "No issues found" is rendered as a positive, complete
   result — never as a blank page, a spinner, or an apology.
3. **The record of what the agent read is visible.** Files reviewed and every GitHub call
   (including blocked ones) are one click away on every review.

The audience lives in GitHub. Match its density and conventions; do not decorate.

---

## Hard rules

These are not stylistic preferences. A change that breaks one is a bug.

### 1. Colour means severity and nothing else

Hue appears in exactly one place: the severity scale below. Everything else — text, surfaces,
borders, links, buttons, focus rings, status (queued/running/succeeded/failed), errors,
role badges — is neutral grey.

Non-severity affordances are expressed with **weight, underline, borders, and text**:

| Affordance | Treatment |
|---|---|
| Link | foreground colour, underline (offset 2–4px), heavier underline on hover |
| Primary button | solid near-black fill (neutral), white text |
| Secondary button | 1px neutral border, transparent fill |
| Focus | 2px solid neutral outline (`--foreground`), 2px offset — never a coloured ring |
| Run status | text label in the monospace face, 1px neutral border; `failed` is bold |
| Form error | bold text + a thick neutral border on the field + `aria-invalid` |
| 403 / 429 / API error | bordered neutral box, bold heading, plain explanation |

A red "failed" status or a green "success" check would teach users that colour means
something other than severity, and weaken the one signal we care about. Don't.

### 2. Severity renders as a left rule, never a filled chip

A finding is a block with a **4px left border** in its severity colour. The severity word is
printed as plain uppercase text (small, bold, neutral foreground) next to the title. No
filled pills, no background tints, no coloured badges, no coloured icons.

Overall PR risk (`low | medium | high`) uses the same left-rule treatment on the summary block.

---

## Severity scale

The only hue in the product. Tokens live in `frontend/app/globals.css`; the mapping from
severity to classes lives in one module (`frontend/lib/severity.ts`) and is unit-tested.

| Severity | Token | Light | Dark | Label |
|---|---|---|---|---|
| low | `--severity-low` | `oklch(0.62 0.02 250)` slate | `oklch(0.70 0.02 250)` | `LOW` |
| medium | `--severity-medium` | `oklch(0.72 0.15 75)` amber | `oklch(0.80 0.15 80)` | `MEDIUM` |
| high | `--severity-high` | `oklch(0.63 0.20 40)` orange | `oklch(0.72 0.18 45)` | `HIGH` |
| critical | `--severity-critical` | `oklch(0.53 0.21 27)` red | `oklch(0.66 0.21 27)` | `CRITICAL` |

Ordering is by lightness *and* hue so the scale survives greyscale printing and most colour
vision deficiencies. `low` is deliberately near-neutral: a low finding should not shout.

Findings are sorted within a file by severity (critical first), then start line.

---

## Accessibility

- **Severity is never conveyed by colour alone.** The label (`CRITICAL`, `HIGH`, …) is always
  rendered as visible text, and the block carries `data-severity` for tests.
- The left rule is decorative reinforcement: it is a border, not content, and meets 3:1
  non-text contrast against the page background in both themes.
- Body text meets WCAG AA (4.5:1). Muted text (`--muted-foreground`) is used only for
  secondary metadata and still meets 4.5:1.
- Focus is always visible (rule 1 treatment); never `outline: none` without a replacement.
- Polling status changes are announced via an `aria-live="polite"` region.
- Tables of GitHub calls use real `<table>` markup with header cells.
- Every interactive element is reachable and operable by keyboard.

---

## Typography

- **UI face:** system stack (`-apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans",
  Helvetica, Arial, sans-serif`) — the same family GitHub uses, and no build-time font fetch.
- **Monospace:** `ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono",
  monospace`. Used for **file paths, line ranges, repo `owner/name`, PR numbers, HTTP methods,
  API paths, status codes, durations, run ids, and run status**. Anything a user might copy
  into a terminal or search for in GitHub is monospace.
- Base size 14px; metadata 12px; page title 20px semibold; section headings 14px semibold.
  No display sizes. Line height 1.5 for prose (detail, suggestion), 1.25 for dense rows.
- Line ranges render as `L12–L18` (or `L12` when start = end), matching GitHub's anchor style.

---

## Density and layout

- Single column, `max-width: 72rem`, 16px side gutter at every width.
- Rows are compact: 8px vertical padding in lists and tables, 12–16px inside finding blocks.
- Radius is small (4–6px). 1px neutral borders separate regions; no shadows.
- Header: product name (left), signed-in `github_login` + role as monospace text in a bordered
  box, sign-out (right).
- The review page, top to bottom: title (`owner/name#123`, status), summary with risk rule,
  findings grouped by file, "What the agent read", step timeline.
- Files group header: monospace path, finding count, link "View in PR" to
  `https://github.com/{repo}/pull/{n}/files`.

---

## States

| State | Treatment |
|---|---|
| Queued / running | status text + "Checking again every few seconds"; skeleton-free — show what is known (repo, PR, created time) |
| Succeeded, findings present | findings grouped by file |
| **Succeeded, no findings** | A bordered block, bold heading **"No issues found"**, and "The agent reviewed N files and found nothing worth flagging." Followed by the file list. This is a result, not an absence. |
| Failed | bold "Review failed", the backend `error` string in monospace, and the agent-read panel (the call log is persisted whatever the outcome) |
| No runs yet | "No reviews yet. Start one above." |
| 403 | "You don't have permission to …" — plain, bordered, no colour |
| 429 | "Review quota reached. Try again in {Retry-After} (≈ HH:MM)." |
| Sign-in rejected | "Your GitHub account isn't authorised for this app." |

---

## "What the agent read"

Always present on a review page once steps exist.

- **Files reviewed:** monospace list from `result.files_reviewed`, with a count.
- **GitHub calls:** a table — method, path, status, duration (ms). A **blocked** call is shown
  with the word `BLOCKED` in bold uppercase in the status column and a thick left border on the
  row (neutral foreground, not a severity colour — it is a transport event, not a finding).
  A summary line states "N calls, M blocked"; "0 blocked" is stated explicitly.

---

## Implementation notes

- Tokens: shadcn/ui neutral base colour; `--primary`, `--ring`, `--destructive`, chart and
  sidebar tokens are overridden to neutrals so no shadcn component can introduce hue.
- Never use Tailwind palette hues (`red-500`, `green-600`, …) in components. Severity colours
  are referenced only via `lib/severity.ts`.
