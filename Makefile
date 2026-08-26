# PulseRecover — root Makefile (Git Bash friendly).
# Windows: venv interpreters live under .venv/Scripts.

PY := backend/.venv/Scripts/python
PIP := backend/.venv/Scripts/python -m pip

.PHONY: setup backend test run migrate export-openapi compose-up compose-down

setup:
	"/c/Users/rushm/AppData/Local/Programs/Python/Python314/python.exe" -m venv backend/.venv
	$(PIP) install --upgrade pip
	$(PIP) install -r backend/requirements.txt

backend:
	cd backend && .venv/Scripts/python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	cd backend && .venv/Scripts/python -m pytest -q

migrate:
	cd backend && .venv/Scripts/python -m alembic upgrade head

export-openapi:
	cd backend && .venv/Scripts/python scripts/export_openapi.py

compose-up:
	docker compose -f deploy/docker-compose.yml up --build

compose-down:
	docker compose -f deploy/docker-compose.yml down -v

run: compose-up
