#!/usr/bin/env bash
# Visual demo: a live web page with guided steps and free-play buttons.
#
#   demo/visual/run.sh                  # everything on this computer
#   demo/visual/run.sh --cloud <vm-ip>  # control plane on the VM (start it there first with
#                                       # demo/cloud/control-plane.sh), devices on this computer
#   options: --keep (leave everything running), --no-open (do not open a browser)
#
# Opens http://localhost:8088. Ctrl+C stops the demo and cleans up: in local mode its own
# control plane (compose project ota-demo) and the devices; in cloud mode the devices only.
# The terminal demo (demo/local/demo.sh, demo/cloud/devices.sh) remains the fallback.
set -uo pipefail

MODE=local KEEP=0 OPEN=1 VM_HOST=""
while (($#)); do
    case "$1" in
        --cloud) MODE=cloud; VM_HOST=${2:-}; shift ;;
        --keep) KEEP=1 ;;
        --no-open) OPEN=0 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done
if [[ "$MODE" == cloud && -z "$VM_HOST" ]]; then
    echo "Usage: demo/visual/run.sh --cloud <vm-ip>" >&2
    exit 2
fi

SERVER=ota-demo-visual-server
SERVER_IMAGE=ota-demo-visual-server
PORT=8088
URL="http://localhost:$PORT"

if [[ "$MODE" == local ]]; then
    export CP_LOCAL=1 CP_PROJECT=ota-demo
    export MQTT_HOST=host.docker.internal REGISTRY_URL=http://host.docker.internal:8080
    export HELPER_URL=http://host.docker.internal:8090
    export POSTGRES_PASSWORD="demo-$RANDOM$RANDOM"
    export VISUAL_DEMO_PASSWORD="demo-$RANDOM$RANDOM$RANDOM"
    export VISUAL_HELPER_BIND=127.0.0.1
    CP_LABEL="this computer"
else
    export CP_LOCAL=0
    export MQTT_HOST=$VM_HOST REGISTRY_URL=http://$VM_HOST:8080 HELPER_URL=http://$VM_HOST:8090
    CP_LABEL=$VM_HOST
fi

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
export COMPOSE_EXTRA="$VISUAL_HELPER_COMPOSE"

cleanup() {
    echo
    if [[ "$KEEP" == 1 ]]; then
        info "Left running (--keep). To remove it later:"
        info "  docker rm -f $SERVER \$(docker ps -aq --filter label=$DEMO_LABEL)"
        [[ "$MODE" == local ]] && info "  docker compose -p $CP_PROJECT -f deploy/docker-compose.yml -f demo/visual/helper/compose.yml down -v"
        return
    fi
    info "Stopping the visual demo..."
    docker rm -f "$SERVER" >/dev/null 2>&1
    remove_devices
    if [[ "$MODE" == local ]]; then
        cp_compose down -v --remove-orphans >/dev/null 2>&1
        ok "Removed the devices and the demo control plane."
    else
        ok "Removed the devices (the control plane on the VM keeps running)."
    fi
}

open_browser() {
    [[ "$OPEN" == 1 ]] || return 0
    if command -v explorer.exe >/dev/null; then explorer.exe "$URL" >/dev/null 2>&1   # WSL
    elif command -v xdg-open >/dev/null; then xdg-open "$URL" >/dev/null 2>&1 &
    elif command -v open >/dev/null; then open "$URL"
    fi
    return 0
}

require_docker
check_no_other_devices
docker rm -f "$SERVER" >/dev/null 2>&1
port_open 127.0.0.1 "$PORT" && die "Port $PORT is in use on this computer; the page needs it."

banner "OTA Update Fleet Manager — visual demo" \
       "$([[ "$MODE" == local ]] && echo "Everything on this computer" || echo "Control plane on the VM ($VM_HOST), devices on this computer")"

if [[ "$MODE" == local ]]; then
    for port in 1883 5432 8080 8090; do
        port_open 127.0.0.1 "$port" && die "Port $port is already in use on this computer.
  If your own control plane or another demo is running, stop it first:  (cd deploy && docker compose down)"
    done
else
    say "Checking the VM:"
    port_open "$VM_HOST" 1883 && ok "MQTT broker        $VM_HOST:1883" \
        || die "MQTT broker $VM_HOST:1883 is not reachable. Is demo/cloud/control-plane.sh running on the VM,
  and does the security group allow TCP 1883 from your IP?"
    port_open "$VM_HOST" 8090 && ok "Visual demo helper $VM_HOST:8090" \
        || die "The visual demo helper at $VM_HOST:8090 is not reachable. Open TCP 8090 to your IP in the
  security group (control-plane.sh starts the helper)."
    port_open "$VM_HOST" 8080 && ok "Software registry  $VM_HOST:8080" \
        || warn "Software registry $VM_HOST:8080 is not reachable: the firmware check will fail."
    if [[ -z "${VISUAL_DEMO_PASSWORD:-}" ]]; then
        read -r -s -p "   Visual demo password (printed by control-plane.sh on the VM): " VISUAL_DEMO_PASSWORD </dev/tty
        echo
    fi
fi

trap cleanup EXIT
# Ctrl+C, closing the terminal, or `kill` all end up in cleanup.
trap 'exit 130' INT
trap 'exit 143' TERM HUP

info "Building images (cached after the first run)..."
docker build -q -t "$DEVICE_IMAGE" "$REPO_ROOT/deploy/device-agent" >/dev/null || die "Could not build the device image."
docker build -q -t "$SERVER_IMAGE" "$DEMO_DIR/visual/server" >/dev/null || die "Could not build the visual demo server image."

if [[ "$MODE" == local ]]; then
    say "Starting the control plane and the demo helper..."
    if ! output=$(cp_compose up -d --build --wait --quiet-pull 2>&1); then
        printf '%s\n' "$output" | tail -n 15 | indent
        die "The control plane did not start."
    fi
    ok "Control plane up."
fi

say "Starting the visual demo server..."
docker run -d --name "$SERVER" --label ota-demo=visual "${HOST_GATEWAY[@]}" \
    -p "127.0.0.1:$PORT:8088" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$REPO_ROOT:/repo:ro" \
    -e MODE="$MODE" -e CP_LABEL="$CP_LABEL" \
    -e MQTT_HOST="$MQTT_HOST" -e REGISTRY_URL="$REGISTRY_URL" \
    -e HELPER_URL="$HELPER_URL" -e HELPER_PASSWORD="$VISUAL_DEMO_PASSWORD" \
    -e DEVICE_IMAGE="$DEVICE_IMAGE" \
    "$SERVER_IMAGE" >/dev/null || die "Could not start the visual demo server."

server_up() { curl -sf "$URL/api/ping" >/dev/null 2>&1 || port_open 127.0.0.1 "$PORT"; }
if ! wait_until 30 "waiting for the page" server_up; then
    docker logs --tail 20 "$SERVER" 2>&1 | indent
    die "The visual demo server did not start."
fi
ok "Visual demo ready."
echo
printf '      %s%s%s\n\n' "$BOLD" "$URL" "$RESET"
info "Use the guided steps at the bottom of the page (← → and Enter), or the buttons."
info "Press Ctrl+C here to stop the demo and clean up."
open_browser

# Watch the server; a brief stop (docker restart) is tolerated, a real exit ends the demo.
down=0
while ((down < 3)); do
    if docker ps -q --filter "name=^${SERVER}$" | grep -q .; then down=0; else down=$((down + 1)); fi
    sleep 2
done
warn "The visual demo server stopped unexpectedly. Its last log lines:"
docker logs --tail 20 "$SERVER" 2>&1 | indent
