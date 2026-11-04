"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.database.models import Base
from app.database.session import create_db_engine


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Create a temporary SQLite engine with all tables."""

    database_url = f"sqlite:///{(tmp_path / 'argus-test.db').as_posix()}"
    engine = create_db_engine(database_url)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    """Provide a session bound to the temporary test database."""

    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        try:
            yield session
        finally:
            session.rollback()
