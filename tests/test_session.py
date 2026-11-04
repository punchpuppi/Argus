"""Tests for transactional session management and engine initialisation."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.database import session as session_module
from app.database.enums import ProviderName
from app.database.models import EmailAccount


def test_session_scope_commits(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(session_module, "_session_factory", factory)

    with session_module.session_scope() as session:
        session.add(
            EmailAccount(
                provider=ProviderName.GMAIL,
                email_address="commit@example.com",
            )
        )

    with Session(engine) as verification_session:
        account = verification_session.scalar(
            select(EmailAccount).where(
                EmailAccount.email_address == "commit@example.com"
            )
        )
        assert account is not None


def test_session_scope_rolls_back(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(session_module, "_session_factory", factory)

    with pytest.raises(RuntimeError, match="forced failure"):
        with session_module.session_scope() as session:
            session.add(
                EmailAccount(
                    provider=ProviderName.GMAIL,
                    email_address="rollback@example.com",
                )
            )
            raise RuntimeError("forced failure")

    with Session(engine) as verification_session:
        account = verification_session.scalar(
            select(EmailAccount).where(
                EmailAccount.email_address == "rollback@example.com"
            )
        )
        assert account is None


def test_session_scope_rolls_back_on_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(session_module, "_session_factory", factory)

    with pytest.raises(IntegrityError):
        with session_module.session_scope() as session:
            session.add_all(
                [
                    EmailAccount(
                        provider=ProviderName.GMAIL,
                        email_address="collision@example.com",
                    ),
                    EmailAccount(
                        provider=ProviderName.GMAIL,
                        email_address="COLLISION@example.com",
                    ),
                ]
            )

    with Session(engine) as verification_session:
        count = verification_session.scalar(
            select(func.count()).select_from(EmailAccount)
        )
        assert count == 0


def test_session_factory_creates_distinct_sessions(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(session_module, "_session_factory", factory)

    first = session_module.get_session_factory()()
    second = session_module.get_session_factory()()
    try:
        assert first is not second
    finally:
        first.close()
        second.close()


def test_get_engine_is_thread_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session_module.dispose_engine()
    database_url = f"sqlite:///{(tmp_path / 'engine-threads.db').as_posix()}"
    fake_settings = SimpleNamespace(
        resolved_database_url=database_url,
        database_echo=False,
    )

    monkeypatch.setattr(session_module, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(session_module, "_session_factory", None)

    barrier = threading.Barrier(8)

    def fetch_engine(_: int) -> Engine:
        barrier.wait()
        return session_module.get_engine()

    with ThreadPoolExecutor(max_workers=8) as pool:
        engines = list(pool.map(fetch_engine, range(8)))

    try:
        assert len({id(engine) for engine in engines}) == 1
    finally:
        session_module.dispose_engine()
