"""Visual demo server: runs on the presenter's computer, serves the page, and does the work
behind its buttons. Started by demo/visual/run.sh (in a container, with the repository
mounted at /repo and the Docker socket, to manage the local device containers).

  GET  /                           the page (demo/visual/web)
  GET  /api/state                  everything the page shows, polled every second
  POST /api/steps/{n}/run          run guided step n (0-based); /api/steps/reset
  POST /api/devices/start-fleet    start device-001..003
  POST /api/devices/add            start one more device
  POST /api/devices/{id}/stop|start|crash|bad-message
  POST /api/firmware/check         verify 2.0 and tampered variants
  POST /api/rollouts               {"version": "2.0"}   (through the control plane helper)
  POST /api/rollouts/rollback      roll back the latest rollout
  POST /api/rollouts/reply         {"outcome": "succeed" | "fail_one"}  simulated device replies
"""
import importlib.util
import os
import re
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from fleet import Fleet, Mqtt                       # noqa: E402
from helper_client import Helper, HelperError       # noqa: E402
from steps import BAD_MESSAGES, StepFailed, StepRunner, simulate_replies  # noqa: E402

REPO = Path(os.environ.get("REPO", "/repo"))
WEB = REPO / "demo" / "visual" / "web"
DEVICE_ID_RE = re.compile(r"device-\d{3,}", re.ASCII)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Demo:
    def __init__(self):
        self.mode = os.environ.get("MODE", "local")
        self.mqtt_host = os.environ["MQTT_HOST"]
        self.registry_url = os.environ["REGISTRY_URL"].rstrip("/")
        mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
        self.fleet = Fleet(os.environ.get("DEVICE_IMAGE", "ota-demo-device-agent"),
                           self.mqtt_host, mqtt_port)
        self.mqtt = Mqtt(self.mqtt_host, mqtt_port)
        self.helper = Helper(os.environ["HELPER_URL"], os.environ["HELPER_PASSWORD"])
        self.steps = StepRunner(self)
        self.firmware_results = None
        self.firmware_lock = threading.Lock()
        self._firmware_check = _load("firmware_check", REPO / "demo" / "lib" / "firmware_check.py")

    def check_firmware(self, version: str = "2.0") -> list:
        with self.firmware_lock:
            results = self._firmware_check.run_cases(self.registry_url, version)
            self.firmware_results = {"version": version, "cases": results}
            return results


demo = Demo()
app = FastAPI(title="OTA visual demo")


def _device(device_id: str) -> str:
    if not DEVICE_ID_RE.fullmatch(device_id):
        raise HTTPException(400, "invalid device ID")
    return device_id


def _do(fn):
    try:
        return fn()
    except HTTPException:
        raise
    except (HelperError, StepFailed) as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception as exc:                          # docker / mqtt problems: show them
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from None


# --------------------------------------------------------------------------- state

@app.get("/api/state")
def state():
    try:
        cp = demo.helper.state()
        cp_error = cp.get("error")
    except HelperError as exc:
        cp, cp_error = {}, str(exc)

    containers = demo.fleet.containers()
    beats = demo.mqtt.heartbeat_ages()
    commands = demo.mqtt.recent_commands(40)
    last_command = {}
    for c in commands:
        last_command[c["device_id"]] = c
    rollout = cp.get("rollout") or {}
    rollout_states = {d["device_id"]: d["state"] for d in rollout.get("devices", [])}

    registry = {d["device_id"]: d for d in cp.get("fleet", [])}
    ids = sorted(set(registry) | set(containers))
    devices = []
    for d in ids:
        row = registry.get(d, {})
        devices.append({
            "device_id": d,
            "version": row.get("version"),
            "previous_version": row.get("previous_version"),
            "status": row.get("status", "unregistered"),
            "errors": row.get("errors", 0),
            "crashes": row.get("crashes", 0),
            "seconds_since_heartbeat": row.get("seconds_since_heartbeat"),
            "heartbeat_seen_s": beats.get(d),          # seen by this computer's listener
            "container": containers.get(d),            # running | stopped | None (not local)
            "rollout_state": rollout_states.get(d),
            "last_command": last_command.get(d),
        })

    return {
        "mode": demo.mode,
        "cp_label": os.environ.get("CP_LABEL", "this computer"),
        "mqtt_connected": demo.mqtt.connected,
        "control_plane_error": cp_error,
        "services": cp.get("services", {}),
        "devices": devices,
        "crashes": cp.get("crashes", []),
        "firmware": cp.get("firmware", []),
        "firmware_check": demo.firmware_results,
        "rollout": cp.get("rollout"),
        "decisions": cp.get("decisions", [])[-40:],
        "commands": commands[-12:],
        "steps": demo.steps.view(),
    }


# --------------------------------------------------------------------------- guided steps

@app.post("/api/steps/{index}/run")
def run_step(index: int):
    if not 0 <= index < len(demo.steps.states):
        raise HTTPException(404, "no such step")
    _do(lambda: demo.steps.run(index))
    return {"ok": True}


@app.post("/api/steps/reset")
def reset_steps():
    demo.steps.reset()
    return {"ok": True}


# --------------------------------------------------------------------------- free play

@app.post("/api/devices/start-fleet")
def start_fleet():
    def go():
        running = demo.fleet.containers()
        started = [d for d in ("device-001", "device-002", "device-003")
                   if running.get(d) != "running"]
        for d in started:
            demo.fleet.start(d)
        return {"message": f"started {', '.join(started)}" if started else "already running"}
    return _do(go)


@app.post("/api/devices/add")
def add_device():
    def go():
        d = demo.fleet.next_device_id()
        demo.fleet.start(d)
        return {"message": f"started {d}"}
    return _do(go)


@app.post("/api/devices/{device_id}/{action}")
def device_action(device_id: str, action: str):
    d = _device(device_id)

    def go():
        if action == "stop":
            demo.fleet.stop(d)
            return {"message": f"stopped {d}; offline in about 30 s"}
        if action == "start":
            demo.fleet.start(d)
            return {"message": f"started {d}"}
        if action == "crash":
            demo.mqtt.heartbeat(d, version="2.1", status="unhealthy", error_count=25,
                                crash_count=1)
            return {"message": f"simulated crash heartbeat from {d}"}
        if action == "bad-message":
            label, payload = BAD_MESSAGES[1]
            demo.mqtt.publish(f"ota/devices/{d}/heartbeat", payload.replace("device-002", d))
            return {"message": f"published a malformed heartbeat ({label}) for {d}"}
        raise HTTPException(404, "unknown action")
    return _do(go)


@app.post("/api/firmware/check")
def firmware_check():
    return _do(lambda: {"cases": demo.check_firmware("2.0")})


class RolloutBody(BaseModel):
    version: str = "2.0"


BASELINE = "1.0"      # every demo device starts on 1.0, so rollbacks return there


@app.post("/api/rollouts")
def start_rollout(body: RolloutBody):
    # An explicit rollback version: right after a simulated crash heartbeat a device can
    # briefly report 2.1, and the orchestrator would otherwise refuse mixed versions.
    rollback_to = BASELINE if body.version != BASELINE else None
    return _do(lambda: demo.helper.start_rollout(body.version, rollback_to=rollback_to))


@app.post("/api/rollouts/rollback")
def rollback():
    return _do(lambda: demo.helper.rollback_latest(
        "operator: rollback requested from the visual demo"))


class ReplyBody(BaseModel):
    outcome: str = "succeed"            # succeed | fail_one


@app.post("/api/rollouts/reply")
def simulated_reply(body: ReplyBody):
    """Reply for every device still waiting on the latest rollout's commands."""
    def go():
        r = demo.helper.state().get("rollout")
        if not r or r["state"] not in ("in_progress", "halted"):
            raise StepFailed("no rollout is waiting for device replies")
        waiting = [d["device_id"] for d in r["devices"] if d["state"] == "commanded"]
        if not waiting:
            raise StepFailed("every device in the rollout has already replied")
        phase = "update" if r["state"] == "in_progress" else "rollback"
        version = r["target_version"] if phase == "update" else r["rollback_version"]
        lines = simulate_replies(demo.mqtt, r["id"], waiting, phase, version,
                                 fail_last=body.outcome == "fail_one")
        return {"message": "; ".join(lines)}
    return _do(go)


# --------------------------------------------------------------------------- page

@app.get("/api/ping")
def ping():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")
