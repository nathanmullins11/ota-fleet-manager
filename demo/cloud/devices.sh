#!/usr/bin/env bash
# Cloud demo, part 2 of 2: run on your computer. Starts the simulated devices, connects
# them to the control plane on the VM, and walks through the demo steps. Keep the VM
# terminal (demo/cloud/control-plane.sh) visible next to this one.
#
#   demo/cloud/devices.sh <vm-ip>           # pauses before each step: press Enter
#   demo/cloud/devices.sh <vm-ip> --auto    # runs straight through
#   demo/cloud/devices.sh <vm-ip> --keep    # leave the devices running afterwards
#   demo/cloud/devices.sh <vm-ip> --simulate  # fake the device replies
#   demo/cloud/devices.sh <vm-ip> --devices N # number of simulated devices, 3 to 50 (default 5)
#   demo/cloud/devices.sh <vm-ip> --detail    # full evidence: device logs, processes, raw messages
#
# The VM's security group must allow TCP 1883 (MQTT) from your IP, and TCP 8080
# (software registry) for the signed-firmware step.
set -uo pipefail

KEEP=0
VM_HOST=${OTA_VM_HOST:-}
while (($#)); do
    arg=$1
    case "$arg" in
        --devices) export DEVICE_COUNT=${2:-}; shift ;;
        --auto) export AUTO=1 ;;
        --keep) KEEP=1 ;;
        --simulate) export SIMULATE=1 ;;
        --detail) export DETAIL=1 ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
        *) VM_HOST=$arg ;;
    esac
    shift
done
if [[ -n "${DEVICE_COUNT:-}" ]] && ! { [[ "$DEVICE_COUNT" =~ ^[0-9]+$ ]] && ((DEVICE_COUNT >= 3 && DEVICE_COUNT <= 50)); }; then
    echo "--devices must be a number from 3 to 50 (steps use device-002 and device-003)" >&2
    exit 2
fi
if [[ -z "$VM_HOST" ]]; then
    echo "Usage: demo/cloud/devices.sh <vm-ip> [--auto] [--keep]   (or set OTA_VM_HOST)" >&2
    exit 2
fi

export CP_LOCAL=0
export MQTT_HOST=$VM_HOST
export REGISTRY_URL=http://$VM_HOST:8080

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

cleanup() {
    echo
    if [[ "$KEEP" == 1 ]]; then
        info "Devices left running (--keep). Remove them with:"
        info "  docker rm -f \$(docker ps -aq --filter label=$DEMO_LABEL)"
        return
    fi
    info "Removing the demo devices (the control plane on the VM keeps running)..."
    remove_devices
    ok "Done."
}

require_docker
check_no_other_devices

banner "OTA Update Fleet Manager — cloud demo" \
       "Control plane on the VM ($VM_HOST), $DEVICE_COUNT simulated devices on this machine"

say "Checking the VM is reachable from here:"
if port_open "$VM_HOST" 1883; then
    ok "MQTT broker   $VM_HOST:1883"
else
    bad "MQTT broker   $VM_HOST:1883 is not reachable."
    die "Check that control-plane.sh is running on the VM and the security group allows TCP 1883
  from your IP (find it with: curl -4 ifconfig.me). Some campus/work networks block 1883."
fi
if port_open "$VM_HOST" 8080; then
    ok "Registry      $VM_HOST:8080"
    export REGISTRY_REACHABLE=1
else
    warn "Registry      $VM_HOST:8080 is not reachable; the signed-firmware step will be skipped."
    export REGISTRY_REACHABLE=0
fi

trap cleanup EXIT
trap 'exit 130' INT

build_images
[[ "${SIMULATE:-0}" == 1 ]] && warn "--simulate: device replies are faked by the demo (marked SIMULATED)."
pause 1

run_steps
