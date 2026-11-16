"""The Grafana dashboard (deploy/grafana/) against a real PostgreSQL.

Every query in the dashboard runs as the read-only dashboard login, so a migration that
renames a column a panel uses fails here instead of showing "No data" in a demo.
Needs OTA_TEST_DB (see tests/conftest.py) for the database tests.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.component

GRAFANA = Path(__file__).resolve().parents[2] / "deploy" / "grafana"
DASHBOARD = GRAFANA / "dashboards" / "fleet.json"
DATASOURCE = GRAFANA / "provisioning" / "datasources" / "postgres.yml"
READER, PASSWORD = "dashboard_reader", "test-reader-password"


def dashboard() -> dict:
    return json.loads(DASHBOARD.read_text())


def queries() -> dict:
    """Panel title (or variable name) -> SQL."""
    found = {}
    for panel in dashboard()["panels"]:
        for t in panel.get("targets", []):
            found[panel["title"]] = t["rawSql"]
    for var in dashboard()["templating"]["list"]:
        found[f"${var['name']}"] = var["query"]
    return found


def runnable(sql: str) -> str:
    """Replace Grafana's macros and variables with plain SQL, as Grafana does before running."""
    sql = re.sub(r"\$__timeGroupAlias\((\w+),\s*'[^']*'\)", r"date_trunc('second', \1) AS time", sql)
    sql = re.sub(r"\$__timeFilter\((\w+)\)", r"\1 > now() - interval '1 hour'", sql)
    sql = sql.replace("$rollout", "0")
    assert "$" not in sql, f"unhandled Grafana macro or variable in: {sql}"
    return sql


# --------------------------------------------------------------------------- the files

def test_every_panel_uses_the_provisioned_datasource():
    uid = re.search(r"uid:\s*(\S+)", DATASOURCE.read_text()).group(1)
    for panel in dashboard()["panels"]:
        if panel["type"] == "row":
            continue
        assert panel["datasource"]["uid"] == uid, panel["title"]
        assert all(t["datasource"]["uid"] == uid for t in panel["targets"]), panel["title"]


def test_panels_fit_the_grid_and_have_unique_ids():
    panels = dashboard()["panels"]
    assert len({p["id"] for p in panels}) == len(panels)
    for p in panels:
        assert p["gridPos"]["x"] + p["gridPos"]["w"] <= 24, p["title"]


def test_dashboard_is_live_and_view_only():
    d = dashboard()
    assert d["refresh"] == "5s" and d["editable"] is False


# --------------------------------------------------------------------------- the database

@pytest.fixture
def reader(orch_db, pg_dsn, pg):
    """The read-only login, created the way the orchestrator does at start-up (twice:
    it runs on every start), and a connection made with it."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    orch_db.ensure_dashboard_reader(READER, PASSWORD)
    orch_db.ensure_dashboard_reader(READER, PASSWORD)
    parts = conninfo_to_dict(pg_dsn)
    parts.update(user=READER, password=PASSWORD)
    with psycopg.connect(make_conninfo(**parts), autocommit=True) as conn:
        yield conn


@pytest.fixture
def sample_data(pg):
    """A small fleet with a heartbeat history and an automatically rolled-back rollout."""
    pg.execute("INSERT INTO firmware_versions (version, checksum_sha256) VALUES "
               "('1.0', %s), ('2.1', %s)", ("1" * 64, "2" * 64))
    pg.execute("INSERT INTO devices (device_id, current_version, status, crash_count, "
               "last_heartbeat) VALUES ('device-001', '1.0', 'healthy', 0, now()), "
               "('device-002', '2.1', 'unhealthy', 1, now()), "
               "('device-003', '1.0', 'offline', 0, now() - interval '5 minutes')")
    pg.execute("UPDATE devices SET cpu_percent = 0.5, memory_mb = 24")
    pg.execute("INSERT INTO heartbeats (device_id, reported_version, status, error_count, "
               "crash_count, cpu_percent, memory_mb, reported_at) "
               "VALUES ('device-002', '2.1', 'unhealthy', 25, 1, 0.5, 24, %s)",
               (datetime.now(timezone.utc),))
    pg.execute("INSERT INTO control_plane_stats (service, cpu_percent, memory_mb) VALUES "
               "('orchestrator', 0.3, 40), ('postgres', 0.1, 70)")
    pg.execute("INSERT INTO rejected_messages (device_id, topic, kind, reason) VALUES "
               "('device-002', 'ota/devices/device-002/heartbeat', 'invalid_message', 'bad JSON'), "
               "('device-002', 'ota/devices/device-002/events', 'refused_command', 'forged')")
    pg.execute("INSERT INTO rollouts (target_version, rollback_version, state, started_at, "
               "ended_at, halt_reason) VALUES ('2.1', '1.0', 'rolled_back', "
               "now() - interval '1 minute', now(), 'automatic: device-002 unhealthy')")
    pg.execute("INSERT INTO rollout_devices (rollout_id, device_id, state, verdict, "
               "verdict_reason) VALUES (1, 'device-002', 'rolled_back', 'unhealthy', 'crashed')")
    for event in ("started", "device_verdict", "halted", "rollback_started", "rollback_completed"):
        pg.execute("INSERT INTO rollout_events (rollout_id, event, reason) VALUES (1, %s, 'x')",
                   (event,))


@pytest.mark.parametrize("title", sorted(queries()))
def test_every_dashboard_query_runs_as_the_reader(reader, sample_data, title):
    rows = reader.execute(runnable(queries()[title])).fetchall()
    assert rows, f"{title}: no rows from sample data"


def test_reader_sees_the_latest_rollout_by_default(reader, sample_data):
    rows = reader.execute(runnable(queries()["Rollout"])).fetchall()
    assert rows[0][:4] == ("#1", "ROLLED_BACK", "2.1", "1.0")


def test_reader_cannot_change_anything(reader, sample_data):
    import psycopg

    for statement in ("DELETE FROM heartbeats",
                      "UPDATE devices SET status = 'healthy'",
                      "INSERT INTO rollout_events (rollout_id, event) VALUES (1, 'halted')",
                      "CREATE TABLE sneaky (x int)"):
        with pytest.raises(psycopg.Error):
            reader.execute(statement)


def test_reader_can_read_tables_added_later(orch_db, reader, pg):
    pg.execute("CREATE TABLE added_by_a_migration (x int)")   # created by the owner
    assert reader.execute("SELECT count(*) FROM added_by_a_migration").fetchone() == (0,)
    pg.execute("DROP TABLE added_by_a_migration")
