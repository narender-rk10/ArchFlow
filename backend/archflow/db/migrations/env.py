"""Alembic environment: runs on the connection handed over by ``archflow.db.migrate``, or on ``DATABASE_URL`` /
``ARCHFLOW_DB`` when run from the command line (``alembic -c archflow/db/alembic.ini ...``)."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import context
from sqlalchemy import URL, create_engine

from archflow.db.models import Base

config = context.config


def _url() -> str | URL:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith(("postgres://", "postgresql://")):
        return "postgresql+psycopg://" + url.split("://", 1)[1]
    default = Path(__file__).resolve().parents[3] / "data" / "archflow.db"
    return URL.create("sqlite", database=os.environ.get("ARCHFLOW_DB", str(default)))


def run(connection) -> None:  # noqa: ANN001
    context.configure(connection=connection, target_metadata=Base.metadata,
                      render_as_batch=connection.dialect.name == "sqlite", compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    with create_engine(_url()).begin() as conn:
        run(conn)
