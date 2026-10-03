"""Tests for versioned schema migrations, metadata tracking, and persistence settings."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import json
from pathlib import Path
import sqlite3
import pytest

from persistence import (
    ActionDetails,
    ActionEventEnvelope,
    ActionStatus,
    ActionType,
    EventStore,
    InterceptionMetadata,
    AuditorVerdict,
)
from persistence.schema import CURRENT_SCHEMA_VERSION, migrate_audit_schema
from persistence.settings import PersistenceSettings


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


def _sample_event(event_id="event-1", **kwargs):
    return ActionEventEnvelope(
        event_id=event_id,
        trace_id="trace-1",
        session_id="session-1",
        agent_id="agent-1",
        action_type=ActionType.TOOL_CALL,
        status=ActionStatus.BLOCKED,
        action_details=ActionDetails(name="create_client", parameters={"application_id": "APP-0001"}),
        interception_metadata=InterceptionMetadata(verdict=AuditorVerdict.BLOCKED, policy_version="policy-1"),
        **kwargs,
    )


def test_future_schema_is_rejected(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as con:
        con.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="schema"):
        asyncio.run(EventStore(path).initialize())
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 999


def test_settings_validation():
    # Valid default settings
    settings = PersistenceSettings()
    assert settings.outbox_maxsize == 10000
    assert settings.max_retries == 3
    assert settings.timeout_seconds == 5.0

    # Policy parsing
    custom = PersistenceSettings.from_policy({"outbox_maxsize": 5000, "max_retries": 5})
    assert custom.outbox_maxsize == 5000
    assert custom.max_retries == 5
    assert custom.timeout_seconds == 5.0

    # Rejects boolean for numeric fields
    with pytest.raises(ValueError, match="integer between"):
        PersistenceSettings(outbox_maxsize=True)
    with pytest.raises(ValueError, match="finite number between"):
        PersistenceSettings(timeout_seconds=False)

    # Rejects negative or out of bounds
    with pytest.raises(ValueError):
        PersistenceSettings(outbox_maxsize=-1)
    with pytest.raises(ValueError):
        PersistenceSettings(timeout_seconds=-0.5)

    # Rejects unknown keys
    with pytest.raises(ValueError, match="Unknown persistence policy"):
        PersistenceSettings.from_policy({"unknown_key": 42})


@async_test
async def test_store_metadata_initialization(tmp_path):
    db_path = tmp_path / "metadata.db"
    store = EventStore(db_path)
    await store.initialize()

    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        assert con.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        meta = con.execute("SELECT * FROM store_metadata WHERE singleton = 1").fetchone()
        assert meta is not None
        assert len(meta["epoch"]) == 32
        assert meta["next_offset"] == 1
        settings_dict = json.loads(meta["settings_json"])
        assert settings_dict["outbox_maxsize"] == 10000

    await store.close()


@async_test
async def test_monotonic_ingest_offset_allocation(tmp_path):
    db_path = tmp_path / "offset.db"
    store = EventStore(db_path)
    await store.initialize()

    ev1 = _sample_event("event-1")
    ev2 = _sample_event("event-2")
    ev3 = _sample_event("event-3")

    await store.insert_event(ev1)
    await store.insert_event(ev2)
    await store.insert_event(ev3)

    with sqlite3.connect(db_path) as con:
        offsets = con.execute("SELECT event_id, ingest_offset FROM events ORDER BY ingest_offset ASC").fetchall()
        assert len(offsets) == 3
        assert offsets[0] == ("event-1", 1)
        assert offsets[1] == ("event-2", 2)
        assert offsets[2] == ("event-3", 3)

        next_offset = con.execute("SELECT next_offset FROM store_metadata WHERE singleton = 1").fetchone()[0]
        assert next_offset == 4

    await store.close()


@async_test
async def test_conflicting_simultaneous_writer_settings_rejected(tmp_path):
    db_path = tmp_path / "conflict.db"
    s1 = PersistenceSettings(outbox_maxsize=5000)
    store1 = EventStore(db_path, settings=s1)
    await store1.initialize()
    await store1.close()

    s2 = PersistenceSettings(outbox_maxsize=10000)
    store2 = EventStore(db_path, settings=s2)
    with pytest.raises(ValueError, match="Conflicting simultaneous writer settings"):
        await store2.initialize()


def test_migration_rollback_on_failure(tmp_path):
    db_path = tmp_path / "rollback.db"
    with sqlite3.connect(db_path) as con:
        con.execute("PRAGMA user_version = 1")
        con.commit()

    # Corrupt store_metadata to force failure
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE store_metadata (singleton INT, invalid_schema TEXT)")
        con.commit()

    with pytest.raises(Exception):
        with sqlite3.connect(db_path) as con:
            migrate_audit_schema(con, PersistenceSettings())

    with sqlite3.connect(db_path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1


@async_test
async def test_two_connections_racing_initialization(tmp_path):
    db_path = tmp_path / "race.db"
    store1 = EventStore(db_path)
    store2 = EventStore(db_path)

    await asyncio.gather(store1.initialize(), store2.initialize())

    with sqlite3.connect(db_path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        count = con.execute("SELECT COUNT(*) FROM store_metadata").fetchone()[0]
        assert count == 1

    await store1.close()
    await store2.close()
