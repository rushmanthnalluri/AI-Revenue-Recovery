"""Alembic environment. Uses app.config.settings.DATABASE_URL and the full
model metadata from app.models (importing the package registers all tables)."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.db import Base, _normalize_url
import app.models  # noqa: F401  (registers all tables on Base.metadata)

config = context.config
# Normalize like the app runtime (demo-chaos F4): bare postgresql:// and
# postgres:// URLs (Render/Neon style) must select the shipped psycopg v3
# driver, not the absent psycopg2.
config.set_main_option("sqlalchemy.url", _normalize_url(settings.DATABASE_URL))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
