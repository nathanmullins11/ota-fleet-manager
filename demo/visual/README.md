# Visual demo

A live web page for presenting the OTA Update Fleet Manager: device tiles that pulse with every heartbeat, the current rollout, the orchestrator's decisions with their reasons, signed-firmware checks, and a guided walkthrough. It is demo tooling, separate from the Iteration 3 dashboard (FR-9). The terminal demo (`demo/local/demo.sh`, `demo/cloud/devices.sh`) remains the fallback.

## Run it

**Everything on this computer** (one script):

```bash
demo/visual/run.sh
```

**Control plane on the cloud VM, devices on this computer** (two scripts):

```bash
# on the VM
demo/cloud/control-plane.sh            # prints the visual demo password
# on your computer
demo/visual/run.sh --cloud <vm-ip>     # asks for the password (or set VISUAL_DEMO_PASSWORD)
```

The page opens at **http://localhost:8088**. Press **Ctrl+C** in the terminal to stop; it cleans up the devices and, in local mode, its own control plane (compose project `ota-demo`, fresh database each run). `--keep` leaves everything running; `--no-open` skips opening the browser.

Only Docker is needed. For the cloud mode, the VM's security group must allow TCP **1883**, **8080** and **8090** from your IP only.

## Using the page

| Area | Shows | Buttons |
|---|---|---|
| Header | Health of the broker, device registry, software registry, orchestrator, and this page's MQTT connection | |
| Fleet | One tile per device: version, status, last heartbeat (the dot pulses on each one), errors, crashes, rollout state, last command | Start fleet, + Device; hover a tile for Stop/Start, Crash (simulated crash heartbeat), Bad msg (malformed heartbeat). Click a tile to see the full last command |
| Rollout | Latest rollout: state, one segment per device, decisions with reasons | Start rollout (2.0 or 2.1), Roll back, and the **simulated** device replies: All succeed / One fails |
| Orchestrator decisions | The orchestrator's log of decisions: rollouts, rejected messages, offline devices | |
| Signed firmware | Versions and checksums in the registry, the device-side verification of 2.0 and four tampered variants, recent crashes | Verify & tamper test |
| Guided demo | Eight steps, the same story as the terminal demo, each with an explanation, what to look at, a Run button and a live log. The panel the step is about is highlighted | **← →** choose a step, **Enter** runs it |

Device replies to update and rollback commands are **simulated** (marked SIM / simulated on the page) because the device agent does not install firmware yet. The commands themselves are real: the orchestrator sends them and the device containers receive them. Once the agent replies on its own, remove `simulate_replies` from `server/steps.py` and the reply buttons.

## How it works

```
 your computer                                          control plane (local or VM)
 ┌──────────────────────────────────────┐               ┌──────────────────────────────┐
 │ browser ── http://localhost:8088 ──▶ │ visual demo   │ visual-helper (port 8090)    │
 │                                      │ server ───────┼▶ fleet, rollouts, decisions; │
 │ device containers ◀── Docker ────────┤ (container)   │  start rollout / roll back   │
 │        │                             │   │           │        │                     │
 │        └────── MQTT 1883 ────────────┼───┴───────────┼▶ broker, orchestrator, DB,   │
 └──────────────────────────────────────┘               │  software registry           │
                                                        └──────────────────────────────┘
```

- **server/** runs on your computer in a container (`ota-demo-visual-server`). It serves the page, starts and stops the device containers through the Docker socket, publishes test messages, listens to command topics, runs the guided steps, and runs the firmware checks (`demo/lib/firmware_check.py`, which uses `tools/release.py`).
- **helper/** runs next to the control plane, added by `helper/compose.yml` on top of `deploy/docker-compose.yml`; it is never part of the control plane's own compose file. PostgreSQL only listens on its own host, so the helper reads the device registry for the page, reads the orchestrator's decisions from a log file the orchestrator writes when `LOG_FILE` is set, and starts rollouts and rollbacks through the orchestrator's own code (`rollout_logic`, `RolloutStore`), exactly like `cli.py`. Every `/api` call needs the `X-Demo-Password` header.
- **web/** is one static page (HTML, CSS, JavaScript; no build step) that polls the server every second.

Local and cloud mode use the same code; only the addresses differ.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Port ... is already in use` | Stop your own control plane (`cd deploy && docker compose down`) or another demo |
| Red banner "the helper rejected the password" | Use the password printed by `control-plane.sh` (stored as `VISUAL_DEMO_PASSWORD` in `deploy/.env` on the VM) |
| Red banner "cannot reach the control plane helper" | Is `control-plane.sh` running on the VM? Is TCP 8090 open to your current IP? |
| A step fails with "rollout ... is still in_progress" | A rollout from earlier is still active: press Roll back, then the simulated reply button, or restart with a fresh database (`control-plane.sh --reset` on the VM) |
| Leftover containers after a crash | `docker rm -f ota-demo-visual-server $(docker ps -aq --filter label=ota-demo=device)` and, for local mode, `docker compose -p ota-demo -f deploy/docker-compose.yml -f demo/visual/helper/compose.yml down -v` |
