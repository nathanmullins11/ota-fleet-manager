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
DEVICE_COUNT=${DEVICE_COUNT:-5}
REGISTRY_REACHABLE=${REGISTRY_REACHABLE:-1}
SIMULATE=${SIMULATE:-0}

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
    # DEVICE_AGENT_DIR builds the devices from another folder, for example a work-in-progress
    # agent, so the demo's checks can be run against it.
    docker build -q -t "$DEVICE_IMAGE" "${DEVICE_AGENT_DIR:-$REPO_ROOT/deploy/device-agent}" >/dev/null \
        || die "Could not build the device agent image."
    docker build -q -t "$TOOLS_IMAGE" -f "$DEMO_DIR/lib/tools.Dockerfile" "$REPO_ROOT/tools" >/dev/null \
        || die "Could not build the demo tools image."
    docker image inspect "$MQTT_IMAGE" >/dev/null 2>&1 || docker pull -q "$MQTT_IMAGE" >/dev/null
}

# A device from deploy/device-agent's own compose stack would share an MQTT client ID
# with a demo device, and the broker would keep disconnecting one of them.
check_no_other_devices() {
    if [[ -n "$(docker ps -q --filter label=com.docker.compose.project=ota-fleet-devices)" ]]; then
        die "Devices from deploy/device-agent's Compose file are running and would clash with the demo devices.
  Stop them first:  (cd deploy/device-agent && docker compose down)"
    fi
    local fleet
    fleet=$(docker ps -q --filter label=ota-fleet=device | wc -l)
    if ((fleet > 0)); then
        die "$fleet device(s) from the fleet launcher are running. They share device IDs with the demo's
  devices and would join its rollouts. Remove them first:  deploy/device-agent/fleet.sh down"
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

# ----------------------------------------------------------------------------- resource use
# show_resources "when": CPU, memory and network traffic of the demo's device containers
# (docker stats on this computer), and of the control plane when it runs here too.
# Network in/out counts since each container started: it grows as messages flow.
STATS_AWK='
function mib(s,  v, u) { v = s + 0; u = s; sub(/^[0-9.]+/, "", u)
    if (u ~ /^k/ || u ~ /^K/) v /= 1024; else if (u ~ /^G/) v *= 1024; else if (u == "B") v /= 1048576
    return v }
function kb(s,  v, u) { v = s + 0; u = s; sub(/^[0-9.]+/, "", u)
    if (u ~ /^M/) v *= 1000; else if (u ~ /^G/) v *= 1000000; else if (u == "B") v /= 1000
    return v }
'
show_resources() {
    local when=$1 names cp_ids
    names=$(docker ps --filter "label=$DEMO_LABEL" --format '{{.Names}}' | grep -E '^ota-demo-device-' | sort)
    [[ -n "$names" ]] || return 0
    printf '   %sResource use%s %s(%s · docker stats on this computer)%s\n' "$BOLD" "$RESET" "$DIM" "$when" "$RESET"
    # shellcheck disable=SC2086
    docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.NetIO}}' $names | sort \
        | awk -F'|' "$STATS_AWK"'
            { split($3, m, " / "); split($4, n, " / "); cpu = $2 + 0
              name = $1; sub(/^ota-demo-/, "", name)
              printf "      %-12s CPU %5.2f%%   MEM %6.1f MiB   NET in %7.1f kB / out %7.1f kB\n", name, cpu, mib(m[1]), kb(n[1]), kb(n[2])
              tc += cpu; tm += mib(m[1]); c++ }
            END { if (c) printf "      %-12s CPU %5.2f%%   MEM %6.1f MiB   (%.1f MiB per device)\n", c " devices", tc, tm, tm / c }'
    if [[ "$CP_LOCAL" == 1 ]]; then
        cp_ids=$(cp_compose ps -q 2>/dev/null)
        if [[ -n "$cp_ids" ]]; then
            # shellcheck disable=SC2086
            docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}' $cp_ids | sort \
                | awk -F'|' "$STATS_AWK"'
                    { split($3, m, " / "); name = $1
                      if (match(name, /software-registry|orchestrator|mosquitto|postgres/)) name = substr(name, RSTART, RLENGTH)
                      line = line sprintf("%s%s %.1f%% %.0f MiB", (line ? " · " : ""), name, $2 + 0, mib(m[1])) }
                    END { if (line) printf "      control plane: %s\n", line }'
        fi
    fi
}

# ----------------------------------------------------------------------------- device listener
# One MQTT listener records every device's heartbeats, events and commands (with a time
# stamp), so steps can check what devices actually did, in local and cloud mode alike.
# Lines look like: 2026-11-07T10:00:00+0000 ota/devices/device-001/heartbeat {...}
LISTENER=ota-demo-listener
start_listener() {
    docker rm -f "$LISTENER" >/dev/null 2>&1 || true
    # Restarted automatically if the broker connection drops (logs survive restarts).
    docker run -d --name "$LISTENER" --label "$DEMO_LABEL" --restart unless-stopped \
        "${HOST_GATEWAY[@]}" "$MQTT_IMAGE" \
        mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" -q 1 -i ota-demo-listener -F '%I %t %p' \
        -t 'ota/devices/+/heartbeat' -t 'ota/devices/+/events' -t 'ota/devices/+/commands' \
        >/dev/null
}
listener_lines() { docker logs "$LISTENER" 2>/dev/null; }
listener_running() { [[ "$(docker inspect -f '{{.State.Running}}' "$LISTENER" 2>/dev/null)" == true ]]; }
# A marker: later queries can look only at what arrived after it.
listener_mark() { listener_lines | wc -l; }
lines_since() { listener_lines | tail -n +"$(( ${1:-0} + 1 ))"; }

# json_field KEY: the value of a top-level string or number field from a JSON line on stdin.
json_field() { sed -n "s/.*\"$1\": *\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" | tail -n 1; }

# hb_field MARK DEVICE KEY: a field of DEVICE's latest heartbeat since MARK.
hb_field() { lines_since "$1" | grep " ota/devices/$2/heartbeat " | tail -n 1 | json_field "$3"; }
hb_is()    { [[ "$(hb_field "$1" "$2" "$3")" == "$4" ]]; }
# Any heartbeat since MARK with KEY equal to / above a value (errors and crashes come and go).
hb_values() { lines_since "$1" | grep " ota/devices/$2/heartbeat " \
                | sed -n "s/.*\"$3\": *\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p"; }
hb_ever_is()    { hb_values "$1" "$2" "$3" | grep -qx "$4"; }
hb_ever_above() { hb_values "$1" "$2" "$3" | awk -v n="$4" '$1 + 0 > n { f = 1 } END { exit !f }'; }

# event_line MARK DEVICE EVENT [ROLLOUT_ID]: the first such event since MARK.
event_line() {
    lines_since "$1" | grep " ota/devices/$2/events " | grep "\"event\": *\"$3\"" \
        | { if [[ -n "${4:-}" ]]; then grep "\"rollout_id\": *$4[,}]"; else cat; fi; } | head -n 1
}
event_seen() { [[ -n "$(event_line "$@")" ]]; }

# commands_seen KIND [ROLLOUT_ID]: listener lines for update or rollback commands.
commands_seen() {
    listener_lines | grep " ota/devices/[^/]*/commands " | grep "\"command\": \"$1\"" \
        | { if [[ -n "${2:-}" ]]; then grep "\"rollout_id\": $2,"; else cat; fi; } || true
}
command_seen() { [[ -n "$(commands_seen "$@")" ]]; }
latest_rollout_id() { commands_seen "$1" | tail -n 1 | grep -o '"rollout_id": [0-9]*' | grep -o '[0-9]*$'; }
commanded_devices() { commands_seen "$1" "$2" | cut -d' ' -f2 | cut -d/ -f3 | sort -u; }
# all_commanded KIND ROLLOUT_ID DEVICE...: every listed device received the command.
all_commanded() {
    local kind=$1 rid=$2 d got
    shift 2
    got=$(commanded_devices "$kind" "$rid")
    for d in "$@"; do grep -qx "$d" <<<"$got" || return 1; done
}

# Pretty-print one listener line's payload.
show_command() {
    sed -e 's/^[^ ]* [^ ]* //' -e 's/^{/{\n  /' -e 's/, "/,\n  "/g' -e 's/}$/\n}/' | indent
}

# What a device has been seen doing since MARK, for NOT WORKING messages.
observed() {
    local mark=$1 d=$2 hb events cmds
    hb=$(lines_since "$mark" | grep " ota/devices/$d/heartbeat " | tail -n 1)
    if [[ -n "$hb" ]]; then
        hb="heartbeat says version $(json_field version <<<"$hb"), $(json_field status <<<"$hb"), errors $(json_field error_count <<<"$hb"), crashes $(c=$(json_field crash_count <<<"$hb"); echo "${c:-not reported}")"
    else
        hb="no heartbeat"
    fi
    events=$(lines_since "$mark" | grep " ota/devices/$d/events " | sed -n 's/.*"event": *"\([^"]*\)".*/\1/p' | tr '\n' ' ')
    cmds=$(lines_since "$mark" | grep -c " ota/devices/$d/commands ")
    printf '%s; commands received: %s; events sent: %s' "$hb" "$cmds" "${events:-none}"
}

# ----------------------------------------------------------------------------- checks
# Each check is recorded for the summary. A failed check prints NOT WORKING in red with what
# was seen instead, and the demo carries on.
CHECKS=()
check_pass() { CHECKS+=("pass|$1"); ok "$1"; }
check_fail() {
    CHECKS+=("fail|$1")
    printf '   %s%s✘ NOT WORKING: %s%s\n' "$BOLD" "$RED" "$1" "$RESET"
    [[ -n "${2:-}" ]] && printf '     %sseen: %s%s\n' "$RED" "$2" "$RESET"
    return 0
}
check_skip() { CHECKS+=("skip|$1"); printf '   %s– skipped: %s (%s)%s\n' "$YELLOW" "$1" "$2" "$RESET"; }

# Devices that have not answered a command once get short waits afterwards, so a missing
# feature costs one full timeout, not one per check.
DEVICES_SILENT=0
event_timeout() { if [[ "$DEVICES_SILENT" == 1 ]]; then echo 8; else echo 40; fi; }

# expect TIMEOUT "check" MARK DEVICE command [args...]: pass if the command succeeds in time.
expect() {
    local timeout=$1 what=$2 mark=$3 dev=$4
    shift 4
    if wait_until "$timeout" "waiting: $what" "$@"; then
        check_pass "$what (${WAITED}s)"
        return 0
    fi
    check_fail "$what (nothing within ${timeout}s)" "$(observed "$mark" "$dev")"
    return 1
}

# begin_rollout VERSION KEY "what" CLI_ARGS...: start a rollout. Local: through the CLI.
# Cloud: the presenter presses KEY in the VM's live view. Sets RID; returns 1 on failure.
begin_rollout() {
    local version=$1 key=$2 what=$3 mark out
    shift 3
    RID=""
    mark=$(listener_mark)
    if [[ "$CP_LOCAL" == 1 ]]; then
        wait_rollouts_idle || return 1
        run_note "python cli.py rollout start $version $*"
        out=$(cli rollout start "$version" "$@" 2>&1)
        printf '%s\n' "$out" | grep -v -E "^$|Follow it|cli.py rollout status" | indent
        if ! grep -q "^Rollout [0-9]* created" <<<"$out"; then
            check_fail "the orchestrator did not start the rollout of $version" "$(tail -n 1 <<<"$out")"
            return 1
        fi
    else
        look_at_vm "press ${BOLD}$key${RESET}${CYAN} in the live view ($what)."
    fi
    # Cloud: the presenter has to press the key first, so wait longer.
    local wait=30
    [[ "$CP_LOCAL" == 1 ]] || wait=180
    if ! wait_until "$wait" "waiting for the orchestrator's update commands" \
            new_command "$mark" update "$version"; then
        check_fail "no update command for $version arrived" \
                   "an earlier rollout may still be active (cli.py rollout list on the control plane)"
        return 1
    fi
    sleep 2      # let the commands to every target arrive
    RID=$(lines_since "$mark" | grep " ota/devices/[^/]*/commands " | grep '"command": "update"' \
          | tail -n 1 | grep -o '"rollout_id": [0-9]*' | grep -o '[0-9]*$')
    check_pass "orchestrator sent update commands for $version (rollout $RID) to: $(commanded_devices update "$RID" | tr '\n' ' ')"
}

# begin_rollback RID: roll a rollout back (local: CLI; cloud: key b on the VM).
begin_rollback() {
    local rid=$1 mark out
    mark=$(listener_mark)
    if [[ "$CP_LOCAL" == 1 ]]; then
        run_note "python cli.py rollout rollback $rid"
        out=$(cli rollout rollback "$rid" --reason "demo: roll back rollout $rid" 2>&1)
        printf '%s\n' "$out" | head -n 1 | indent
        if ! grep -q "requested" <<<"$out"; then
            check_fail "the orchestrator did not accept the rollback of rollout $rid" "$(tail -n 1 <<<"$out")"
            return 1
        fi
    else
        look_at_vm "press ${BOLD}b${RESET}${CYAN} in the live view to roll back rollout $rid."
    fi
    local wait=30
    [[ "$CP_LOCAL" == 1 ]] || wait=180
    if ! wait_until "$wait" "waiting for the orchestrator's rollback commands" \
            new_command "$mark" rollback "" "$rid"; then
        check_fail "no rollback command for rollout $rid arrived" ""
        return 1
    fi
    sleep 2
    check_pass "orchestrator sent rollback commands (rollout $rid) to: $(commanded_devices rollback "$rid" | tr '\n' ' ')"
}

# new_command MARK KIND [VERSION] [ROLLOUT_ID]: such a command arrived since MARK.
new_command() {
    lines_since "$1" | grep " ota/devices/[^/]*/commands " | grep "\"command\": \"$2\"" \
        | { if [[ -n "${3:-}" ]]; then grep "\"target_version\": \"$3\""; else cat; fi; } \
        | { if [[ -n "${4:-}" ]]; then grep "\"rollout_id\": $4,"; else cat; fi; } | grep -q .
}

rollout_state() { cp_psql -Atc "SELECT state FROM rollouts WHERE id = $1;"; }
rollout_state_is() { [[ "$(rollout_state "$1")" == "$2" ]]; }

# event_reason MARK DEVICE EVENT ROLLOUT_ID: the reason field of that event.
event_reason() { event_line "$@" | sed -n 's/.*"reason": *"\([^"]*\)".*/\1/p'; }

# Local mode: wait until no rollout is active (a rollout whose devices never answered ends
# after the orchestrator's command timeout).
rollouts_active() {
    (( $(cp_psql -Atc "SELECT count(*) FROM rollouts WHERE state IN ('pending','in_progress','halted');") > 0 ))
}
rollouts_idle() { ! rollouts_active; }
wait_rollouts_idle() {
    [[ "$CP_LOCAL" == 1 ]] || return 0
    rollouts_active || return 0
    info "An earlier rollout is still waiting for device replies; it ends at the orchestrator's command timeout."
    if ! wait_until 90 "waiting for the earlier rollout to finish" rollouts_idle; then
        check_fail "earlier rollout did not finish, so a new one cannot start" ""
        return 1
    fi
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
