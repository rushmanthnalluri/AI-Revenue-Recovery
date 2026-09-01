# PulseRecover UI Design System

> **Mandate (user-specified):** the PulseRecover UI must follow the design language of these reference projects:
> `D:/Machine_Learning/Placement-predict`, `D:/Machine_Learning/Prop-pulse`, `D:/FEDF/ModularComponentShowcaseApplication`, `D:/Portfolio/video_portfolio`.
> This document is the binding spec, distilled from those codebases. Frontend agents MUST implement to this spec.

## Decision summary

- **Base language = Placement-predict + Prop-pulse.** They are the same system (Prop-pulse's stylesheet was rebuilt on the Placement-predict blueprint), they are operations-console products, and Prop-pulse's `design-system/MASTER.md` independently validates dark + amber as the correct fintech language. Anti-patterns it names: light-mode default, ornate/playful design, **AI purple/pink gradients**.
- **From ModularComponentShowcaseApplication:** the glass sticky-header treatment and the layered radial page-wash idea — recolored to amber/slate at very low alpha.
- **From video_portfolio:** framer-motion for subtle reveal animations only. Its red/glow/pill/rotated-card language is brand-playful and is **excluded**.
- **Must-keep shared signature:** flat amber `#D9A63F` on deep near-black-green `#0B0D0C`, Inter for UI + IBM Plex Mono for data, hairline borders instead of shadows, mono-uppercase kickers, tabular numerals, inset 2px amber active-nav bar.

Reference implementations to consult when in doubt:
- `D:/Machine_Learning/Prop-pulse/frontend/src/styles.css` (token sheet)
- `D:/Machine_Learning/Prop-pulse/frontend/src/components/shared/chartTheme.js` (recharts skin)
- `D:/Machine_Learning/Prop-pulse/frontend/src/components/Layout.jsx` (shell + live status pill)
- `D:/Machine_Learning/Placement-predict/flask_project/static/css/style.css` (component vocabulary)

## Tokens (Tailwind 3.4 — `tailwind.config.ts` is the source of truth)

The project is on **Tailwind v3.4** (`darkMode: "class"`); there is no v4 `@theme` block. Every token lives in `theme.extend` of `frontend/tailwind.config.ts`:

```ts
// tailwind.config.ts — theme.extend
colors: {
  /* surfaces — elevation by brightness step, not shadow */
  bg: "#0B0D0C",
  surface: { DEFAULT: "#141715", hi: "#181D1A" },
  raised: "#1A1E1B",
  elevated: "#202521", // floating layers only

  /* hairlines */
  border: "#262B27",
  "border-strong": "#3A403B",

  /* text ramp */
  text: { DEFAULT: "#EFF0EB", 2: "#A2A8A0", 3: "#8A9189" },

  /* amber accent — THE signature */
  accent: {
    DEFAULT: "#D9A63F",
    hover: "#E3B254",
    ink: "#16130A",
    dim: "rgba(217, 166, 63, 0.14)",
    wash: "rgba(217, 166, 63, 0.05)",
    border: "rgba(217, 166, 63, 0.4)",
  },

  /* status — warning is amber (the accent doubles as the warn tone) */
  success: { DEFAULT: "#6FBF8C", dim: "rgba(111, 191, 140, 0.1)" },
  warning: { DEFAULT: "#D9A63F", dim: "rgba(217, 166, 63, 0.14)" },
  danger: { DEFAULT: "#D36B62", dim: "rgba(211, 107, 98, 0.1)", ink: "#21100E" },
  info: { DEFAULT: "#6E8FA0", dim: "rgba(110, 143, 160, 0.12)" }, // slate, secondary series
},
borderRadius: { sm: "4px", md: "6px", lg: "8px" }, // tight, not squircle
boxShadow: {
  /* floating layers ONLY (dropdown, toast, tooltip, modal) */
  pop: "0 8px 32px rgba(0, 0, 0, 0.45)",
  float: "0 4px 20px rgba(0, 0, 0, 0.5), 0 1px 3px rgba(0, 0, 0, 0.4)",
},
fontFamily: {
  sans: ["var(--font-inter)", "-apple-system", "Segoe UI", "sans-serif"],
  mono: ["var(--font-plex-mono)", "ui-monospace", "SF Mono", "monospace"],
},
fontSize: { "2xs": ["0.6875rem", { lineHeight: "0.875rem" }] },
transitionTimingFunction: { apple: "cubic-bezier(0.32, 0.72, 0, 1)" },
animation: { "status-pulse": "status-pulse 1.4s ease-in-out infinite" }, // alert dots only
```

Usage notes:
- Text is always the canonical ramp — `text-text` / `text-text-2` / `text-text-3`. The config still carries legacy shadcn aliases (`background`, `foreground`, `card`, `muted`, `primary`, `input`, `ring`) that resolve onto the same ramp so un-restyled files keep working; **new code must not use them** (`muted-foreground` is `#A2A8A0` = `text-2`, not the intended `text-3` muted gray).
- `danger.ink` (`#21100E`) is the on-danger text colour, the counterpart of `accent.ink` — used by the destructive button variant.
- Numeric-heavy readouts use `text-2xs` (11px) and the `.tnum` utility (tabular-nums).

Also in `globals.css` (v3 `@tailwind base/components/utilities` directives): `color-scheme: dark` on `html`; a universal default border colour (`*, ::before, ::after { @apply border-border }`) so a bare `border` always renders the spec hairline; body `bg-bg font-sans text-text` at 14px/1.55 with Inter feature settings and `font-variant-numeric: tabular-nums`; `::selection { background: rgba(217,166,63,0.24) }`; global `:focus-visible { outline: 2px solid #D9A63F; outline-offset: 2px }`; thin scrollbars (`#3A403B` thumb); the `prefers-reduced-motion` kill-switch. Components layer: `.card-sheen` (top-light overlay) and `.nav-item-active` (accent wash + inset 2px amber bar). Utilities layer: `.tnum` and `.page-wash` (the optional radial flourish). Fonts load via `next/font/google` in `layout.tsx` — Inter 400/500/600 and IBM Plex Mono 400/500/600, exposed as the `--font-inter` / `--font-plex-mono` CSS vars the `fontFamily` tokens reference.

## Component recipes

- **Card / panel**: `bg-surface border border-border rounded-lg` + card-sheen overlay `bg-[linear-gradient(180deg,rgba(255,255,255,0.022),transparent_110px)]` (the `.card-sheen` component class). No drop shadow. Interactive-card hover: `hover:border-border-strong` only (never lift, never glow).
- **Primary button**: `bg-accent text-accent-ink rounded-md px-4 py-2.5 text-[13px] font-medium`, hover `bg-accent-hover`. Secondary: `border border-border-strong text-text bg-transparent`, hover `bg-surface`. Destructive: `bg-danger text-danger-ink`, hover `bg-danger/90`. Transition 150ms `ease-apple`. Disabled: `opacity-60`.
- **Badge / status pill**: `font-mono text-[9.5px] uppercase tracking-[0.07em] px-[7px] py-[3px] rounded-sm border border-border-strong text-text-2`; accent variant `bg-accent-dim text-accent border-transparent`; success/danger variants use their `-dim` washes. Live status dot: 7px `rounded-full bg-success`, 1.4s pulse animation for degraded/alert states.
- **Table**: wrapper `border border-border rounded-lg overflow-auto`; header cells `font-mono text-[10px] uppercase tracking-[0.09em] text-text-3 bg-surface`; cells `px-3.5 py-2.5 text-[13px]`, hairline `border-b border-border` rows; row hover `bg-surface`; numbers `font-mono text-xs tabular-nums`; sticky header for long tables. Clickable rows get `role="link"` when they navigate, `role="button"` when they open an in-page layer (drawer/dialog) — `DataTable`'s `rowRole` prop.
- **Sidebar nav** (264px, sticky, `bg-surface`, right hairline): brand row = 28px amber-gradient tile (`linear-gradient(155deg,#E3B254,#D9A63F 55%,#B8892E)` + `inset 0 1px 0 rgba(255,255,255,0.25)`) with mono mark; section captions `font-mono text-[10px] uppercase tracking-[0.11em] text-text-3`; items 13px `text-text-2`, hover `bg-raised text-text`; **active = `.nav-item-active` — `bg-[linear-gradient(90deg,rgba(217,166,63,0.05),transparent_65%)] bg-raised` + `shadow-[inset_2px_0_0_#D9A63F]`**, leading number/icon turns amber.
- **Kicker / section title**: `font-mono text-[11px] uppercase tracking-[0.11em] text-text-3`; hero kicker preceded by an 18px × 1px amber tick.
- **Metric / KPI strip**: not cards — values in a row divided by hairline left borders; value `font-mono text-[23px] tabular-nums`, label `text-xs text-text-3`.
- **Inputs**: `bg-bg border border-border-strong rounded-md px-3 py-2 font-mono text-[13px]`, hover `border-text-3`, focus `border-accent`; invalid = danger border + `bg-danger-dim`.
- **Alerts / banners**: `border rounded-lg px-4 py-3.5 text-[13.5px] text-text-2`; error = `border-[rgba(198,93,85,0.45)] bg-danger-dim`; warn = `border-accent-border bg-accent-wash`.
- **Toast / dropdown / tooltip / modal**: `bg-elevated border border-border-strong rounded-lg shadow-float` (tooltip: mono 12px, 8px radius — matches the recharts skin).
- **Charts** (Recharts skin): y-grid-only `rgba(235,236,232,0.07)`, ticks 10px IBM Plex Mono `#8A9189`, no axis lines, series amber / slate `#6E8FA0` / danger `#D36B62` / neutral `#8A9189`, fills ~0.55 alpha, 450ms ease-out animation, off under reduced-motion.

## Operational patterns

- **Error surfaces** — every query/mutation failure renders `<ErrorPanel error={err} onRetry={…} title="…" />` (`components/error-panel.tsx`): the error banner recipe above plus a mono `code` / `request id` footer and a secondary Retry button. It classifies `ApiError` itself (unreachable / timeout / 401 / status); timeout detail comes from `err.message`, which carries the real wait (10s default, 120s for long-running endpoints) — never hardcode a duration. `alert()`, `window.confirm()` and console-only error surfaces are banned.
- **Empty & loading states** — empty datasets render `<EmptyState title description action>` (dashed `border-border-strong` panel, centered lucide icon, honest copy); pending renders `Skeleton` rows. Never fabricated placeholder numbers, never fake data.
- **Provenance chips** — KPI strips and detail headers pin a `<ProvenanceChip>`: real mode = emerald (`bg-success-dim text-success`) "Razorpay Test Mode · window · n records", research mode = slate (`bg-info-dim text-info`) "Synthetic Research Dataset · detail". Row level uses `<EnvironmentBadge>` / `<SourceTypeBadge>` with the same success/info split. Text always carries the meaning — colour is never the only signal.
- **Two-step confirms** — mutating operator actions are armed first, fired second: an outline button opens an inline confirm step, and the mutating call only fires from that step. Destructive ops (demo reset) use the danger panel + destructive button + ghost cancel; non-destructive mutating ops (reconciliation) use an inline primary "Confirm …" + ghost cancel. Pending swaps the icon for a spinner and disables both buttons; failure renders `ErrorPanel` with retry; success renders the real response, never assumed counts.
- **Reconciliation sweep** — "Run reconciliation" lives in the Recovery pipeline header (`ReconcileAction`, POST `/api/v1/recovery/reconcile`, ADR 0011): UNKNOWN actions are re-queried against gateway truth (GETs only, never a blind retry) and failed webhooks are re-run through the live handler registry. Idempotent, 120s client window. The result strip under the header reports the real sweep in mono with tabular numerals: sweep id, resolved/scanned UNKNOWN counts, reprocessed/still-failing webhook counts.

## Background treatments

- Default page: flat `bg-bg`. Nothing else.
- **Optional premium flourish** (from MCSA, recolored): one subtle layered wash at page top — the `.page-wash` utility: `radial-gradient(circle at 15% 0%, rgba(217,166,63,0.05), transparent 30%)` + `radial-gradient(circle at 85% 5%, rgba(110,143,160,0.05), transparent 25%)`. Never stronger.
- Sticky top header over scrolling content: `bg-bg/80 backdrop-blur-[18px] border-b border-border` — the one sanctioned glassmorphism use.
- Marketing/login hero only may use a faint 64px hairline grid (`rgba(148,163,184,0.07)` lines, masked to fade downward).

## Motion

- CSS transitions for hover/focus: 150ms on `cubic-bezier(0.32,0.72,0,1)` (the `ease-apple` utility). Emphasis transitions (drawers, result fills): 500ms same curve.
- framer-motion for route reveals / list staggering only: fade + 8–12px rise, ease-out, ≤450ms, respect `useReducedMotion`. No springs, no bounce, no scale-on-hover for controls.
- Status pulse `1.4s ease-in-out infinite` (`animate-status-pulse`) on alert dots only. Spinners 0.7s linear.

## Do / Don't

**Do**
- Hairline 1px borders as the primary separation device; mono uppercase micro-labels; tabular-nums everywhere numbers appear.
- One accent only: amber `#D9A63F`, used flat (buttons, active states, key metrics, focus rings).
- Two-tone shell: sidebar one step lighter than the page backdrop.
- Live-status chrome (API pill, "up 1h · 42ms avg" mono strips) — fits the ops-console identity.
- `rounded-full` only for status dots and pill badges; `rounded-md/lg` (6/8px) for everything else.
- Icons: inline SVG (lucide-react) at 18px, `currentColor`, 1.5 stroke, round caps.

**Don't**
- No purple/pink/AI gradients, no neon glows, no colored drop shadows.
- No drop shadows on cards — sheen + hairline only; shadows mean "floating layer".
- No gradient text, no gradient buttons.
- No light mode for v1.
- No bounce/spring easing, no rotated cards, no pill-shaped primary buttons.
- No emoji as icons.

## Content layout

- Sidebar + content column max 940px (1200px ≥1440px viewport), 40px section rhythm, sections separated by hairline dividers.
- Sidebar footer: live "API status" pill (status dot + mono label) fed by `/api/v1/system/health`.
- Error/degraded states: banner bar above content (see Alerts recipe), never fake data.
