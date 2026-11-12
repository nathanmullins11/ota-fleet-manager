# ota-fleet-manager
SENG 650: Over-the-Air (OTA) Update Fleet Manager: staged firmware rollout and automatic rollback for IoT devices, built on a cloud control plane

## Demo

`demo/local/demo.sh` runs a scripted demo with everything on one computer. For the control plane on the cloud VM and devices on your computer, run `demo/cloud/control-plane.sh` on the VM and `demo/cloud/devices.sh <vm-ip>` locally. See [demo/README.md](demo/README.md).

## Tests and CI

```bash
pip install -r tests/requirements.txt
pytest -m "unit or component" --cov    # unit + component tests, coverage report (.coveragerc)
pip install ruff==0.17.4 && ruff check .   # Python lint (ruff.toml)
demo/local/demo.sh --auto              # end-to-end: exits 1 if any check is NOT WORKING
```

GitHub Actions (`.github/workflows/ci.yml`) runs on every pull request and push to `main`:

| Job | What it checks |
|---|---|
| tests | Unit and component tests on Python 3.12; fails if coverage drops below the floor in `.coveragerc` (coverage summary on the run page) |
| lint | ruff: unused or undefined names and likely bugs |
| shellcheck | Every shell script |
| docker | Both Compose files are valid and every image builds |
| e2e | The full local demo on the runner: real control plane and 5 real devices; every check must pass. Its output is attached to the run |
