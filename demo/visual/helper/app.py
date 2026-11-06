"""Visual demo helper. Demo tooling only: it is never part of the control plane's own
compose file, and is added with demo/visual/helper/compose.yml.

Runs next to the control plane and gives the visual demo (on the presenter's computer)
what it cannot reach directly, because PostgreSQL only listens on the control plane host:

  GET  /health                        liveness (no password)
  GET  /api/state                     services, fleet, crashes, firmware, latest rollout,
                                      orchestrator decisions (from its log file)
  POST /api/rollouts                  start a rollout       {"version": "2.0", "devices": null}
  POST /api/rollouts/latest/rollback  roll back the latest rollout   {"reason": "..."}

Rollouts go through the orchestrator's own code (rollout_logic, RolloutStore), exactly as
cli.py does: the helper records the request and the orchestrator sends the commands.
Every /api call needs the X-Demo-Password header (VISUAL_DEMO_PASSWORD).
"""
import os
import re
import secrets
import socket
import threading
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from config import load_config
from db import Database
from registry import RegistryError, fetch_versions
from rollout_logic import RolloutError, plan_targets
from rollouts import RolloutStore

PASSWORD = os.environ.get("VISUAL_DEMO_PASSWORD", "")
LOG_FILE = Path(os.environ.get("ORCHESTRATOR_LOG_FILE", "/var/log/orchestrator/orchestrator.log"))
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

# Orchestrator log lines worth showing: decisions, rejections, offline devices, connections.
# ("Event from" lines are left out: each event also appears as a "Rollout N: ..." line.)
DECISION_RE = re.compile(r"Rejected|marked offline|Rollout|Firmware synced|Connected to broker|"
                         r"Starting orchestrator|Lost connection")
LOG_LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (\w+) ([\w.]+): (.*)$")

cfg = load_config()
db = Database(cfg, application_name="visual-demo-helper")
store = RolloutStore(db)
db_lock = threading.Lock()      # one connection; FastAPI runs handlers on a thread pool


def require_password(x_demo_password: str = Header(default="")) -> None:
    if not PASSWORD or not secrets.compare_digest(x_demo_password, PASSWORD):
        raise HTTPException(status_code=401, detail="wrong or missing X-Demo-Password")


app = FastAPI(title="OTA visual demo helper")
api = APIRouter(prefix="/api", dependencies=[Depends(require_password)])


@app.get("/health")
def health():
    return {"status": "ok"}


# --------------------------------------------------------------------------- state

def _query(sql: str, params=()) -> list:
    with db_lock:
        return db.run(lambda conn: conn.execute(sql, params).fetchall())


def _tcp_ok(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


def services() -> dict:
    try:
        orchestrator = bool(_query(
            "SELECT 1 FROM pg_stat_activity WHERE application_name = 'orchestrator' LIMIT 1"))
        postgres = True
    except Exception:
        orchestrator = postgres = False
    return {
        "mosquitto": _tcp_ok(MQTT_HOST, MQTT_PORT),
        "postgres": postgres,
        "software-registry": _http_ok(f"{cfg.registry_url}/health"),
        "orchestrator": orchestrator,
    }


def decisions(limit: int = 60) -> list[dict]:
    """The last orchestrator log lines worth showing, oldest first."""
    try:
        with LOG_FILE.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        m = LOG_LINE_RE.match(line)
        if m and DECISION_RE.search(m[4]):
            out.append({"time": m[1], "level": m[2], "source": m[3], "text": m[4]})
    return out[-limit:]


def _iso(ts) -> Optional[str]:
    return ts.isoformat() if ts else None


@api.get("/state")
def state():
    result = {"services": services(), "decisions": decisions(), "error": None}
    try:
        result["fleet"] = [
            {"device_id": d, "version": v, "previous_version": pv, "status": s,
             "errors": e, "crashes": c, "seconds_since_heartbeat": age}
            for d, v, pv, s, e, c, age in _query(
                "SELECT device_id, current_version, previous_version, status, error_count, "
                "       crash_count, EXTRACT(EPOCH FROM (now() - last_heartbeat))::INTEGER "
                "  FROM devices ORDER BY device_id")]
        result["crashes"] = [
            {"time": _iso(t), "device_id": d, "version": v, "errors": e, "crashes": c}
            for t, d, v, e, c in _query(
                "SELECT received_at, device_id, reported_version, error_count, crash_count "
                "  FROM heartbeats WHERE crash_count > 0 ORDER BY id DESC LIMIT 5")]
        result["firmware"] = [
            {"version": v, "sha256": sha, "notes": n}
            for v, sha, n in _query(
                "SELECT version, checksum_sha256, notes FROM firmware_versions "
                " WHERE checksum_sha256 IS NOT NULL ORDER BY string_to_array(version, '.')::int[]")]
        result["rollout"] = latest_rollout()
    except Exception as exc:          # database down: still return services and decisions
        result["error"] = f"database: {exc}"
    return result


def latest_rollout() -> Optional[dict]:
    with db_lock:
        rows = store.rollouts(limit=1)
        if not rows:
            return None
        r = store.rollout(rows[0][0])
        devices = store.device_details(r["id"])
        events = store.events(r["id"])
    return {
        "id": r["id"], "target_version": r["target_version"],
        "rollback_version": r["rollback_version"], "state": r["state"],
        "halt_reason": r["halt_reason"], "started_at": _iso(r["started_at"]),
        "ended_at": _iso(r["ended_at"]),
        "devices": [{"device_id": d, "state": s, "updated_at": _iso(t)}
                    for d, s, _v, _st, t in devices],
        "events": [{"time": _iso(t), "event": e, "reason": reason} for t, e, reason in events],
    }


# --------------------------------------------------------------------------- actions

class RolloutRequest(BaseModel):
    version: str
    devices: Optional[list[str]] = None     # None: every online device
    rollback_to: Optional[str] = None


class RollbackRequest(BaseModel):
    reason: str = "operator: rollback requested from the visual demo"


@api.post("/rollouts")
def start_rollout(req: RolloutRequest):
    try:
        versions = fetch_versions(cfg.registry_url)
        with db_lock:
            store.sync_firmware(versions)
            firmware = store.firmware()
            if req.version not in firmware:
                raise RolloutError(f"version {req.version} is not in the registry")
            plan = plan_targets(store.devices(), req.version, req.devices, req.rollback_to)
            rid = store.create_rollout(req.version, plan, origin="manual (visual demo)")
    except (RolloutError, RegistryError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"id": rid, "target_version": req.version, "rollback_version": plan.rollback_version,
            "commanding": plan.to_command, "skipped_offline": plan.skipped_offline,
            "already_on_target": plan.already_on_target}


@api.post("/rollouts/latest/rollback")
def rollback_latest(req: RollbackRequest):
    try:
        with db_lock:
            rows = store.rollouts(limit=1)
            if not rows:
                raise RolloutError("there is no rollout to roll back")
            rid = rows[0][0]
            store.request_rollback(rid, req.reason)
            r = store.rollout(rid)
    except RolloutError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"id": rid, "rollback_version": r["rollback_version"]}


app.include_router(api)
