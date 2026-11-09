# Shared helpers for the demo scripts. Sourced by demo/local/demo.sh and demo/cloud/*.sh.
#
# Settings the entry scripts provide before sourcing the steps:
#   CP_LOCAL       1 if the control plane runs on this machine (we can query it), else 0
#   CP_PROJECT     compose project name of the control plane (empty: the compose file's own)
#   MQTT_HOST      broker address as seen from a container on this machine
#   REGISTRY_URL   software registry base URL as seen from a container on this machine
#   AUTO           1 to run without waiting for Enter

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEMO_DIR="$REPO_ROOT/demo"
COMPOSE_FILE="$REPO_ROOT/deploy/docker-compose.yml"

AUTO=${AUTO:-0}
CP_LOCAL=${CP_LOCAL:-1}
CP_PROJECT=${CP_PROJECT:-}
MQTT_PORT=${MQTT_PORT:-1883}
DEVICE_COUNT=${DEVICE_COUNT:-3}
REGISTRY_REACHABLE=${REGISTRY_REACHABLE:-1}

DEVICE_IMAGE=ota-demo-device-agent
TOOLS_IMAGE=ota-demo-tools
MQTT_IMAGE=eclipse-mosquitto:2
DEMO_LABEL=ota-demo=device
# Lets a container reach ports published on this machine (Linux, WSL, Docker Desktop).
HOST_GATEWAY=(--add-host host.docker.internal:host-gateway)

# --------------------------------------------------------------------------- output

if [[ -t 1 ]]; then
    BOLD=$'\e[1m' DIM=$'\e[2m' RED=$'\e[31m' GREEN=$'\e[32m' YELLOW=$'\e[33m'
    BLUE=$'\e[34m' CYAN=$'\e[36m' RESET=$'\e[0m'
else
    BOLD="" DIM="" RED="" GREEN="" YELLOW="" BLUE="" CYAN="" RESET=""
fi

STEP_NO=0

banner() {
    local line
    line=$(printf '═%.0s' $(seq 1 72))
    printf '\n%s%s%s\n' "$CYAN" "$line" "$RESET"
    printf '%s  %s%s\n' "$BOLD" "$1" "$RESET"
    [[ -n "${2:-}" ]] && printf '  %s%s%s\n' "$DIM" "$2" "$RESET"
    printf '%s%s%s\n' "$CYAN" "$line" "$RESET"
}

# step "Title" "Requirements covered"
step() {
    STEP_NO=$((STEP_NO + 1))
    printf '\n%s━━ Step %d: %s%s\n' "$BOLD$BLUE" "$STEP_NO" "$1" "$RESET"
    [[ -n "${2:-}" ]] && printf '   %sCovers: %s%s\n' "$DIM" "$2" "$RESET"
    echo
}

say()  { printf '   %s\n' "$*"; }
info() { printf '   %s%s%s\n' "$DIM" "$*" "$RESET"; }
ok()   { printf '   %s✔ %s%s\n' "$GREEN" "$*" "$RESET"; }
bad()  { printf '   %s✘ %s%s\n' "$RED" "$*" "$RESET"; }
warn() { printf '   %s! %s%s\n' "$YELLOW" "$*" "$RESET"; }
run_note() { printf '   %s$ %s%s\n' "$DIM" "$*" "$RESET"; }
die()  { printf '\n%s✘ %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }

# Point the audience at the VM terminal (cloud mode).
look_at_vm() { printf '   %s👉 On the VM terminal: %s%s\n' "$CYAN" "$*" "$RESET"; }

# Indent command output so it reads as part of the step.
indent() { sed 's/^/      /'; }

pause() {
    if [[ "$AUTO" == 1 ]]; then
        sleep "${1:-2}"
    else
        printf '\n   %s[Enter] to continue%s ' "$DIM" "$RESET"
        read -r _ </dev/tty
    fi
}

# countdown SECONDS "message"
countdown() {
    local n=$1
    while ((n > 0)); do
        [[ -t 1 ]] && printf '\r   %s%s: %2ds %s' "$DIM" "$2" "$n" "$RESET"
        sleep 1
        n=$((n - 1))
    done
    [[ -t 1 ]] && printf '\r\033[K'
    return 0
}

# wait_until TIMEOUT "message" command [args...]
# Polls every second, showing elapsed time. Sets WAITED to the seconds taken.
wait_until() {
    local timeout=$1 message=$2 start
    shift 2
    start=$SECONDS
    while ! "$@" >/dev/null 2>&1; do
        WAITED=$((SECONDS - start))
        if ((WAITED >= timeout)); then
            [[ -t 1 ]] && printf '\r\033[K'
            return 1
        fi
        [[ -t 1 ]] && printf '\r   %s%s ... %ds%s' "$DIM" "$message" "$WAITED" "$RESET"
        sleep 1
    done
    WAITED=$((SECONDS - start))
    [[ -t 1 ]] && printf '\r\033[K'
    return 0
}

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# port_open HOST PORT: true if something accepts TCP connections there.
port_open() { timeout 3 bash -c "</dev/tcp/$1/$2" 2>/dev/null; }

require_docker() {
    command -v docker >/dev/null || die "Docker is not installed."
    docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker Desktop (or the Docker service) and try again."
    docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (docker compose ...)."
}

# --------------------------------------------------------------------------- control plane

cp_compose() {
    if [[ -n "$CP_PROJECT" ]]; then
        docker compose -p "$CP_PROJECT" -f "$COMPOSE_FILE" "$@"
    else
        docker compose -f "$COMPOSE_FILE" "$@"
    fi
}

# psql inside the postgres container, using the container's own user and database names.
cp_psql() {
    cp_compose exec -T postgres sh -c 'psql -X -P footer=off -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' psql "$@"
}

show_fleet() {
    cp_psql -c "SELECT device_id, current_version AS version, status,
                       error_count AS errors, crash_count AS crashes,
                       seconds_since_heartbeat AS last_seen_s
                  FROM fleet_overview ORDER BY device_id;" | sed '/^$/d' | indent
}

device_status() { cp_psql -Atc "SELECT status FROM devices WHERE device_id = '$1';"; }

# status_is DEVICE STATUS: for wait_until.
status_is() { [[ "$(device_status "$1")" == "$2" ]]; }

devices_with_status() { cp_psql -Atc "SELECT count(*) FROM devices WHERE status = '$1';"; }
at_least_healthy() { (( $(devices_with_status healthy) >= $1 )); }

# orchestrator_log SINCE PATTERN: orchestrator log lines since a timestamp matching a pattern.
orchestrator_log() {
    cp_compose logs --no-log-prefix --since "$1" orchestrator 2>/dev/null | grep -E "$2" || true
}

# Applies every migration in order. Each one must be safe to run more than once.
apply_migrations() {
    local f
    for f in "$REPO_ROOT"/deploy/registries/postgres/migrations/*.sql; do
        [[ -e "$f" ]] || continue
        cp_compose exec -T postgres sh -c \
            'psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <"$f" 2>&1 \
            | grep -v -E "already exists, skipping|^$" || true
        info "migration applied: $(basename "$f")"
    done
}

# --------------------------------------------------------------------------- devices

device_id()   { printf 'device-%03d' "$1"; }
device_name() { printf 'ota-demo-device-%03d' "$1"; }

build_images() {
    info "Building the device agent and demo tool images (cached after the first run)..."
    docker build -q -t "$DEVICE_IMAGE" "$REPO_ROOT/deploy/device-agent" >/dev/null \
        || die "Could not build the device agent image."
    docker build -q -t "$TOOLS_IMAGE" -f "$DEMO_DIR/lib/tools.Dockerfile" "$REPO_ROOT/tools" >/dev/null \
        || die "Could not build the demo tools image."
    docker image inspect "$MQTT_IMAGE" >/dev/null 2>&1 || docker pull -q "$MQTT_IMAGE" >/dev/null
}

# A device from deploy/device-agent's own compose stack would share an MQTT client ID
# with a demo device, and the broker would keep disconnecting one of them.
check_no_other_devices() {
    local running
    running=$(docker ps -q --filter label=com.docker.compose.project=ota-fleet-devices)
    if [[ -n "$running" ]]; then
        die "Devices from deploy/device-agent are already running and would clash with the demo devices.
  Stop them first:  (cd deploy/device-agent && docker compose down)"
    fi
}

start_device() {
    local i=$1
    docker rm -f "$(device_name "$i")" >/dev/null 2>&1 || true
    docker run -d --name "$(device_name "$i")" --label "$DEMO_LABEL" "${HOST_GATEWAY[@]}" \
        -e DEVICE_ID="$(device_id "$i")" \
        -e MQTT_HOST="$MQTT_HOST" -e MQTT_PORT="$MQTT_PORT" \
        -e FIRMWARE_VERSION=1.0 -e HEARTBEAT_INTERVAL_SECONDS=5 \
        "$DEVICE_IMAGE" >/dev/null
}

device_connected() { docker logs "$(device_name "$1")" 2>&1 | grep -q "Connected to broker"; }

remove_devices() {
    local ids
    ids=$(docker ps -aq --filter "label=$DEMO_LABEL")
    [[ -n "$ids" ]] && docker rm -f $ids >/dev/null
    return 0
}

# mqtt_pub TOPIC PAYLOAD: publish as an outside client, the way a device would.
mqtt_pub() {
    docker run --rm "${HOST_GATEWAY[@]}" "$MQTT_IMAGE" \
        mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -q 1 -i "ota-demo-publisher" -t "$1" -m "$2"
}

# publish_event DEVICE EVENT ROLLOUT_ID VERSION [REASON]: an event as the device would send it.
publish_event() {
    local reason=null
    [[ -n "${5:-}" ]] && reason="\"$5\""
    mqtt_pub "ota/devices/$1/events" \
        "{\"schema_version\":1,\"device_id\":\"$1\",\"event\":\"$2\",\"rollout_id\":$3,\"version\":\"$4\",\"reason\":$reason,\"timestamp\":\"$(now_utc)\"}"
}

# A listener on every device's command topic, so the demo can show the full commands the
# orchestrator sends (the agent's own log shortens them). Removed with the devices.
LISTENER=ota-demo-command-listener
start_command_listener() {
    docker rm -f "$LISTENER" >/dev/null 2>&1 || true
    docker run -d --name "$LISTENER" --label "$DEMO_LABEL" "${HOST_GATEWAY[@]}" "$MQTT_IMAGE" \
        mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" -q 1 -i ota-demo-listener -v \
        -t 'ota/devices/+/commands' >/dev/null
}

# commands_seen KIND [ROLLOUT_ID]: "topic payload" lines for update or rollback commands.
commands_seen() {
    docker logs "$LISTENER" 2>/dev/null | grep "\"command\": \"$1\"" \
        | { if [[ -n "${2:-}" ]]; then grep "\"rollout_id\": $2,"; else cat; fi; } || true
}
command_seen() { [[ -n "$(commands_seen "$@")" ]]; }

# Rollout ID of the latest command of a kind, and the devices it went to.
latest_rollout_id() { commands_seen "$1" | tail -n 1 | grep -o '"rollout_id": [0-9]*' | grep -o '[0-9]*$'; }
commanded_devices() { commands_seen "$1" "$2" | cut -d' ' -f1 | cut -d/ -f3 | sort -u; }

# Pretty-print one command payload.
show_command() {
    sed -e 's/^[^ ]* //' -e 's/^{/{\n  /' -e 's/, "/,\n  "/g' -e 's/}$/\n}/' | indent
}

# cli ARGS...: the orchestrator's rollout CLI (control plane on this machine only).
cli() { cp_compose exec -T orchestrator python cli.py "$@"; }

# tools_run ARGS...: python in the tools image, with the repository mounted read-only.
tools_run() {
    local color=()
    [[ -t 1 ]] && color=(-e FORCE_COLOR=1)
    docker run --rm "${HOST_GATEWAY[@]}" "${color[@]}" -v "$REPO_ROOT:/repo:ro" -w /repo \
        "$TOOLS_IMAGE" python -I "$@"
}

# --------------------------------------------------------------------------- steps

run_steps() {
    local f
    for f in "$DEMO_DIR"/steps/[0-9]*.sh; do
        # shellcheck source=/dev/null
        source "$f"
    done
}
