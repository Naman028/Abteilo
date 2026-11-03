# Abteilo Design System (Master)

> Page files in `design-system/abteilo/pages/` override this file where they
> say so. Tokens live in `frontend/src/styles.css`; components in
> `frontend/src/components/ui.tsx`. Change the tokens there, not per page.

**Product:** a B2B operations workspace for German engineering and technology
companies: four department workspaces (Engineering, Finance, HR, IT & Security)
and an owner console, with an integrated AI assistant that shows its evidence.

**Source:** generated with `ui-ux-pro-max` 2.15.0 (`--design-system`, query
"B2B enterprise SaaS operations dashboard professional", variance 2, motion 2,
density 8), then adapted. The adaptations are listed under "Deviations".

## 1. Direction

- **Style:** minimal Swiss / enterprise.
  - The grid, type hierarchy and borders carry the structure, not decoration.
  - One accent colour. Status is shown by colour **and** text, never colour alone.
- **Density:** dashboard-dense.
  - Tables and forms fit a 1280 px laptop without scrolling sideways.
  - Readable at 100 % zoom; nothing below 12 px.
- **Motion:** subtle. Only colour, border and shadow transitions, at 120-160 ms.
  - No entrance animations, scroll reveals, parallax or transforms on hover.
  - `prefers-reduced-motion` turns transitions off.
- **Never:**
  - gradients, glassmorphism, glow, neon;
  - purple/pink "AI" colouring, sparkle icons;
  - emoji as icons, hero banners, marketing copy inside the product.

## 2. Tokens

### Colour (light / dark)

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#F8FAFC` | `#0B1220` | Page background |
| `--surface` | `#FFFFFF` | `#111A2B` | Cards, sidebar, inputs |
| `--surface-2` | `#F1F5F9` | `#172235` | Hover, table header, code, quotes |
| `--border` | `#E2E8F0` | `#25324A` | All 1 px borders |
| `--border-strong` | `#CBD5E1` | `#334462` | Inputs, focusable outlines |
| `--text` | `#0F172A` | `#E2E8F0` | Body text |
| `--muted` | `#475569` | `#94A3B8` | Secondary text (4.5:1 on `--surface` in both modes) |
| `--accent` | `#0369A1` | `#38BDF8` | Primary action, links, active nav, focus ring |
| `--accent-strong` | `#075985` | `#7DD3FC` | Primary hover |
| `--accent-soft` | `#E0F2FE` | `#0C2A40` | Active nav background, info notices |
| `--on-accent` | `#FFFFFF` | `#0B1220` | Text on the accent |
| `--ok` / `--ok-soft` | `#15803D` / `#DCFCE7` | `#4ADE80` / `#0F2E1C` | Verified, approved, active |
| `--warn` / `--warn-soft` | `#92400E` / `#FEF3C7` | `#FACC15` / `#33280A` | Waiting, in progress, partial |
| `--bad` / `--bad-soft` | `#B91C1C` / `#FEE2E2` | `#F87171` / `#3B1414` | Rejected, blocked, unsupported, destructive |

Contrast: on its background, `--accent` on white is 5.9:1, `--muted` on white is 7.6:1, and every status colour on its `-soft` background is at least 4.5:1.

### Typography

- **Family:** the system stack: `"Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif`. Monospace: `ui-monospace, "Cascadia Mono", Consolas, monospace` (codes, amounts, ids).
- **Scale (root 15 px):**

  | Element | Size | Line height | Weight |
  |---|---|---|---|
  | Page title (h1) | 1.5rem | 1.25 | 650 |
  | Section (h2) | 1.0rem | 1.3 | 650 |
  | Sub-section (h3) | 0.93rem | 1.3 | 600 |
  | Body | 1rem | 1.5 | 400 |
  | Table | 0.9rem | 1.5 | 400 |
  | Label, meta | 0.82rem | 1.5 | 400 |
  | Overline (nav groups, table heads) | 0.72rem, uppercase, letter-spacing 0.06em | 1.5 | 400 |

- Figures use `font-variant-numeric: tabular-nums` and are right-aligned in tables.
- Dates and amounts use the `de-DE` format (`api.ts`).

### Spacing, radius, elevation

- **Spacing scale:** 4 / 8 / 12 / 16 / 24 / 32 px (`--s1`..`--s6`).
  - Cards: 16 px padding.
  - Page gutter: 32 px on desktop, 16 px on mobile.
- **Radius:** 6 px for controls and badges, 8 px for cards and panels.
- **Elevation:** cards have a border and `0 1px 2px rgb(15 23 42 / 0.04)` only. Nothing floats except the focus ring.

### Interaction

- **Focus:** every operable element shows `outline: 2px solid var(--accent); outline-offset: 2px` on `:focus-visible`. The ring is never removed.
- **Pointer:** `cursor: pointer` on anything clickable.
- **Hit targets:** at least 32 px on fine pointers, 44 px on `(pointer: coarse)`.
- **Disabled:** 55 % opacity, `cursor: not-allowed`, and the reason is shown near the control when it is not obvious.

## 3. Components (`components/ui.tsx` + `styles.css`)

| Component | Contract |
|---|---|
| `PageHeader` | Breadcrumb (`nav[aria-label=Breadcrumb]`, current page `aria-current="page"`), h1, one-line description, optional meta and actions on the right. Every routed page starts with it. |
| `Tabs` | `role="tablist"`, buttons with `role="tab"`, `aria-selected`, `aria-controls`. Arrow keys, Home and End move between tabs (roving `tabIndex`). The panel has `role="tabpanel"`. |
| `Stat` | A `button` when it navigates, otherwise a `div`. The value (tabular, 1.5rem) sits above the label. |
| Button | `.primary` (accent, one per area), default (bordered), `.danger` (red text, destructive only), `.small`. |
| Row selection | A selectable table row puts a `button.row-select` in its first cell (keyboard reachable) and marks the selected row with `aria-selected` styling. Rows never use `onClick` alone. |
| Badge | Status pill: colour plus its text. The status vocabulary maps to ok, warn or bad in `styles.css`. |
| Notice / error | Inline, next to what caused it. Errors use `role="alert"`. |
| Empty state | A sentence saying what is missing and, where the user can act, what to do next. |
| Evidence chip | A `button` with `aria-expanded`. The evidence panel shows the source title, authority (live record, document, ...) and as-of date. |
| Department mark | A two-letter monogram tile (EN, FI, HR, IT, or the initials of a custom department), neutral colours. It identifies the department, not its status. |

## 4. Layout

- **Shell:**
  - fixed 248 px sidebar: brand, company, grouped navigation, user footer;
  - fluid main column with a 1200 px maximum width;
  - below 860 px the sidebar stacks above the content.
- **Sidebar groups:** Home · Departments (with department marks) · Personal · Company. The active item has the accent-soft background, the accent text and `aria-current="page"` (from `NavLink`).
- **Page anatomy:** PageHeader → tabs (if any) → content grid (`grid two` for list + detail, `grid stats` for KPI rows).

## 5. Page-by-page

| Page | Changes |
|---|---|
| Login | Brand, product line, labelled fields, inline error with `role="alert"`, privacy note. Layout otherwise unchanged. |
| Home | Breadcrumb "Home". Greeting as the h1, with the company as its description. Stats unchanged. Workspace links carry department marks. Empty states explain the next step. |
| Department workspace | Breadcrumb Home › Departments › {name}. The description comes from the department template (what the workspace is for). The meta line shows the roles granted, in words. Accessible tabs. |
| Engineering | Project rows are selectable by keyboard (`row-select` button). Task table and connections unchanged. Connections: authority badges, quotes as blockquotes. |
| Finance, HR, IT | Visual consistency through the shared tokens and table styles. No behaviour change. |
| Documents | Document rows open through a `row-select` button. Search hits unchanged. |
| Assistant | Rendered as a native panel with a header ("Assistant · {department}", the data-scope note, the model class picker). Claim marks are explained in a legend. Evidence chips are buttons. A summary that is not verified shows a text badge, not a warning emoji. |
| Owner console | Breadcrumb Home › Owner console. Accessible tabs. |
| Owner overview | "Needs your decision" comes first, with an explicit "Nothing waits for you" empty state. KPI tiles that navigate are buttons. The rest is unchanged: blocked work, budget pressure, departments table, AI platform, activity. |
| My requests, Privacy | PageHeader with breadcrumb. No behaviour change. |

## 6. Deviations from the generated recommendation

| Generated | Abteilo | Why |
|---|---|---|
| Plus Jakarta Sans from Google Fonts | System font stack | Loading Google Fonts from Google's CDN sends visitors' IP addresses to Google; German courts have found this unlawful without consent (LG München I, 3 O 17493/20). Self-hosting would add a dependency and asset weight that a demo does not need. |
| GSAP scroll-reveal snippet | None | No new packages. Motion is limited to state transitions. |
| Landing pattern (hero, logos, CTA) | Not used | Abteilo is an application, not a marketing page. |
| "Stream text response token by token" (UX guideline) | Business answers stream claim by claim, each only after verification, with a live status line ("Writing the answer; each statement is checked before it appears…"). Non-factual small talk is not a separate path. | Unverified business facts are never shown as facts (INT-10). The status line and early sources keep the wait informative without a spinner. |
| Cards that lift on hover; modal backdrop blur | Static cards; no blur | A lift implies clickability where there is none. Blur is a glass effect. |
| 12 x 24 px button padding, 16 px inputs | 7 x 12 px controls (32 px high), 44 px on touch | Dashboard density; touch targets are kept by the coarse-pointer rule. |

## 7. Pre-delivery checklist

- [ ] No emoji used as icons. Text marks (✓ ≈ ⟲ ≠ ?) are explained in a legend and a tooltip.
- [ ] Every clickable element is a `button` or `a`, and shows a visible focus ring.
- [ ] Contrast is at least 4.5:1 for text in light and dark modes.
- [ ] `prefers-reduced-motion` is respected.
- [ ] No horizontal page scroll at 375 px; wide tables scroll inside `.table-wrap`.
- [ ] Routes, API calls and permission checks are unchanged (frontend diff review).
- [ ] `npm run build` passes, and the browser checks pass.
