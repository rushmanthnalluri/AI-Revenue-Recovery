# ADR 0007: Frontend design system mandated from the user's reference projects

- **Decision:** The console UI implements the binding spec in
  `docs/ui-design-system.md`, distilled from four user-specified reference
  projects — a dark operations-console language: flat amber `#D9A63F` on deep
  near-black `#0B0D0C`, Inter + IBM Plex Mono, hairline borders instead of
  shadows, mono-uppercase kickers, tabular numerals.
- **Context:** The user mandated the design language of their reference
  projects (`Placement-predict`, `Prop-pulse`,
  `ModularComponentShowcaseApplication`, `video_portfolio`) rather than
  leaving visual identity to per-screen improvisation. Multiple frontend
  agents build screens in parallel; without a binding token sheet and
  component recipes the console would drift into a patchwork.
- **Options:**
  1. Adopt a stock component library theme (shadcn default, MUI dark).
  2. Distill a binding design-system spec from the user's reference codebases
     and enforce it across all screens.
  3. Let each screen choose its own styling within Tailwind.
- **Chosen:** (2).
- **Why:** The reference projects are operations-console products with an
  already-resolved fintech visual language (Prop-pulse's own design master
  independently validates dark + amber and names the anti-patterns: light-mode
  default, ornate design, AI purple/pink gradients). A single token sheet
  (`@theme` in `globals.css`) plus component recipes gives parallel agents one
  source of truth, so screens compose without coordination overhead.
- **Tradeoffs:** Custom primitives instead of off-the-shelf theme coverage
  (more upfront component work); the spec is dark-only for v1; any future
  rebrand touches every screen at once — though the tokens centralize most of
  that change.
