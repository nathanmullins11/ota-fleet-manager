"""Rollouts: database records and the commands that go with them.

RolloutStore holds the SQL. cli.py uses it to create rollouts and request rollbacks;
the orchestrator's main loop (RolloutRunner) is the only part that publishes commands
and the only part that judges device health and rolls back automatically.
Every decision is logged with its reason (NF-5) and recorded in rollout_events.
"""
import json
import logging
from typing import Optional

import paho.mqtt.client as mqtt

from db import Database
from messages import Event
from registry import FirmwareVersion
from rollout_logic import (ACTIVE_STATES, DeviceHealth, DeviceRow, HealthLimits, RolloutError,
                           TargetPlan, apply_event, build_command, check_can_roll_back,
                           check_can_start, classify_no_answer, evaluate_device,
                           health_decision, outcome)
from topics import command_topic

log = logging.getLogger("orchestrator.rollouts")

UPSERT_FIRMWARE = """
    INSERT INTO firmware_versions (version, checksum_sha256, package_path, notes)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (version) DO UPDATE
      SET checksum_sha256 = EXCLUDED.checksum_sha256,
          package_path    = EXCLUDED.package_path,
          notes           = EXCLUDED.notes
"""


class RolloutStore:
    def __init__(self, db: Database):
        self.db = db

    # ----------------------------------------------------------------- firmware

    def sync_firmware(self, versions: list[FirmwareVersion]) -> None:
        def work(conn):
            with conn.transaction():
                for v in versions:
                    conn.execute(UPSERT_FIRMWARE, (v.version, v.sha256, v.package_path, v.notes))
        self.db.run(work)

    def firmware(self) -> dict:
        """version -> (sha256, notes), for versions with a known checksum."""
        rows = self.db.run(lambda conn: conn.execute(
            "SELECT version, checksum_sha256, notes FROM firmware_versions "
            "WHERE checksum_sha256 IS NOT NULL").fetchall())
        return {v: (sha, notes) for v, sha, notes in rows}

    # ----------------------------------------------------------------- queries

    def devices(self) -> list[DeviceRow]:
        rows = self.db.run(lambda conn: conn.execute(
            "SELECT device_id, current_version, status FROM devices").fetchall())
        return [DeviceRow(*r) for r in rows]

    def rollout(self, rollout_id: int) -> Optional[dict]:
        row = self.db.run(lambda conn: conn.execute(
            "SELECT id, target_version, rollback_version, state, halt_reason, started_at, "
            "ended_at FROM rollouts WHERE id = %s", (rollout_id,)).fetchone())
        if row is None:
            return None
        keys = ("id", "target_version", "rollback_version", "state", "halt_reason",
                "started_at", "ended_at")
        return dict(zip(keys, row, strict=True))

    def rollouts(self, limit: int = 10) -> list[tuple]:
        return self.db.run(lambda conn: conn.execute(
            "SELECT r.id, r.target_version, r.rollback_version, r.state, r.started_at, "
            "       count(d.device_id) "
            "  FROM rollouts r LEFT JOIN rollout_devices d ON d.rollout_id = r.id "
            " GROUP BY r.id ORDER BY r.id DESC LIMIT %s", (limit,)).fetchall())

    def limits(self, rollout_id: int, defaults: HealthLimits) -> HealthLimits:
        """The health limits stored with a rollout; missing values (older rollouts) use
        the defaults."""
        row = self.db.run(lambda conn: conn.execute(
            "SELECT health_window_seconds, max_crashes, max_errors, max_failed_devices, "
            "       unresponsive_seconds FROM rollouts WHERE id = %s", (rollout_id,)).fetchone())
        if row is None:
            return defaults
        return defaults.with_overrides(
            window_seconds=row[0], max_crashes=row[1], max_errors=row[2],
            max_failed_devices=row[3], unresponsive_seconds=row[4])

    def device_verdicts(self, rollout_id: int) -> dict:
        """device_id -> (verdict, reason) for devices that have a verdict."""
        rows = self.db.run(lambda conn: conn.execute(
            "SELECT device_id, verdict, verdict_reason FROM rollout_devices "
            " WHERE rollout_id = %s AND verdict IS NOT NULL", (rollout_id,)).fetchall())
        return {d: (v, r) for d, v, r in rows}

    def device_states(self, rollout_id: int) -> dict:
        rows = self.db.run(lambda conn: conn.execute(
            "SELECT device_id, state FROM rollout_devices WHERE rollout_id = %s",
            (rollout_id,)).fetchall())
        return dict(rows)

    def device_details(self, rollout_id: int) -> list[tuple]:
        return self.db.run(lambda conn: conn.execute(
            "SELECT rd.device_id, rd.state, d.current_version, d.status, rd.verdict, "
            "       rd.verdict_reason, rd.updated_at "
            "  FROM rollout_devices rd JOIN devices d USING (device_id) "
            " WHERE rd.rollout_id = %s ORDER BY rd.device_id", (rollout_id,)).fetchall())

    def events(self, rollout_id: int) -> list[tuple]:
        return self.db.run(lambda conn: conn.execute(
            "SELECT created_at, event, reason FROM rollout_events "
            " WHERE rollout_id = %s ORDER BY id", (rollout_id,)).fetchall())

    # ----------------------------------------------------------------- requests (cli.py)

    def create_rollout(self, target_version: str, plan: TargetPlan, origin: str,
                       limits: Optional[HealthLimits] = None) -> int:
        """Record a pending rollout and the health limits it is judged by; the orchestrator
        sends the commands."""
        limits = limits or HealthLimits()
        def work(conn):
            with conn.transaction():
                # Serializes concurrent creates, so "one active rollout" holds.
                conn.execute("LOCK TABLE rollouts IN SHARE ROW EXCLUSIVE MODE")
                active = conn.execute(
                    "SELECT id, state FROM rollouts WHERE state = ANY(%s) ORDER BY id",
                    (list(ACTIVE_STATES),)).fetchall()
                check_can_start(active)
                rid = conn.execute(
                    "INSERT INTO rollouts (target_version, rollback_version, "
                    "  health_window_seconds, max_crashes, max_errors, max_failed_devices, "
                    "  unresponsive_seconds) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (target_version, plan.rollback_version, limits.window_seconds,
                     limits.max_crashes, limits.max_errors, limits.max_failed_devices,
                     limits.unresponsive_seconds)).fetchone()[0]
                for device_id in plan.to_command:
                    conn.execute("INSERT INTO rollout_devices (rollout_id, device_id) "
                                 "VALUES (%s, %s)", (rid, device_id))
                conn.execute(
                    "INSERT INTO rollout_events (rollout_id, event, stage, reason) "
                    "VALUES (%s, 'started', 0, %s)",
                    (rid, _start_reason(origin, target_version, plan, limits)))
                return rid
        return self.db.run(work)

    def request_rollback(self, rollout_id: int, reason: str) -> None:
        def work(conn):
            with conn.transaction():
                row = conn.execute("SELECT state FROM rollouts WHERE id = %s FOR UPDATE",
                                   (rollout_id,)).fetchone()
                if row is None:
                    raise RolloutError(f"rollout {rollout_id} does not exist")
                check_can_roll_back(row[0], rollout_id)
                # ended_at is cleared: a completed rollout that is rolled back is running again.
                conn.execute("UPDATE rollouts SET state = 'halted', halt_reason = %s, "
                             "ended_at = NULL WHERE id = %s", (reason, rollout_id))
                conn.execute("INSERT INTO rollout_events (rollout_id, event, reason) "
                             "VALUES (%s, 'halted', %s)", (rollout_id, reason))
        self.db.run(work)


def _start_reason(origin: str, target: str, plan: TargetPlan, limits: HealthLimits) -> str:
    parts = [f"{origin}: update to {target} for {len(plan.to_command)} device(s)",
             f"rollback version {plan.rollback_version}", f"health: {limits.describe()}"]
    if plan.skipped_offline:
        parts.append(f"skipped offline: {', '.join(plan.skipped_offline)}")
    if plan.already_on_target:
        parts.append(f"already on {target}: {', '.join(plan.already_on_target)}")
    return "; ".join(parts)


class RolloutRunner:
    """Called by the orchestrator: publishes commands and follows rollouts to the end.

    tick() runs on the main thread; on_event() on paho's callback thread. Each uses its
    own Database (connection).
    """

    def __init__(self, store: RolloutStore, client: mqtt.Client, public_url: str,
                 command_timeout_seconds: int, limits: Optional[HealthLimits] = None):
        self.store = store
        self.client = client
        self.public_url = public_url
        self.timeout = command_timeout_seconds
        self.default_limits = limits or HealthLimits()   # for rollouts that stored none

    # ----------------------------------------------------------------- main loop

    def tick(self) -> None:
        self._send_updates()
        self._expire_commands()
        self._evaluate_health()
        self._apply_health_rules()
        self._send_rollbacks()          # same tick as an automatic halt
        self._finish_rollouts()

    def _send_updates(self) -> None:
        def claim(conn):
            # Mark devices commanded and the rollout in_progress *before* publishing, so a
            # fast device's event never finds its row still pending.
            with conn.transaction():
                rows = conn.execute(
                    "UPDATE rollouts SET state = 'in_progress', started_at = now() "
                    " WHERE state = 'pending' RETURNING id, target_version").fetchall()
                claimed = []
                for rid, target in rows:
                    ids = [r[0] for r in conn.execute(
                        "UPDATE rollout_devices SET state = 'commanded', updated_at = now() "
                        " WHERE rollout_id = %s AND state = 'pending' RETURNING device_id",
                        (rid,)).fetchall()]
                    claimed.append((rid, target, sorted(ids)))
                return claimed

        for rid, target, device_ids in self.store.db.run(claim):
            sha = self._sha(target)
            log.info("Rollout %d: sending update to %s to %d device(s): %s",
                     rid, target, len(device_ids), ", ".join(device_ids))
            for device_id in device_ids:
                self._publish(rid, device_id, "update", target, sha)

    def _send_rollbacks(self) -> None:
        def claim(conn):
            with conn.transaction():
                rows = conn.execute(
                    "SELECT r.id, r.rollback_version, r.halt_reason FROM rollouts r "
                    " WHERE r.state = 'halted' AND NOT EXISTS (SELECT 1 FROM rollout_events e "
                    "   WHERE e.rollout_id = r.id AND e.event = 'rollback_started') "
                    " FOR UPDATE").fetchall()
                claimed = []
                for rid, version, reason in rows:
                    ids = [r[0] for r in conn.execute(
                        "UPDATE rollout_devices SET state = 'commanded', updated_at = now() "
                        " WHERE rollout_id = %s RETURNING device_id", (rid,)).fetchall()]
                    conn.execute(
                        "INSERT INTO rollout_events (rollout_id, event, reason) "
                        "VALUES (%s, 'rollback_started', %s)",
                        (rid, f"{reason}; rolling back {len(ids)} device(s) to {version}"))
                    claimed.append((rid, version, reason, sorted(ids)))
                return claimed

        for rid, version, reason, device_ids in self.store.db.run(claim):
            sha = self._sha(version)
            log.warning("Rollout %d: rolling back %d device(s) to %s (reason: %s): %s",
                        rid, len(device_ids), version, reason, ", ".join(device_ids))
            for device_id in device_ids:
                self._publish(rid, device_id, "rollback", version, sha)

    def _expire_commands(self) -> None:
        """Commanded devices that sent no event in time are failed. In the update phase they
        also get a verdict: heartbeats but no reply (no_reply, counts as a failed update) or
        silent (not_reached: offline, which never triggers a rollback)."""
        def work(conn):
            with conn.transaction():
                rows = conn.execute(
                    "SELECT rd.rollout_id, rd.device_id, r.state, "
                    "       (SELECT count(*) FROM heartbeats h WHERE h.device_id = rd.device_id "
                    "          AND h.received_at > rd.updated_at) "
                    "  FROM rollout_devices rd JOIN rollouts r ON r.id = rd.rollout_id "
                    " WHERE r.state IN ('in_progress', 'halted') AND rd.state = 'commanded' "
                    "   AND rd.updated_at < now() - make_interval(secs => %s) "
                    "   FOR UPDATE OF rd", (self.timeout,)).fetchall()
                expired = []
                for rid, device_id, r_state, beats in rows:
                    verdict = reason = None
                    if r_state == "in_progress":
                        verdict, reason = classify_no_answer(beats, self.timeout)
                    conn.execute(
                        "UPDATE rollout_devices SET state = 'failed', updated_at = now(), "
                        "       verdict = COALESCE(%s, verdict), "
                        "       verdict_reason = COALESCE(%s, verdict_reason), "
                        "       verdict_at = CASE WHEN %s::text IS NULL THEN verdict_at ELSE now() END "
                        " WHERE rollout_id = %s AND device_id = %s",
                        (verdict, reason, verdict, rid, device_id))
                    if verdict:
                        _record_verdict(conn, rid, device_id, verdict, reason)
                    expired.append((rid, device_id, verdict, reason))
                return expired

        for rid, device_id, verdict, reason in self.store.db.run(work):
            if verdict:
                log.warning("Rollout %d: %s %s: %s", rid, device_id, verdict, reason)
            else:
                log.warning("Rollout %d: %s failed (no response within %ss)",
                            rid, device_id, self.timeout)

    def _evaluate_health(self) -> None:
        """Give each updated device in its health window a verdict once it has one (FR-4)."""
        rows = self.store.db.run(lambda conn: conn.execute(
            "SELECT rd.rollout_id, rd.device_id, r.target_version, "
            "       EXTRACT(EPOCH FROM now() - rd.window_started_at), "
            "       COALESCE((SELECT sum(h.crash_count) FROM heartbeats h "
            "                  WHERE h.device_id = rd.device_id "
            "                    AND h.received_at > rd.window_started_at), 0), "
            "       COALESCE((SELECT sum(h.error_count) FROM heartbeats h "
            "                  WHERE h.device_id = rd.device_id "
            "                    AND h.received_at > rd.window_started_at), 0), "
            "       EXTRACT(EPOCH FROM now() - d.last_heartbeat) "
            "  FROM rollout_devices rd JOIN rollouts r ON r.id = rd.rollout_id "
            "  JOIN devices d ON d.device_id = rd.device_id "
            " WHERE r.state = 'in_progress' AND rd.state = 'updated' AND rd.verdict IS NULL "
            "   AND rd.window_started_at IS NOT NULL").fetchall())
        limits_cache = {}
        for rid, device_id, version, since_update, crashes, errors, silent in rows:
            if rid not in limits_cache:
                limits_cache[rid] = self.store.limits(rid, self.default_limits)
            health = DeviceHealth(device_id, version, float(since_update), int(crashes),
                                  int(errors), None if silent is None else float(silent))
            result = evaluate_device(health, limits_cache[rid])
            if result is None:
                continue
            verdict, reason = result

            def work(conn, rid=rid, device_id=device_id, verdict=verdict, reason=reason):
                with conn.transaction():
                    changed = conn.execute(
                        "UPDATE rollout_devices SET verdict = %s, verdict_reason = %s, "
                        "       verdict_at = now() "
                        " WHERE rollout_id = %s AND device_id = %s AND verdict IS NULL",
                        (verdict, reason, rid, device_id)).rowcount
                    if changed:
                        _record_verdict(conn, rid, device_id, verdict, reason)
                    return changed

            if self.store.db.run(work):
                (log.info if verdict == "healthy" else log.warning)(
                    "Rollout %d: %s %s: %s", rid, device_id, verdict, reason)

    def _apply_health_rules(self) -> None:
        """Halt and roll back a rollout whose verdicts break its limits (FR-5)."""
        rids = [r[0] for r in self.store.db.run(lambda conn: conn.execute(
            "SELECT id FROM rollouts WHERE state = 'in_progress'").fetchall())]
        for rid in rids:
            reason = health_decision(self.store.device_verdicts(rid),
                                     self.store.limits(rid, self.default_limits))
            if reason is None:
                continue
            try:
                self.store.request_rollback(rid, reason)
            except RolloutError as exc:          # finished or halted meanwhile
                log.info("Rollout %d: automatic rollback not needed (%s)", rid, exc)
                continue
            log.warning("Rollout %d halted: %s", rid, reason)

    def _finish_rollouts(self) -> None:
        rows = self.store.db.run(lambda conn: conn.execute(
            "SELECT id, state FROM rollouts r WHERE state = 'in_progress' "
            "   OR (state = 'halted' AND EXISTS (SELECT 1 FROM rollout_events e "
            "       WHERE e.rollout_id = r.id AND e.event = 'rollback_started'))").fetchall())
        for rid, state in rows:
            verdicts = {d: v for d, (v, _) in self.store.device_verdicts(rid).items()}
            result = outcome(state, self.store.device_states(rid),
                             verdicts if state == "in_progress" else None)
            if result is None:
                continue
            new_state, event, reason = result

            def work(conn, rid=rid, state=state, new_state=new_state, event=event, reason=reason):
                with conn.transaction():
                    changed = conn.execute(
                        "UPDATE rollouts SET state = %s, ended_at = now() "
                        " WHERE id = %s AND state = %s", (new_state, rid, state)).rowcount
                    if changed:
                        conn.execute("INSERT INTO rollout_events (rollout_id, event, reason) "
                                     "VALUES (%s, %s, %s)", (rid, event, reason))
                    return changed

            if self.store.db.run(work):
                log.info("Rollout %d %s: %s", rid, new_state, reason)

    # ----------------------------------------------------------------- helpers

    def _sha(self, version: str) -> str:
        entry = self.store.firmware().get(version)
        if entry is None:
            # Cannot happen through cli.py, which checks; guards hand-made rows.
            raise RolloutError(f"no checksum recorded for version {version}")
        return entry[0]

    def _publish(self, rid: int, device_id: str, command: str, version: str, sha: str) -> None:
        payload = build_command(command, rid, version, sha, self.public_url)
        info = self.client.publish(command_topic(device_id), json.dumps(payload), qos=1)
        if info.rc == mqtt.MQTT_ERR_SUCCESS:
            log.info("Rollout %d: %s %s sent to %s", rid, command, version, device_id)
        elif info.rc == mqtt.MQTT_ERR_NO_CONN:
            # QoS 1: paho keeps it and sends it when the connection is back.
            log.warning("Rollout %d: %s for %s queued (broker connection down)",
                        rid, command, device_id)
        else:
            log.error("Rollout %d: could not publish %s to %s (%s); marking it failed",
                      rid, command, device_id, mqtt.error_string(info.rc))
            self.store.db.run(lambda conn: _in_txn(conn, lambda: conn.execute(
                "UPDATE rollout_devices SET state = 'failed', updated_at = now() "
                " WHERE rollout_id = %s AND device_id = %s", (rid, device_id))))

    # ----------------------------------------------------------------- device events

    def on_event(self, ev: Event) -> None:
        """Apply a device event to its rollout_devices row (paho callback thread)."""
        def work(conn):
            with conn.transaction():
                row = conn.execute(
                    "SELECT r.state, r.target_version, r.rollback_version, rd.state "
                    "  FROM rollouts r JOIN rollout_devices rd ON rd.rollout_id = r.id "
                    " WHERE r.id = %s AND rd.device_id = %s FOR UPDATE OF rd",
                    (ev.rollout_id, ev.device_id)).fetchone()
                if row is None:
                    return None, "device is not part of this rollout"
                r_state, target, rollback, d_state = row
                new_state, ignored = apply_event(r_state, ev.event, ev.version, target, rollback)
                if ignored:
                    return None, ignored
                if new_state and new_state != d_state:
                    conn.execute(
                        "UPDATE rollout_devices SET state = %s, updated_at = now() "
                        " WHERE rollout_id = %s AND device_id = %s",
                        (new_state, ev.rollout_id, ev.device_id))
                    if new_state == "updated":
                        # The health window starts now (rollout_logic.evaluate_device).
                        conn.execute("UPDATE rollout_devices SET window_started_at = now() "
                                     " WHERE rollout_id = %s AND device_id = %s",
                                     (ev.rollout_id, ev.device_id))
                        conn.execute("UPDATE devices SET previous_version = %s, "
                                     "updated_at = now() WHERE device_id = %s",
                                     (rollback, ev.device_id))
                    elif new_state == "failed" and r_state == "in_progress":
                        reason = ev.reason or "update failed"
                        conn.execute(
                            "UPDATE rollout_devices SET verdict = 'failed_update', "
                            "       verdict_reason = %s, verdict_at = now() "
                            " WHERE rollout_id = %s AND device_id = %s",
                            (reason, ev.rollout_id, ev.device_id))
                        _record_verdict(conn, ev.rollout_id, ev.device_id, "failed_update",
                                        reason)
                return new_state, None

        new_state, ignored = self.store.db.run(work)
        suffix = f" ({ev.reason})" if ev.reason else ""
        if ignored:
            log.warning("Rollout %d: ignored %s from %s: %s",
                        ev.rollout_id, ev.event, ev.device_id, ignored)
        elif new_state == "failed":
            log.warning("Rollout %d: %s reported %s for %s%s",
                        ev.rollout_id, ev.device_id, ev.event, ev.version, suffix)
        else:
            log.info("Rollout %d: %s reported %s for %s%s",
                     ev.rollout_id, ev.device_id, ev.event, ev.version, suffix)


def _record_verdict(conn, rid: int, device_id: str, verdict: str, reason: str) -> None:
    """A device verdict in the decision log (NF-5)."""
    conn.execute("INSERT INTO rollout_events (rollout_id, event, stage, reason) "
                 "SELECT %s, 'device_verdict', current_stage, %s FROM rollouts WHERE id = %s",
                 (rid, f"{device_id} {verdict}: {reason}", rid))


def _in_txn(conn, fn):
    with conn.transaction():
        return fn()
