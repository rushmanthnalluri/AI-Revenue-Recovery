# ADR 0002: SQLite by default, Postgres via docker-compose

- **Decision:** Default `DATABASE_URL` is `sqlite:///./pulserecover.db`;
  Postgres 16 is available through `deploy/docker-compose.yml`. Models use only
  portable SQLAlchemy types.
- **Context:** Judges and developers should be able to run the system with zero
  infrastructure; the graded/demo path should also prove production-shaped
  Postgres operation. SQLAlchemy 2.x makes the dialect switch a config change —
  provided we never use Postgres-only column types.
- **Options:**
  1. Postgres only.
  2. SQLite only.
  3. Portable models, dialect chosen by env (SQLite default, PG in compose).
- **Chosen:** (3).
- **Why:** Zero-friction local run and tests (in-memory SQLite via StaticPool),
  while `docker compose up` exercises the real Postgres path with migrations.
  Portability rules: `sa.JSON` (not JSONB), enums as `native_enum=False`
  (VARCHAR + CHECK on both dialects), `TZDateTime` decorator guaranteeing
  tz-aware UTC on engines that don't store tz (SQLite), integer paise for
  money (no NUMERIC/float ambiguity), string PKs.
- **Tradeoffs:** We forgo Postgres-native features (JSONB operators, advisory
  locks, native enums); heavy concurrency would need the compose stack; SQLite
  DDL migration support is weaker (batch mode if we ever alter tables).
