"""Alembic environment configuration.

Reads database URL from RUN_STATE_DB_URL environment variable.
Uses synchronous SQLAlchemy engine for migrations (not asyncpg).
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Alembic Config object — provides access to .ini values.
config = context.config

# Environment variable takes priority, then programmatic callers,
# then alembic.ini default.
db_url = (
    os.environ.get("RUN_STATE_DB_URL")
    or config.get_main_option("sqlalchemy.url")
    or "postgresql://localhost/cloud_agents"
)
# Ensure sync driver (alembic uses psycopg2, not asyncpg)
if "+asyncpg" in db_url:
    db_url = db_url.replace("+asyncpg", "")
config.set_main_option("sqlalchemy.url", db_url)

# Set up loggers from the config file.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No SQLAlchemy ORM models — we use raw SQL migrations.
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL (no Engine needed).
    Calls to context.execute() emit SQL to the script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates a synchronous Engine and runs migrations within
    a transaction.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
