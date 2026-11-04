"""Synchronous SQLAlchemy engine and session management."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import Base
from config import get_settings

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_engine_lock = threading.Lock()
_session_factory_lock = threading.Lock()


def _apply_sqlite_pragmas(
    dbapi_connection: Any,
    _connection_record: Any,
) -> None:
    """Enable SQLite foreign keys, WAL mode, and a busy timeout per connection."""

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def create_db_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create an engine suitable for the configured database URL."""

    connect_args: dict[str, Any] = {}
    is_sqlite = database_url.startswith("sqlite")

    if is_sqlite:
        # The bot and provider layers may use worker threads. Each thread must
        # still use its own Session; this only allows connections to be used
        # across the SQLAlchemy pool.
        connect_args["check_same_thread"] = False

    engine = create_engine(
        database_url,
        echo=echo,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    if is_sqlite:
        event.listen(engine, "connect", _apply_sqlite_pragmas)

    return engine


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use.

    Initialisation is guarded by a lock because the Telegram layer and
    provider workers may call this from multiple threads at startup.
    """

    global _engine

    if _engine is None:
        with _engine_lock:
            if _engine is None:
                settings = get_settings()
                _engine = create_db_engine(
                    settings.resolved_database_url,
                    echo=settings.database_echo,
                )

    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide session factory.

    Sessions must not be shared between threads; callers should create one
    session per task/thread.
    """

    global _session_factory

    if _session_factory is None:
        with _session_factory_lock:
            if _session_factory is None:
                _session_factory = sessionmaker(
                    bind=get_engine(),
                    autoflush=False,
                    expire_on_commit=False,
                )

    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional session scope with commit/rollback handling."""

    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except BaseException:
        try:
            session.rollback()
        except Exception:  # pragma: no cover - defensive rollback failure
            logger.exception("Failed to roll back database session")
        raise
    finally:
        session.close()


def init_db(bind: Engine | None = None) -> None:
    """Create all tables that do not yet exist."""

    Base.metadata.create_all(bind=bind or get_engine())


def dispose_engine() -> None:
    """Dispose the cached engine and factory. Intended for tests and shutdown."""

    global _engine, _session_factory

    with _session_factory_lock:
        with _engine_lock:
            if _engine is not None:
                _engine.dispose()

            _engine = None
            _session_factory = None
