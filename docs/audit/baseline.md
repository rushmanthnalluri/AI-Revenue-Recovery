# Audit Baseline — PulseRecover

Captured: 2026-09-02 (start of the full engineering audit).
Everything below is verified fact from the repository and the live deployment, not documentation claims.

## Repository

- Root: `D:/Razorpay`
- Branch: `main`
- HEAD commit: `dcef95a` — "fix(sync): degrade per entity on 4xx endpoint refusals (subscriptions 401)"
- Remote: `https://github.com/rushmanthnalluri/AI-Revenue-Recovery` (public)
- Uncommitted at audit start: 1 file (`frontend/scripts/rz-discover.mjs` — scratch Playwright probe, not production code)

## Runtimes

- Python: 3.14.5 (venv `backend/.venv`); Docker backend image pins python:3.12-slim
- Node: v24.14.0 (engines: >=20); frontend Docker image node:20-alpine
- Backend: FastAPI 0.141.1, SQLAlchemy 2.0.52, pydantic 2.13.4, alembic 1.19.1, httpx 0.28.1, scikit-learn 1.9.0, pandas 3.0.5, psycopg[binary] 3.3.4 (pinned `backend/requirements.txt`)
- Frontend: Next.js ^15.5.0, React ^19.1.0, Tailwind 3.4, @tanstack/react-query 5, Playwright 1.62.1 (chromium installed)

## Database

- Local dev default: SQLite (`pulserecover.db` at repo root)
- Production (deployed): Neon serverless Postgres (us-east-2), pooled DSN, migrations at head `a83af82e8438` (4 revisions)
- Compose stack path: postgres:16 (ADR 0002)

## Deployment (live at audit start)

- Blueprint: `render.yaml` (repo root) → Render services `pulserecover-api` (Docker, migrate-on-boot) and `pulserecover-web` (Next.js standalone), both free plan (15-min idle spin-down)
- Live URLs: `https://pulserecover-api.onrender.com`, `https://pulserecover-web.onrender.com`
- Last verified live state (2026-09-02 ~04:31 UTC): healthz ok; system/health → database ok (Neon), policy engine ok, gateway `razorpay_test`, worker ok (ticking); `APP_ENV=prod`, `SIMULATION_MODE=false`, `WORKER_ENABLED=true`

## Environment variables (names only — values never recorded)

Backend (`Settings` in `backend/app/config.py` + worker additions): `DATABASE_URL`, `APP_ENV`, `SIMULATION_MODE`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `RAZORPAY_BASE_URL`, `LLM_PROVIDER`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, `API_KEY`, `CORS_ORIGINS`, `POLICY_FILE`, `LOG_LEVEL`, `WORKER_ENABLED`, `WORKER_TICK_SECONDS`, `WORKER_RECONCILE_SECONDS`, `WORKER_NOTIFICATION_SENDER`
Frontend: `NEXT_PUBLIC_API_BASE_URL`, `NEXT_PUBLIC_API_KEY`

## Integrations available

- Razorpay Test Mode API (read: orders/payments/subscriptions/payment_links collections; write: orders, payment_links — subscriptions + direct payments APIs return 401, products not enabled on the audit account)
- Razorpay webhooks (HMAC-SHA256 verified intake; live secret alignment fixed 2026-09-02)
- LLM: none configured (`LLM_PROVIDER=none` → offline heuristic reasoner); OpenAI-compatible seam exists
- Neon Postgres; Render hosting; GitHub (public repo)

## Commands

- Backend tests: `cd backend && .venv/Scripts/python -m pytest -q` (968 tests at audit start, last full run: 968 passed)
- Frontend gates: `cd frontend && npx tsc --noEmit && npm run lint && npm run build`
- Migrations: `cd backend && .venv/Scripts/python -m alembic upgrade head`
- Contract export: `cd backend && .venv/Scripts/python scripts/export_openapi.py` (42 paths)
- Local run: `make backend` (uvicorn :8000), `cd frontend && npm run dev` (:3000)
- Deploy: push to `main` → Render auto-deploys both services

## Audit ground rules (inherited from the mission)

No production code modified during the audit. Facts over assumptions; code over docs; running behavior over static source. Secret values are never printed or recorded.
