"""Guided demo steps for the visual demo. Same story as the terminal demo (demo/steps/).

Each step runs on a background thread and reports progress lines the page shows live.
`focus` names the panel the page highlights while the step is selected.
"""
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

DEVICES = ["device-001", "device-002", "device-003"]
BAD_MESSAGES = [
    ("payload is not JSON", "not json at all"),
    ("status value not allowed",
     '{"schema_version":1,"device_id":"device-002","version":"1.0","status":"on-fire",'
     '"error_count":0,"timestamp":"2026-11-05T12:00:00Z"}'),
    ("device ID does not match the topic",
     '{"schema_version":1,"device_id":"device-999","version":"1.0","status":"healthy",'
     '"error_count":0,"timestamp":"2026-11-05T12:00:00Z"}'),
]


class StepFailed(Exception):
    pass


@dataclass
class StepState:
    status: str = "ready"            # ready | running | done | failed
    log: list = field(default_factory=list)
    progress: str = ""


class Ctx:
    """What a step can do: log, wait, and reach the demo's parts."""

    def __init__(self, demo, state: StepState):
        self.demo = demo
        self.state = state

    def log(self, kind: str, text: str) -> None:      # kind: info | ok | bad | sim | cmd | note
        self.state.log.append({"kind": kind, "text": text})

    def wait(self, timeout: float, check: Callable[[], bool], message: str) -> float:
        start = time.monotonic()
        while True:
            try:
                if check():
                    self.state.progress = ""
                    return time.monotonic() - start
            except Exception:
                pass
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                self.state.progress = ""
                raise StepFailed(f"timed out after {int(timeout)}s: {message}")
            self.state.progress = f"{message} … {int(elapsed)}s"
            time.sleep(1)

    # shortcuts
    def cp(self) -> dict:
        return self.demo.helper.state()

    def fleet_row(self, device_id: str) -> dict:
        return next((d for d in self.cp().get("fleet", []) if d["device_id"] == device_id), {})


# --------------------------------------------------------------------------- steps

def step_control_plane(ctx: Ctx):
    services = ctx.cp()["services"]
    for name, ok in services.items():
        ctx.log("ok" if ok else "bad", f"{name}: {'up' if ok else 'not reachable'}")
    if not all(services.values()):
        raise StepFailed("not every control plane service is up")
    ctx.log("ok", "Control plane up: broker, device registry, software registry, orchestrator")


def step_devices(ctx: Ctx):
    running = ctx.demo.fleet.containers()
    for d in DEVICES:
        if running.get(d) != "running":
            ctx.demo.fleet.start(d)
            ctx.log("info", f"Started {d} (container on this computer, firmware 1.0)")
    for d in DEVICES:
        ctx.wait(30, lambda d=d: d in ctx.demo.mqtt.heartbeat_ages()
                 and ctx.demo.mqtt.heartbeat_ages()[d] < 10,
                 f"waiting for {d}'s first heartbeat")
        ctx.log("ok", f"{d} connected to the broker and sent a heartbeat")
    ctx.wait(20, lambda: all(ctx.fleet_row(d).get("status") == "healthy" for d in DEVICES),
             "waiting for the orchestrator to record them")
    ctx.log("ok", "All three registered in the device registry: version 1.0, healthy")


def step_bad_messages(ctx: Ctx):
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    time.sleep(1)
    for label, payload in BAD_MESSAGES:
        ctx.demo.mqtt.publish("ota/devices/device-002/heartbeat", payload)
        ctx.log("info", f"Published to device-002's topic: {label}")

    def rejections():
        return [d for d in ctx.cp()["decisions"]
                if d["time"] >= since and d["text"].startswith("Rejected")]
    ctx.wait(15, lambda: len(rejections()) >= 3, "waiting for the orchestrator")
    for d in rejections()[:3]:
        ctx.log("bad", d["text"])
    ctx.log("ok", "All three rejected with the reason logged; device-002's entry is untouched")


def step_crash(ctx: Ctx):
    ctx.log("note", "The agent will report firmware crashes once it runs firmware; this step "
                    "sends the heartbeat a device-003 running faulty 2.1 would send.")
    ctx.demo.mqtt.heartbeat("device-003", version="2.1", status="unhealthy", error_count=25,
                            crash_count=1)
    ctx.log("sim", "device-003 → version 2.1, unhealthy, error_count 25, crash_count 1")
    ctx.wait(10, lambda: any(c["device_id"] == "device-003" for c in ctx.cp()["crashes"]),
             "waiting for it to be recorded")
    ctx.log("ok", "Recorded with errors and crashes in separate columns (Recent crashes panel)")
    ctx.log("info", "Heartbeats keep coming, so this is a crash, not an offline device. "
                    "device-003's next real heartbeat returns it to normal.")


def step_offline(ctx: Ctx):
    ctx.demo.fleet.stop("device-002")
    ctx.log("info", "Stopped device-002's container: its heartbeats stop")
    took = ctx.wait(75, lambda: ctx.fleet_row("device-002").get("status") == "offline",
                    "waiting for the 30 s heartbeat timeout")
    ctx.log("bad", f"device-002 marked offline {int(took)}s after its container stopped "
                   "(30 s after its last heartbeat)")
    ctx.demo.fleet.start("device-002")
    ctx.log("info", "Started device-002 again")
    took = ctx.wait(30, lambda: ctx.fleet_row("device-002").get("status") == "healthy",
                    "waiting for its first heartbeat")
    ctx.log("ok", f"device-002 back online {int(took)}s after restarting")


def step_firmware(ctx: Ctx):
    results = ctx.demo.check_firmware("2.0")
    for case in results:
        verdict = "ACCEPTED" if case["accepted"] else f"REJECTED: {case['reason']}"
        good = case["accepted"] == case["expected"]
        ctx.log("ok" if good else "bad", f"{case['title']} → {verdict}")
    if not all(c["accepted"] == c["expected"] for c in results):
        raise StepFailed("a verification case did not behave as expected")
    ctx.log("ok", "Only the genuine, correctly signed package for the requested version passes")


def simulate_replies(mqtt, rollout_id: int, devices: list, phase: str, version: str,
                     fail_last: bool) -> list:
    """Send the events devices will send once the agent installs firmware. Returns log lines."""
    lines = []
    for i, d in enumerate(devices):
        if phase == "update":
            mqtt.event(d, "update_started", rollout_id, version)
            if fail_last and i == len(devices) - 1 and len(devices) > 1:
                mqtt.event(d, "update_failed", rollout_id, version, "checksum mismatch")
                lines.append(f"{d}: update_started, update_failed (checksum mismatch)")
            else:
                mqtt.event(d, "update_succeeded", rollout_id, version)
                lines.append(f"{d}: update_started, update_succeeded")
        else:
            mqtt.event(d, "rollback_started", rollout_id, version)
            mqtt.event(d, "rollback_succeeded", rollout_id, version)
            lines.append(f"{d}: rollback_started, rollback_succeeded")
    return lines


def _rollout_state(ctx: Ctx) -> str:
    r = ctx.cp().get("rollout") or {}
    return r.get("state", "")


def step_rollout(ctx: Ctx):
    # A simulated crash heartbeat (step 4) leaves device-003 reporting 2.1 until its next
    # real heartbeat; start from a clean 1.0 fleet.
    ctx.wait(15, lambda: all(d.get("version") == "1.0" for d in ctx.cp().get("fleet", [])
                             if d.get("status") != "offline"),
             "waiting for every device to report 1.0")
    result = ctx.demo.helper.start_rollout("2.0", rollback_to="1.0")
    rid, targets = result["id"], result["commanding"]
    ctx.log("info", f"Rollout {rid} created: update to 2.0 for {', '.join(targets)}")
    ctx.wait(15, lambda: set(targets) <= set(ctx.demo.mqtt.commands_for("update", rid)),
             "waiting for the orchestrator's commands")
    ctx.log("cmd", f"Update command received by {len(targets)} devices (click a device to see it)")
    ctx.log("note", "Simulated replies: the agent will send these itself once it installs firmware.")
    for line in simulate_replies(ctx.demo.mqtt, rid, targets, "update", "2.0", fail_last=True):
        ctx.log("sim", line)
    ctx.wait(20, lambda: _rollout_state(ctx) == "completed", "waiting for the rollout to finish")
    ctx.log("ok", f"Rollout {rid} completed; one device failed. Every decision is in the log.")


def step_rollback(ctx: Ctx):
    result = ctx.demo.helper.rollback_latest("operator: 2.0 failed on part of the fleet")
    rid = result["id"]
    ctx.log("info", f"Rollback of rollout {rid} requested: devices go back to "
                    f"{result['rollback_version']}")
    targets = [d["device_id"] for d in (ctx.cp().get("rollout") or {}).get("devices", [])]
    ctx.wait(15, lambda: set(targets) <= set(ctx.demo.mqtt.commands_for("rollback", rid)),
             "waiting for the rollback commands")
    ctx.log("cmd", f"Rollback command received by {len(targets)} devices")
    for line in simulate_replies(ctx.demo.mqtt, rid, targets, "rollback",
                                 result["rollback_version"], fail_last=False):
        ctx.log("sim", line)
    ctx.wait(20, lambda: _rollout_state(ctx) == "rolled_back", "waiting for the rollback to finish")
    ctx.log("ok", f"Rollout {rid} rolled back; the decision log shows each step and why.")


STEPS = [
    dict(title="Control plane", covers="NF-6", focus="services", run=step_control_plane,
         explain="The broker, device registry, software registry and orchestrator start from one "
                 "Docker Compose command.",
         look="The service lights in the header."),
    dict(title="Devices register", covers="FR-1, FR-8", focus="fleet", run=step_devices,
         explain="Three simulated devices, each a container on this computer, connect over MQTT "
                 "and send a heartbeat every 5 seconds with their version and health.",
         look="Device tiles appear and pulse with every heartbeat."),
    dict(title="Bad messages rejected", covers="Contract, NF-5", focus="decisions",
         run=step_bad_messages,
         explain="Three heartbeats that break the message contract are published on "
                 "device-002's topic. The orchestrator rejects each and logs why.",
         look="Three yellow 'Rejected' lines in the decision log."),
    dict(title="Crash vs offline", covers="FR-4 groundwork", focus="fleet", run=step_crash,
         explain="A crashed device keeps sending heartbeats with crash_count; an offline device "
                 "goes silent. Recording crashes apart from errors lets the orchestrator tell "
                 "them apart.",
         look="device-003 turns red with a crash badge, then recovers."),
    dict(title="Offline detection", covers="30 s timeout, NF-1", focus="fleet", run=step_offline,
         explain="device-002's container is stopped. After 30 seconds without a heartbeat the "
                 "orchestrator marks it offline; it comes back on its first heartbeat.",
         look="device-002 greys out as offline, then turns green again."),
    dict(title="Signed firmware", covers="FR-6, FR-3", focus="firmware", run=step_firmware,
         explain="Firmware 2.0 is downloaded from the registry and checked as a device will: "
                 "signature, version, checksum. Then the download is tampered with in memory.",
         look="One green ACCEPTED, four red REJECTED with the reason."),
    dict(title="Rollout 2.0", covers="Commands, NF-5", focus="rollout", run=step_rollout,
         explain="The orchestrator sends each device its own update command. Device replies are "
                 "simulated (one fails) until the agent installs firmware.",
         look="Rollout panel: devices commanded, then updated / failed; click a device to see "
              "its command."),
    dict(title="Rollback", covers="FR-5 groundwork, NF-5", focus="rollout", run=step_rollback,
         explain="The operator rolls the rollout back: every device gets a rollback command to "
                 "1.0, and the rollout ends rolled_back with the reason recorded.",
         look="Rollout panel turns ROLLED BACK; decisions show halted → rollback_completed."),
]


class StepRunner:
    def __init__(self, demo):
        self.demo = demo
        self.states = [StepState() for _ in STEPS]
        self.lock = threading.Lock()
        self.running = None

    def run(self, index: int) -> None:
        with self.lock:
            if self.running is not None:
                raise StepFailed(f"step {self.running + 1} is still running")
            self.running = index
            self.states[index] = StepState(status="running")
        threading.Thread(target=self._run, args=(index,), daemon=True).start()

    def _run(self, index: int) -> None:
        state = self.states[index]
        ctx = Ctx(self.demo, state)
        try:
            STEPS[index]["run"](ctx)
            state.status = "done"
        except Exception as exc:
            ctx.log("bad", f"Step failed: {exc}")
            state.status = "failed"
        finally:
            state.progress = ""
            with self.lock:
                self.running = None

    def reset(self) -> None:
        with self.lock:
            if self.running is None:
                self.states = [StepState() for _ in STEPS]

    def view(self) -> list:
        return [{"index": i, "title": s["title"], "covers": s["covers"], "focus": s["focus"],
                 "explain": s["explain"], "look": s["look"], "status": st.status,
                 "log": st.log, "progress": st.progress}
                for i, (s, st) in enumerate(zip(STEPS, self.states))]
