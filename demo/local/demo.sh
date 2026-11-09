#!/usr/bin/env bash
# Local demo: control plane and devices on this machine, one terminal.
#
#   demo/local/demo.sh            # pauses before each step: press Enter to continue
#   demo/local/demo.sh --auto     # runs straight through (rehearsals, recordings)
#   demo/local/demo.sh --keep     # leave everything running afterwards
#   demo/local/demo.sh --simulate # fake the device replies (before the device update logic exists)
#
# By default the demo checks what the devices really do (install, crash, roll back, reject
# forged commands) and prints NOT WORKING in red for anything they do not do yet.
#
# Uses its own compose project (ota-demo) with a fresh database, so it does not touch a
# control plane you run yourself from deploy/. Everything it starts is removed at the
# end unless you pass --keep.
set -uo pipefail

KEEP=0
for arg in "$@"; do
    case "$arg" in
        --auto) export AUTO=1 ;;
        --keep) KEEP=1 ;;
        --simulate) export SIMULATE=1 ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
    esac
done

export CP_LOCAL=1
export CP_PROJECT=ota-demo
export MQTT_HOST=host.docker.internal
export REGISTRY_URL=http://host.docker.internal:8080
# Only for the demo's own throwaway database; overrides deploy/.env if there is one.
export POSTGRES_PASSWORD="demo-$RANDOM$RANDOM"
# Devices that never answer a command are marked failed after this; short, so a missing
# device feature does not hold up the next step for two minutes.
export COMMAND_TIMEOUT_SECONDS=30

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

cleanup() {
    echo
    if [[ "$KEEP" == 1 ]]; then
        info "Left running (--keep). To remove everything later:"
        info "  docker rm -f \$(docker ps -aq --filter label=$DEMO_LABEL)"
        info "  docker compose -p $CP_PROJECT -f deploy/docker-compose.yml down -v"
        return
    fi
    info "Cleaning up: removing demo devices and the demo control plane..."
    remove_devices
    cp_compose down -v --remove-orphans >/dev/null 2>&1
    ok "Done."
}

require_docker
check_no_other_devices
for port in 1883 5432 8080; do
    if port_open 127.0.0.1 "$port"; then
        die "Port $port is already in use on this machine, so the demo control plane cannot start.
  If your own control plane is running, stop it first:  (cd deploy && docker compose down)"
    fi
done

trap cleanup EXIT
trap 'exit 130' INT

banner "OTA Update Fleet Manager — local demo" \
       "Control plane and $DEVICE_COUNT simulated devices on this machine"
build_images
[[ "${SIMULATE:-0}" == 1 ]] && warn "--simulate: device replies are faked by the demo (marked SIMULATED)."
pause 1

run_steps
