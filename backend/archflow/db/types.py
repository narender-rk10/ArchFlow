"""Column types that behave the same on SQLite and Postgres.

* ``UTCDateTime``: ``TIMESTAMPTZ`` on Postgres, an ISO-8601 string on SQLite. Python sees ISO strings either way
  (``2026-10-11T09:30:00+00:00``, to the second), which is what the API returns. Values are written with
  microseconds so rows created in the same second still sort in creation order.
* ``JSONType``: ``JSONB`` on Postgres, JSON text on SQLite; SQL ``NULL`` for ``None``.
* ``StateJSON``: a ``ProjectState`` stored as JSON.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import JSON, String
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.types import TypeDecorator

from ..models import ProjectState


_last = datetime.min.replace(tzinfo=timezone.utc)
_last_lock = threading.Lock()


def now() -> str:
    """The current UTC time as stored: ISO-8601 with microseconds, strictly increasing within the process.

    The clock can tick far more coarsely than a microsecond (on Windows several milliseconds apart), so two rows
    written back to back would otherwise tie and sort by their random ids.
    """
    global _last
    with _last_lock:
        _last = max(datetime.now(timezone.utc), _last + timedelta(microseconds=1))
        return _last.isoformat(timespec="microseconds")


def to_seconds(value: str | datetime) -> str:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class UTCDateTime(TypeDecorator):
    impl = String(40)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(TIMESTAMP(timezone=True))
        return dialect.type_descriptor(String(40))

    def process_bind_param(self, value: Any, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds") if isinstance(value, datetime) else value

    def process_result_value(self, value: Any, dialect):
        return None if value is None else to_seconds(value)


JSONType = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


class StateJSON(TypeDecorator):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value: Any, dialect):
        if value is None:
            return None
        return value.model_dump(mode="json") if isinstance(value, ProjectState) else value

    def process_result_value(self, value: Any, dialect):
        if value is None:
            return None
        return ProjectState.model_validate_json(value) if isinstance(value, (str, bytes)) else ProjectState.model_validate(value)
