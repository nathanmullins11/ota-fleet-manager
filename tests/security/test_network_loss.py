"""Loss of network connectivity at each critical stage of an update (threat model D7 to D12).

Stages: command delivery, download, install, result report, health window, rollback.
Device tests run a real agent and real firmware against a registry that drops, truncates
or stalls connections on purpose (FileServer.faults). Tests named *_today_* pin current
behaviour the team may decide to change; open_gap tests describe behaviour still missing.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from .stride import open_gap

pytestmark = pytest.mark.security


def outcome(device):
    name, _rid, _version, reason = device.events[-1]
    return name, reason


def assert_still_on_1_0(device):
    assert device.runner.version == "1.0" and device.store.current == "1.0"
    assert not (device.store.versions_dir / "2.0").exists()
    assert not list(device.store.versions_dir.glob(".*.new"))      # no half-installed copy


# =========================================================================== D8: during the download

@pytest.mark.component
@pytest.mark.parametrize("path, fault", [
    ("/packages/2.0/manifest", "drop"),
    ("/packages/2.0/manifest.sig", "drop"),
    ("/packages/2.0", "drop"),
    ("/packages/2.0", "truncate"),
])
def test_D8_link_lost_during_download_leaves_old_firmware_running(device, command, registry,
                                                                 path, fault):
    registry.faults[path] = [fault]
    device.handle(command(registry, "update", "2.0"))
    assert outcome(device)[0] == "update_failed"
    assert_still_on_1_0(device)


@pytest.mark.component
def test_D8_stalled_download_times_out_instead_of_hanging(device, command, registry):
    """The link stays up but data stops: the device gives up after its download timeout."""
    registry.faults["/packages/2.0"] = ["stall"]
    started = time.monotonic()
    device.handle(command(registry, "update", "2.0"))
    elapsed = time.monotonic() - started
    assert outcome(device)[0] == "update_failed"
    assert elapsed < device.cfg.download_timeout_seconds + 5
    assert_still_on_1_0(device)


@pytest.mark.component
def test_D8_update_succeeds_once_the_link_is_back(device, command, registry):
    registry.faults["/packages/2.0"] = ["drop"]
    device.handle(command(registry, "update", "2.0"))
    device.handle(command(registry, "update", "2.0", rollout_id=8))     # the operator retries
    assert outcome(device) == ("update_succeeded", None)
    assert device.runner.version == "2.0"


@pytest.mark.component
@open_gap("D9", "a download that fails once is reported as update_failed; the device never retries")
def test_D9_device_retries_a_download_after_a_brief_outage(device, command, registry):
    registry.faults["/packages/2.0"] = ["drop"]
    device.handle(command(registry, "update", "2.0"))
    assert outcome(device) == ("update_succeeded", None)


# =========================================================================== D10: during the install

@pytest.mark.component
def test_D10_install_cut_short_leaves_previous_version_bootable(device, command, registry):
    """Power or link lost mid-install: only a staging folder is left; the device boots 1.0
    and the next install replaces the leftover."""
    staging = device.store.versions_dir / ".2.0.new"
    (staging / "app").mkdir(parents=True)
    (staging / "app" / "main.py").write_text("half written")
    device.runner.stop()
    device.boot()
    assert device.runner.version == "1.0" and device.store.current == "1.0"
    device.handle(command(registry, "update", "2.0"))
    assert outcome(device) == ("update_succeeded", None)
    assert not staging.exists()


# ===================================================================== D7, D11, D12: orchestrator

@pytest.mark.unit
def test_D7_device_offline_at_command_time_is_not_reached_and_does_not_halt(rollout_logic):
    verdict = rollout_logic.classify_no_answer(heartbeats_since_command=0, timeout_seconds=120)
    assert verdict[0] == "not_reached"
    assert rollout_logic.health_decision({"device-004": verdict}, rollout_logic.HealthLimits()) is None


@pytest.mark.unit
@open_gap("D7", "devices connect with a clean session, so a command sent while a device is "
                "offline (an update, or the rollback it needs most) is dropped by the broker")
def test_D7_device_keeps_its_session_so_missed_commands_arrive(agent, agent_config, tmp_path):
    cfg = agent_config.Config(
        device_id="device-001", mqtt_host="unused", mqtt_port=1883,
        heartbeat_interval_seconds=5, log_level="INFO", firmware_dir=tmp_path,
        factory_dir=tmp_path, public_key_path=tmp_path / "k.pem", start_timeout_seconds=3,
        restart_delay_seconds=0.2, download_timeout_seconds=3)
    client = agent.build_client(cfg, threading.Event(), lambda payload: None)
    assert client._clean_session is False                 # paho's flag for MQTT 3.1.1 sessions


@pytest.mark.unit
def test_D11_late_result_still_counts_after_the_command_timeout(rollout_logic):
    """A result delivered after the link comes back corrects a device marked failed."""
    new_state, ignored = rollout_logic.apply_event("in_progress", "update_succeeded", "2.0",
                                                   "2.0", "1.0")
    assert (new_state, ignored) == ("updated", None)


@pytest.mark.unit
@open_gap("D11", "the orchestrator connects with a clean session, so device results sent while it "
                 "is disconnected are lost and those devices count as failed")
def test_D11_orchestrator_keeps_its_session_so_results_arrive(orch_main):
    cfg = SimpleNamespace(mqtt_client_id="orchestrator", mqtt_host="unused", mqtt_port=8883,
                          mqtt_username="", mqtt_password="", mqtt_ca_cert="")
    client = orch_main.build_client(cfg, None, None)
    assert client._clean_session is False


@pytest.mark.unit
def test_D9_today_one_failed_download_halts_the_whole_rollout(rollout_logic):
    """Current policy (max_failed_devices = 0): one device's network blip halts the rollout and
    rolls back every device updated so far (the whole fleet, unless the rollout runs in stages)."""
    halt = rollout_logic.health_decision({"device-003": ("failed_update", "could not download")},
                                         rollout_logic.HealthLimits())
    assert halt is not None and "device-003" in halt


@pytest.mark.unit
def test_D12_today_one_silent_device_halts_the_whole_rollout(rollout_logic):
    """Current policy: a device that loses its link in the health window looks bricked.
    46 s without a heartbeat (limit 45 s) halts the rollout and rolls back every device updated
    so far (the whole fleet, unless the rollout runs in stages)."""
    limits = rollout_logic.HealthLimits()
    health = rollout_logic.DeviceHealth("device-003", "2.0", seconds_since_update=50, crashes=0,
                                        errors=0, seconds_since_heartbeat=46)
    verdict = rollout_logic.evaluate_device(health, limits)
    assert verdict[0] == "unresponsive"
    assert rollout_logic.health_decision({"device-003": verdict}, limits).startswith(
        "automatic: device-003 unresponsive")
