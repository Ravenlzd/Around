"""
Alembic environment. Uses the app's own settings (app/config.py) for the
database URL rather than duplicating it in alembic.ini, so there's one
source of truth for DATABASE_URL.

NOTE on how this project actually applies schema.sql: the docker-compose
path mounts schema.sql into Postgres's docker-entrypoint-initdb.d, which
Postgres runs automatically the FIRST time the data volume is created —
that's the fastest path for local dev (see README "Run locally").
Alembic is the path for everything after that first run: an existing
database (managed Postgres, a teammate's already-initialized local db,
staging/production), and for every schema change from here on. The
single migration in versions/ mirrors schema.sql's CREATE TABLE
statements so `alembic upgrade head` produces the same schema as the
init-script path; going forward, new schema changes should be added as
new Alembic revisions rather than edits to schema.sql alone, so both
paths stay in sync. schema.sql remains the readable source of truth for
"what does the schema look like right now."
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.config import settings
from app.database import Base
from app import models  # noqa: F401 — ensures all models are registered on Base.metadata

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
