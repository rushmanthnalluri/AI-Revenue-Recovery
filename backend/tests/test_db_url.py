"""DATABASE_URL normalization: a bare `postgresql://` URL selects the psycopg
v3 driver the stack ships (demo-chaos F4) instead of crashing at boot on the
missing psycopg2 module."""

import sqlalchemy as sa

from app.db import _normalize_url


def test_bare_postgres_url_gets_psycopg_driver():
    assert (
        _normalize_url("postgresql://u:p@host:5432/db")
        == "postgresql+psycopg://u:p@host:5432/db"
    )


def test_render_style_postgres_url_gets_psycopg_driver():
    # Managed hosts (Render et al.) hand out `postgres://` without the -ql.
    assert (
        _normalize_url("postgres://u:p@host:5432/db")
        == "postgresql+psycopg://u:p@host:5432/db"
    )


def test_already_qualified_and_sqlite_urls_untouched():
    assert (
        _normalize_url("postgresql+psycopg://u:p@host/db")
        == "postgresql+psycopg://u:p@host/db"
    )
    assert _normalize_url("sqlite:///./pulserecover.db") == "sqlite:///./pulserecover.db"


def test_bare_postgres_url_builds_engine_without_psycopg2():
    # create_engine is lazy — no connection is attempted — but resolving the
    # dialect/driver happens eagerly, which is where the boot crash occurred.
    engine = sa.create_engine(_normalize_url("postgresql://u:p@localhost:5432/db"))
    assert engine.url.get_backend_name() == "postgresql"
    assert engine.url.get_driver_name() == "psycopg"
