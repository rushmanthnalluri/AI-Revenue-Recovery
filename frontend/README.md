# PulseRecover Frontend

Premium dark operations console for the PulseRecover engine (Razorpay AI
Buildathon Track 03). Next.js 15 App Router · TypeScript strict · Tailwind ·
shadcn-style primitives · Recharts · TanStack Query · lucide-react · Inter
(next/font).

## Run

```bash
npm install
cp .env.example .env.local   # NEXT_PUBLIC_API_BASE_URL, NEXT_PUBLIC_API_KEY
npm run dev                  # http://localhost:3000 (backend on :8000)
```

Scripts: `npm run dev` · `npm run build` · `npm run start` · `npm run lint` ·
`npm run typecheck`.

## Layout

```
src/
  lib/
    types.ts    contract types — mirror of contracts/openapi.json
    api.ts      typed client for every endpoint (X-API-Key, 10s timeout,
                typed ApiError from the backend error envelope)
    format.ts   paise -> ₹ (Intl.NumberFormat en-IN), %, relative time
    utils.ts    cn()
  components/
    ui/         shadcn-style primitives (button, card, badge, skeleton,
                table, input, select)
    app-shell / sidebar / topbar (env badge + live health indicator)
    delta-badge, status-pill, section-card, data-table,
    timeline, confidence-bar, error-panel, empty-state,
    page-header
    views/      per-route client views
  app/          routes: / /incidents /incidents/[id] /recovery /audit
                /evaluation (+ loading / error / not-found)
```

## Conventions

- Money arrives as integer paise and is only ever formatted via
  `formatINR` — never fabricate numbers; loading uses skeletons, failures use
  `ErrorPanel`, empty uses `EmptyState`.
- Semantic tones: emerald ok · amber warn · red crit · sky in-progress ·
  slate neutral (`StatusPill` maps every API enum onto these).
- Metrics render with `tabular-nums` (`.tnum` / `tnum` class).
- Keyboard: native links/buttons, visible focus rings, skip-to-content link.
