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

## Tokens (Tailwind v4 `@theme` in `globals.css`)

```css
@theme {
  /* surfaces — elevation by brightness step, not shadow */
  --color-bg: #0B0D0C;
  --color-surface: #141715;
  --color-surface-hi: #181D1A;
  --color-raised: #1A1E1B;
  --color-elevated: #202521;      /* floating layers only */

  /* hairlines */
  --color-border: #262B27;
  --color-border-strong: #3A403B;

  /* text ramp */
  --color-text: #EFF0EB;
  --color-text-2: #A2A8A0;
  --color-text-3: #8A9189;

  /* amber accent — THE signature */
  --color-accent: #D9A63F;
  --color-accent-hover: #E3B254;
  --color-accent-ink: #16130A;
  --color-accent-dim: rgba(217, 166, 63, 0.14);
  --color-accent-wash: rgba(217, 166, 63, 0.05);
  --color-accent-border: rgba(217, 166, 63, 0.4);

  /* status */
  --color-success: #6FBF8C;
  --color-success-dim: rgba(111, 191, 140, 0.1);
  --color-danger: #D36B62;
  --color-danger-dim: rgba(211, 107, 98, 0.1);
  --color-info: #6E8FA0;          /* slate, secondary series */
  --color-info-dim: rgba(110, 143, 160, 0.12);

  /* radius — tight, not squircle */
  --radius-sm: 4px;
  --radius-md: 6px;
  --radius-lg: 8px;

  /* shadows — floating layers ONLY (dropdown, toast, tooltip, modal) */
  --shadow-pop: 0 8px 32px rgba(0, 0, 0, 0.45);
  --shadow-float: 0 4px 20px rgba(0, 0, 0, 0.5), 0 1px 3px rgba(0, 0, 0, 0.4);

  --font-sans: "Inter", -apple-system, "Segoe UI", sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, "SF Mono", monospace;

  --ease-apple: cubic-bezier(0.32, 0.72, 0, 1);
}
```

Also in `globals.css`: `color-scheme: dark`; body 14px/1.55; `::selection { background: rgba(217,166,63,0.24) }`; global `:focus-visible { outline: 2px solid #D9A63F; outline-offset: 2px }`; thin scrollbars (`#3A403B` thumb); `prefers-reduced-motion` kill-switch; `font-variant-numeric: tabular-nums` on all numeric/mono elements. Load Inter 400/500/600 + IBM Plex Mono 400/500/600 via `next/font/google`.

## Component recipes

- **Card / panel**: `bg-surface border border-border rounded-lg` + card-sheen overlay `bg-[linear-gradient(180deg,rgba(255,255,255,0.022),transparent_110px)]`. No drop shadow. Interactive-card hover: `hover:border-border-strong` only (never lift, never glow).
- **Primary button**: `bg-accent text-accent-ink rounded-md px-4 py-2.5 text-[13px] font-medium`, hover `bg-accent-hover`. Secondary: `border border-border-strong text-text bg-transparent`, hover `bg-surface`. Transition 150ms `ease-apple`. Disabled: `opacity-60`.
- **Badge / status pill**: `font-mono text-[9.5px] uppercase tracking-[0.07em] px-[7px] py-[3px] rounded-sm border border-border-strong text-text-2`; accent variant `bg-accent-dim text-accent border-transparent`; success/danger variants use their `-dim` washes. Live status dot: 7px `rounded-full bg-success`, 1.4s pulse animation for degraded/alert states.
- **Table**: wrapper `border border-border rounded-lg overflow-auto`; header cells `font-mono text-[10px] uppercase tracking-[0.09em] text-text-3 bg-surface`; cells `px-3.5 py-2.5 text-[13px]`, hairline `border-b border-border` rows; row hover `bg-surface`; numbers `font-mono text-xs tabular-nums`; sticky header for long tables.
- **Sidebar nav** (264px, sticky, `bg-surface`, right hairline): brand row = 28px amber-gradient tile (`linear-gradient(155deg,#E3B254,#D9A63F 55%,#B8892E)` + `inset 0 1px 0 rgba(255,255,255,0.25)`) with mono mark; section captions `font-mono text-[10px] uppercase tracking-[0.11em] text-text-3`; items 13px `text-text-2`, hover `bg-raised text-text`; **active = `bg-[linear-gradient(90deg,rgba(217,166,63,0.05),transparent_65%)] bg-raised` + `shadow-[inset_2px_0_0_#D9A63F]`**, leading number/icon turns amber.
- **Kicker / section title**: `font-mono text-[11px] uppercase tracking-[0.11em] text-text-3`; hero kicker preceded by an 18px × 1px amber tick.
- **Metric / KPI strip**: not cards — values in a row divided by hairline left borders; value `font-mono text-[23px] tabular-nums`, label `text-xs text-text-3`.
- **Inputs**: `bg-bg border border-border-strong rounded-md px-3 py-2 font-mono text-[13px]`, hover `border-text-3`, focus `border-accent`; invalid = danger border + `bg-danger-dim`.
- **Alerts / banners**: `border rounded-lg px-4 py-3.5 text-[13.5px] text-text-2`; error = `border-[rgba(198,93,85,0.45)] bg-danger-dim`; warn = `border-accent-border bg-accent-wash`.
- **Toast / dropdown / tooltip / modal**: `bg-elevated border border-border-strong rounded-lg shadow-float` (tooltip: mono 12px, 8px radius — matches the recharts skin).
- **Charts** (Recharts skin): y-grid-only `rgba(235,236,232,0.07)`, ticks 10px IBM Plex Mono `#8A9189`, no axis lines, series amber / slate `#6E8FA0` / danger `#D36B62` / neutral `#8A9189`, fills ~0.55 alpha, 450ms ease-out animation, off under reduced-motion.

## Background treatments

- Default page: flat `bg-bg`. Nothing else.
- **Optional premium flourish** (from MCSA, recolored): one subtle layered wash at page top — `radial-gradient(circle at 15% 0%, rgba(217,166,63,0.05), transparent 30%)` + `radial-gradient(circle at 85% 5%, rgba(110,143,160,0.05), transparent 25%)`. Never stronger.
- Sticky top header over scrolling content: `bg-bg/80 backdrop-blur-[18px] border-b border-border` — the one sanctioned glassmorphism use.
- Marketing/login hero only may use a faint 64px hairline grid (`rgba(148,163,184,0.07)` lines, masked to fade downward).

## Motion

- CSS transitions for hover/focus: 150ms on `cubic-bezier(0.32,0.72,0,1)`. Emphasis transitions (drawers, result fills): 500ms same curve.
- framer-motion for route reveals / list staggering only: fade + 8–12px rise, ease-out, ≤450ms, respect `useReducedMotion`. No springs, no bounce, no scale-on-hover for controls.
- Status pulse `1.4s ease-in-out infinite` on alert dots only. Spinners 0.7s linear.

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
