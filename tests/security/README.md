# Security tests

Tests for the STRIDE threat model in [`docs/threat-model.md`](../../docs/threat-model.md). Each test name starts with the threat it covers (`test_S1_...`, `test_D9_...`), so a failure points straight at a row of the threat table.

92 tests, all passing: every threat in the model is mitigated, accepted, or mitigated in part.

## Running them

```bash
pip install -r tests/requirements.txt
pytest -m security                  # all of them, about 20 seconds
pytest -m "security and unit"       # static checks only, under a second
pytest tests/security -k D7         # one threat
```

CI runs them with the unit and component tests. They don't need Docker, a broker or Postgres.

## Open threats: `open_gap`

A test for a threat that isn't fixed yet is marked `@open_gap("ID", "why")`, which is pytest's `xfail(strict=True)`:

- While the threat is open, the test fails, pytest reports it as `xfail`, and CI stays green.
- Once someone fixes the threat, the test passes, strict mode turns that into a failure, and CI goes red.
- Then remove the `open_gap` line, so the test becomes a regression test, and update the threat's row in `docs/threat-model.md`.

No test is marked `open_gap` today: the last nine were fixed and their markers removed. A newly found threat starts with one. `pytest -m security -rx` lists any that are open.

When changing an `open_gap` test, run it with `--runxfail` to check it fails on its assertion. Without that flag, an expected failure also hides crashes, such as a test whose fake config no longer matches the code.

## Files

| File | Level | What it covers |
| --- | --- | --- |
| `test_stride_static.py` | unit | Command and message parsers, the installer with hand-built signed packages (path traversal, links, bad entry points), and checks on `mosquitto.conf`, the ACL, Dockerfiles, Compose ports, `.gitignore`, the schema, and every tracked file for private keys |
| `test_stride_device.py` | component | A real device agent running real firmware, against the genuine registry and an attacker's web server: forged signatures, replayed manifests, downgrades, planted code, foreign download hosts |
| `test_network_loss.py` | component and unit | The link lost at each stage of an update (command, download, install, result, health window): retries, persistent MQTT sessions, and the orchestrator's rules for silent and failed devices |
| `stride.py` | | Shared helpers and fixtures (below) |
| `conftest.py` | | Makes the fixtures in `stride.py` available here. This folder is a package, so this file never shadows `tests/conftest.py` |

## Fixtures (`stride.py`)

| Fixture | Gives you |
| --- | --- |
| `device` | An `agent.Device` on factory firmware 1.0 with real firmware processes, whose configured registry is `registry`. Events it reports are in `device.events`. It tries each download 3 times |
| `registry` | An HTTP server with every test package, signed with the test release key |
| `attacker` | A second, empty HTTP server the attacker controls. The device refuses it unless a test calls `serve_at_registry_address(device, attacker)`: an attacker on the network path, or one who took over the registry |
| `command` | `command(server, "update", "2.0")` builds a command whose URLs point at that server |
| `make_package` | `make_package(version, members, key=...)` builds a correctly signed package from hand-made tar entries |
| `read_repo` | `read_repo("deploy/mqtt/mosquitto.conf")` returns a repository file's text |
| `orch_main` | The orchestrator's `main.py` |

`registry` and `attacker` can also simulate a bad network. Each request to a path uses up one fault from `server.faults[path]`:

```python
registry.faults["/packages/2.0"] = ["drop"]       # or "truncate", "stall"
```

The device retries a failed download, so a fault that should outlast the retries needs repeating: `["drop"] * device.cfg.download_attempts`.

- `drop` closes the connection without answering.
- `truncate` sends half the body.
- `stall` sends part of the body, then hangs until the test ends.

All keys used here are throwaway test keys; nothing needs the real release key.
