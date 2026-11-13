# ota-fleet-manager

[![CI](https://github.com/nathanmullins11/ota-fleet-manager/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/nathanmullins11/ota-fleet-manager/actions/workflows/ci.yml)
[![tests](https://nathanmullins11.github.io/ota-fleet-manager/badges/tests.svg)](https://nathanmullins11.github.io/ota-fleet-manager/)
[![coverage](https://nathanmullins11.github.io/ota-fleet-manager/badges/coverage.svg)](https://nathanmullins11.github.io/ota-fleet-manager/coverage/)
[![end-to-end](https://nathanmullins11.github.io/ota-fleet-manager/badges/e2e.svg)](https://nathanmullins11.github.io/ota-fleet-manager/)

SENG 650: Over-the-Air (OTA) Update Fleet Manager: staged firmware rollout and automatic rollback for IoT devices, built on a cloud control plane

## Demo

`demo/local/demo.sh` runs a scripted demo with everything on one computer. For the control plane on the cloud VM and devices on your computer, run `demo/cloud/control-plane.sh` on the VM and `demo/cloud/devices.sh <vm-ip>` locally. See [demo/README.md](demo/README.md).

## Tests and CI

```bash
pip install -r tests/requirements.txt
pytest -m "unit or component" --cov    # unit + component tests, coverage report (.coveragerc)
                                       # orchestrator database tests need a throwaway Postgres:
docker run -d --name ota-test-db -p 5433:5432 -e POSTGRES_USER=ota -e POSTGRES_PASSWORD=test \
    -e POSTGRES_DB=ota_test postgres:16
OTA_TEST_DB="host=localhost port=5433 user=ota password=test dbname=ota_test" pytest -m component
pip install ruff==0.17.4 && ruff check .   # Python lint (ruff.toml)
demo/local/demo.sh --auto              # end-to-end: exits 1 if any check is NOT WORKING
```

GitHub Actions (`.github/workflows/ci.yml`) runs on every pull request and push to `main`:

| Job | What it checks |
|---|---|
| tests | Unit and component tests on Python 3.12, including the orchestrator's rollout and device registry code against a real Postgres (service container); fails if coverage drops below the floor in `.coveragerc` (coverage summary on the run page) |
| lint | ruff: unused or undefined names and likely bugs |
| shellcheck | Every shell script |
| docker | Both Compose files are valid and every image builds |
| e2e | The full local demo on the runner: real control plane and 5 real devices; every check must pass. Its output is attached to the run |
| dashboard | Builds the [test dashboard](https://nathanmullins11.github.io/ota-fleet-manager/) from the results above: headline numbers, tests by layer, coverage by component with the line-by-line report, and every test. Published to GitHub Pages on pushes to `main`; on pull requests it is attached to the run (artifact `github-pages`) |

Each run also has **Test results** checks listing every test, with failures linked to their line.

**One-time setup for the dashboard:** in the repository's Settings → Pages, set Source to **GitHub Actions**. Until the first push to `main` after that, the badges above show as broken images.
