"""Tests for expected schema objects, constraints, and enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.database.models import Base
from app.database.session import create_db_engine, init_db

EXPECTED_TABLES = {
    "email_accounts",
    "sync_checkpoints",
    "tracking_rules",
    "processed_emails",
    "status_events",
    "notifications",
}

EXPECTED_UNIQUE_CONSTRAINTS = {
    "email_accounts": "uq_email_accounts_provider_email",
    "sync_checkpoints": "uq_sync_checkpoints_account_id",
    "tracking_rules": "uq_tracking_rules_account_name",
    "processed_emails": "uq_processed_emails_account_message",
    "status_events": "uq_status_events_rule_message_status",
    "notifications": "uq_notifications_event_channel",
}

EXPECTED_INDEXES = {
    "email_accounts": {
        "ix_email_accounts_is_active",
        "ix_email_accounts_normalized_email",
    },
    "tracking_rules": {
        "ix_tracking_rules_account_state",
        "ix_tracking_rules_state",
    },
    "processed_emails": {
        "ix_processed_emails_account_received",
        "ix_processed_emails_account_internet_message",
    },
    "status_events": {
        "ix_status_events_rule_occurred",
        "ix_status_events_rule_internet_message",
    },
    "notifications": {"ix_notifications_state_created"},
}

EXPECTED_CHECK_CONSTRAINTS = {
    "email_accounts": {"ck_email_accounts_provider_name"},
    "sync_checkpoints": {
        "ck_sync_checkpoints_consecutive_failures_non_negative",
    },
    "tracking_rules": {"ck_tracking_rules_rule_state"},
    "status_events": {
        "ck_status_events_previous_status",
        "ck_status_events_status",
    },
    "notifications": {
        "ck_notifications_attempts_non_negative",
        "ck_notifications_channel",
        "ck_notifications_state",
    },
}

EXPECTED_FOREIGN_KEYS = {
    "sync_checkpoints": ("account_id", "email_accounts", "CASCADE"),
    "tracking_rules": ("account_id", "email_accounts", "RESTRICT"),
    "processed_emails": ("account_id", "email_accounts", "CASCADE"),
    "status_events": ("rule_id", "tracking_rules", "RESTRICT"),
    "notifications": ("status_event_id", "status_events", "RESTRICT"),
}


def test_init_db_creates_expected_tables(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'schema-test.db').as_posix()}"
    engine = create_db_engine(database_url)
    try:
        init_db(engine)
        table_names = set(inspect(engine).get_table_names())
        assert EXPECTED_TABLES <= table_names
    finally:
        engine.dispose()


def test_expected_unique_constraints_exist(engine: Engine) -> None:
    inspector = inspect(engine)

    for table_name, constraint_name in EXPECTED_UNIQUE_CONSTRAINTS.items():
        names = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table_name)
        }
        assert constraint_name in names, f"Missing {constraint_name} on {table_name}"


def test_expected_indexes_exist(engine: Engine) -> None:
    inspector = inspect(engine)

    for table_name, expected in EXPECTED_INDEXES.items():
        names = {index["name"] for index in inspector.get_indexes(table_name)}
        assert expected <= names, f"Missing indexes on {table_name}: {expected - names}"


def test_expected_check_constraints_exist(engine: Engine) -> None:
    inspector = inspect(engine)

    for table_name, expected in EXPECTED_CHECK_CONSTRAINTS.items():
        names = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table_name)
        }
        assert expected <= names, (
            f"Missing check constraints on {table_name}: {expected - names}"
        )


def test_foreign_keys_reference_expected_tables_and_actions(engine: Engine) -> None:
    inspector = inspect(engine)

    for table_name, (column_name, referred_table, ondelete) in EXPECTED_FOREIGN_KEYS.items():
        foreign_keys = inspector.get_foreign_keys(table_name)
        matching = [
            fk for fk in foreign_keys if fk["constrained_columns"] == [column_name]
        ]
        assert matching, f"Missing foreign key {column_name} on {table_name}"

        foreign_key = matching[0]
        assert foreign_key["referred_table"] == referred_table
        assert foreign_key.get("options", {}).get("ondelete") == ondelete


def test_foreign_keys_are_enforced(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO tracking_rules ("
                "account_id, name, required_keywords, state, created_at, updated_at"
                ") VALUES ("
                "9999, 'orphan', '[]', 'active', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00'"
                ")"
            )


def test_model_metadata_matches_expected_tables() -> None:
    assert EXPECTED_TABLES == set(Base.metadata.tables)
