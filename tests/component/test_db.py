"""Orchestrator device registry writes against a real PostgreSQL (db.py: Database).

Needs OTA_TEST_DB (see tests/conftest.py).
"""
from datetime import UTC, datetime

import pytest

from conftest import load_service_module

pytestmark = pytest.mark.component


def heartbeat(version="1.0", status="healthy", errors=0, crashes=0, device="device-001"):
    msgs = load_service_module("orch", "messages")
    return msgs.Heartbeat(device_id=device, version=version, status=status, error_count=errors,
                          crash_count=crashes, timestamp=datetime.now(UTC))


def test_first_heartbeat_registers_the_device(orch_db, pg):
    orch_db.record_heartbeat(heartbeat())
    row = pg.execute("SELECT current_version, status, error_count, crash_count, "
                     "last_heartbeat IS NOT NULL FROM devices WHERE device_id = 'device-001'"
                     ).fetchone()
    assert row == ("1.0", "healthy", 0, 0, True)


def test_later_heartbeats_update_the_device_and_keep_history(orch_db, pg):
    orch_db.record_heartbeat(heartbeat())
    orch_db.record_heartbeat(heartbeat("2.1", "unhealthy", errors=20, crashes=1))
    assert pg.execute("SELECT current_version, status, error_count, crash_count FROM devices"
                      ).fetchall() == [("2.1", "unhealthy", 20, 1)]
    assert pg.execute("SELECT reported_version, error_count, crash_count FROM heartbeats "
                      "ORDER BY id").fetchall() == [("1.0", 0, 0), ("2.1", 20, 1)]


def test_fleet_overview_shows_crashes(orch_db, pg):
    orch_db.record_heartbeat(heartbeat("2.1", "unhealthy", errors=5, crashes=1))
    assert pg.execute("SELECT device_id, current_version, crash_count FROM fleet_overview"
                      ).fetchall() == [("device-001", "2.1", 1)]


def test_silent_devices_are_marked_offline_once(orch_db, pg):
    orch_db.record_heartbeat(heartbeat(device="device-001"))
    orch_db.record_heartbeat(heartbeat(device="device-002"))
    pg.execute("UPDATE devices SET last_heartbeat = now() - interval '31 seconds' "
               "WHERE device_id = 'device-001'")
    assert orch_db.mark_stale_offline(30) == ["device-001"]
    assert orch_db.mark_stale_offline(30) == []           # already offline: not repeated
    assert dict(pg.execute("SELECT device_id, status FROM devices").fetchall()) == \
        {"device-001": "offline", "device-002": "healthy"}


def test_a_heartbeat_brings_an_offline_device_back(orch_db, pg):
    orch_db.record_heartbeat(heartbeat())
    pg.execute("UPDATE devices SET status = 'offline'")
    orch_db.record_heartbeat(heartbeat())
    assert pg.execute("SELECT status FROM devices").fetchone()[0] == "healthy"


def test_reconnects_after_the_connection_is_lost(orch_db, pg):
    """A database restart drops the connection; the next write reconnects by itself."""
    orch_db.record_heartbeat(heartbeat())
    pg.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
               "WHERE pid <> pg_backend_pid() AND datname = current_database()")
    orch_db.record_heartbeat(heartbeat("2.0"))
    assert pg.execute("SELECT current_version FROM devices").fetchone()[0] == "2.0"
