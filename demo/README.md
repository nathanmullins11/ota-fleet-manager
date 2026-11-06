# Demo

Scripted demos of the OTA Update Fleet Manager in two setups:

| Setup | Scripts | Terminals |
|---|---|---|
| **Local**: control plane and devices on one computer | `demo/local/demo.sh` | 1 |
| **Cloud**: control plane on the VM, devices on your computer | `demo/cloud/control-plane.sh` (VM) and `demo/cloud/devices.sh` (computer) | 2, side by side |

Each step says what is about to happen and which requirement it covers, does it, and shows the result. By default the script waits for **Enter** before each step so you can talk; `--auto` runs straight through (about 2 minutes, most of it the 30-second offline timeout).

## What it shows

| Step | Shows | Requirements |
|---|---|---|
| 1 | Control plane started by one compose command, all services healthy | NF-6 |
| 2 | Three simulated devices connect over MQTT and register with version 1.0 and a heartbeat | FR-1, FR-8 |
| 3 | Three malformed heartbeats are rejected and logged with the reason | Message contract, NF-5 |
| 4 | A heartbeat reporting a firmware crash is stored with `crash_count` separately from errors | FR-4 groundwork, FR-8 |
| 5 | A stopped device is marked offline after the 30 s timeout, then back online on its next heartbeat | Timeout requirement, NF-1 |
| 6 | The registry's signed firmware: a genuine package verifies; a flipped bit, a forged manifest, a different signing key, and a replayed version are all rejected | FR-6, FR-3 checks |

Step 4 publishes the heartbeat a device running faulty 2.1 will send, because the agent does not run firmware yet. Step 6 runs the device's verification checks (`verify_package()` in `tools/release.py`) on a real download from the registry; tampering happens in memory only.

## Requirements

Docker with Compose v2, on both machines. Nothing else: the demo builds small images for the device agent and the verification tool on first run (cached afterwards).

## Local

From the repository root:

```bash
demo/local/demo.sh            # Enter between steps
demo/local/demo.sh --auto     # no pauses
demo/local/demo.sh --keep     # leave everything running afterwards
```

It runs its own copy of the control plane (compose project `ota-demo`) with a fresh database, and removes it and the devices at the end, so it does not touch a stack you run yourself from `deploy/`. Ports 1883, 5432 and 8080 must be free: stop your own stack first with `(cd deploy && docker compose down)`.

## Cloud

**Before the first run:** the VM's security group must allow, from your IP only:

| Port | For |
|---|---|
| 22 | SSH |
| 1883 | Devices to the MQTT broker |
| 8080 | Step 6 (and firmware downloads from Iteration 2). Without it, step 6 is skipped with a warning |

The VM's checkout must be on a branch that contains `demo/` (`git checkout <branch>` in `~/ota-fleet-manager`).

**Terminal 1, on the VM** (`ssh` in first):

```bash
cd ~/ota-fleet-manager
demo/cloud/control-plane.sh             # git pull, build, start, migrate, then live view
demo/cloud/control-plane.sh --reset     # same, but wipe the database first (clean fleet table)
```

It updates the code, starts the control plane, applies database migrations, prints the exact command for your computer, and then shows a live view, refreshed every 2 seconds:

- **Services**: health of each container
- **Fleet**: the device registry (`fleet_overview`)
- **Recent crashes**: heartbeats with `crash_count > 0`
- **Orchestrator decisions**: rejected messages, offline devices, broker connections

Ctrl+C closes the live view; the control plane keeps running. `--view` reopens just the view; `--no-pull` skips `git pull`.

**Terminal 2, on your computer**, from the repository root:

```bash
demo/cloud/devices.sh <vm-ip>           # Enter between steps
demo/cloud/devices.sh <vm-ip> --auto
```

It checks ports 1883 and 8080 on the VM, starts the devices, and walks through the steps. Where the result is on the VM, it prints **👉 On the VM terminal: ...** with what to point at. The devices are removed at the end; the control plane keeps running (and marks them offline 30 s later).

## Troubleshooting

| Problem | Fix |
|---|---|
| `Port 1883 is already in use` (local) | Your own control plane is running: `(cd deploy && docker compose down)` |
| `Devices from deploy/device-agent are already running` | `(cd deploy/device-agent && docker compose down)`; two devices with the same ID keep disconnecting each other |
| `MQTT broker <ip>:1883 is not reachable` | Is `control-plane.sh` running? Does the security group allow 1883 from your current IP (`curl -4 ifconfig.me`)? Some campus and work networks block 1883 |
| Step 6 skipped | Open TCP 8080 to your IP in the security group |
| `git pull failed` on the VM | Local changes or a branch without an upstream: fix it, or run with `--no-pull` |
| Leftover demo containers after a crash | `docker rm -f $(docker ps -aq --filter label=ota-demo=device)` and `docker compose -p ota-demo -f deploy/docker-compose.yml down -v` |

## Adding steps

Each file in `demo/steps/` is one step, run in file-name order by both setups. A step uses the helpers in `demo/lib/common.sh` and checks `CP_LOCAL`: when it is `1` the control plane is on this machine and the step can show results itself (`show_fleet`, `cp_psql`, `orchestrator_log`); otherwise it tells the audience what to look for with `look_at_vm`. Iteration 2 and 3 steps (update, rollback, staged rollout) go in as new numbered files.
