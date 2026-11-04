"""Alembic migration smoke tests.

These tests require Alembic to be installed. Alembic is declared in
``requirements.txt``; when it is not present in the active environment the
module is skipped rather than failing unrelated foundation work.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine, inspect

try:
    from alembic import command
    from alembic.config import Config
except ImportError:  # pragma: no cover - depends on the environment
    pytest.skip("Alembic is not installed", allow_module_level=True)

from app.database.models import Base  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _sqlite_url(tmp_path: Path, name: str) -> str:
    return f"sqlite:///{(tmp_path / name).as_posix()}"


def test_baseline_migration_creates_schema(tmp_path: Path) -> None:
    database_url = _sqlite_url(tmp_path, "migrated.db")

    command.upgrade(_alembic_config(database_url), "head")

    engine = create_engine(database_url)
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert set(Base.metadata.tables) <= table_names
    assert "alembic_version" in table_names


def test_migrated_schema_matches_model_metadata(tmp_path: Path) -> None:
    database_url = _sqlite_url(tmp_path, "matching.db")
    command.upgrade(_alembic_config(database_url), "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)

        for table_name, table in Base.metadata.tables.items():
            database_columns = {
                column["name"] for column in inspector.get_columns(table_name)
            }
            assert database_columns == {column.name for column in table.columns}

            database_uniques = {
                constraint["name"]
                for constraint in inspector.get_unique_constraints(table_name)
            }
            model_uniques = {
                constraint.name
                for constraint in table.constraints
                if isinstance(constraint, UniqueConstraint)
            }
            assert model_uniques <= database_uniques

            database_checks = {
                constraint["name"]
                for constraint in inspector.get_check_constraints(table_name)
            }
            model_checks = {
                constraint.name
                for constraint in table.constraints
                if isinstance(constraint, CheckConstraint)
            }
            assert model_checks <= database_checks

            database_indexes = {
                index["name"] for index in inspector.get_indexes(table_name)
            }
            assert {index.name for index in table.indexes} <= database_indexes

            database_foreign_keys = {
                foreign_key["name"]
                for foreign_key in inspector.get_foreign_keys(table_name)
            }
            model_foreign_keys = {
                constraint.name
                for constraint in table.constraints
                if constraint.__class__.__name__ == "ForeignKeyConstraint"
            }
            assert model_foreign_keys <= database_foreign_keys
    finally:
        engine.dispose()


def test_baseline_migration_downgrades_cleanly(tmp_path: Path) -> None:
    database_url = _sqlite_url(tmp_path, "downgraded.db")
    config = _alembic_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(database_url)
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert not (set(Base.metadata.tables) & table_names)
