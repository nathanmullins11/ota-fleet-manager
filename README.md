# ota-fleet-manager
SENG 650: Over-the-Air (OTA) Update Fleet Manager: staged firmware rollout and automatic rollback for IoT devices, built on a cloud control plane

## Demo

`demo/local/demo.sh` runs a scripted demo with everything on one computer. For the control plane on the cloud VM and devices on your computer, run `demo/cloud/control-plane.sh` on the VM and `demo/cloud/devices.sh <vm-ip>` locally. For a live web page with guided steps and buttons, use `demo/visual/run.sh` (add `--cloud <vm-ip>` for the VM setup). See [demo/README.md](demo/README.md) and [demo/visual/README.md](demo/visual/README.md).
