"""Schema migrations (Alembic), run when a ``Store`` opens.

* A new, empty database is created from the ORM models and stamped at the latest revision.
* Any other database is upgraded with ``alembic upgrade head``. The baseline revision (0001) also converges
  databases made by the SQL scripts of earlier ArchFlow versions (SQLite ``CREATE TABLE IF NOT EXISTS`` and the
  Postgres ``migrations/*.sql`` runner) onto the same schema, so existing deployments upgrade in place.

On Postgres a transaction-level advisory lock serializes concurrent server processes starting at once.

New schema change: edit ``models.py``, then from ``backend/``::

    uv run alembic -c archflow/db/alembic.ini revision --autogenerate -m "what changed"

review the generated file, and add a test case if it moves data.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text

from .models import Base

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
ADVISORY_LOCK = 0x41524346  # "ARCF"


def config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    return cfg


def upgrade(engine: Engine) -> None:
    cfg = config()
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK})
        cfg.attributes["connection"] = conn
        app_tables = set(inspect(conn).get_table_names()) & set(Base.metadata.tables)
        if not app_tables:
            Base.metadata.create_all(conn)
            command.stamp(cfg, "head")
        else:
            command.upgrade(cfg, "head")


def head() -> str | None:
    """The latest revision in ``migrations/versions``."""
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(config()).get_current_head()


def current(engine: Engine) -> str | None:
    with engine.connect() as conn:
        if not inspect(conn).has_table("alembic_version"):
            return None
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
