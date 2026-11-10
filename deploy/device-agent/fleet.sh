#!/usr/bin/env bash
# Fleet launcher: start, inspect and stop a fleet of simulated devices, one container each.
#
#   ./fleet.sh up [N]           start device-001 … device-N (default 10); the fleet becomes N devices
#   ./fleet.sh status           each device: container state and broker connection
#   ./fleet.sh stop  DEVICE_ID  stop one device (it goes offline after the heartbeat timeout)
#   ./fleet.sh start DEVICE_ID  start it again
#   ./fleet.sh logs  DEVICE_ID  show a device's log (add -f to follow)
#   ./fleet.sh stats            sample CPU and memory per device and in total, saved as CSV
#   ./fleet.sh down             remove the whole fleet
#
# Options for up:
#   --host H        broker address (default: MQTT_HOST in .env, else host.docker.internal)
#   --port P        broker port (default: MQTT_PORT in .env, else 1883)
#   --version V     firmware version the devices report (default 1.0)
#   --batch B       devices started per batch (default 5)
#   --pause S       seconds between batches (default 2)
#   --force         allow more than 50 devices
#   --no-build      do not rebuild the device image first
# Options for stats:
#   --duration S    how long to sample (default 60)   --interval S   seconds between samples (default 5)
#   --out FILE      CSV file (default fleet-stats/fleet-stats-<time>.csv)
#
# Settings are read from .env in this folder (the same file docker-compose.yml uses).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE=ota-device-agent
LABEL_KEY=ota-fleet LABEL_VALUE=device
PREFIX=ota-fleet-
MAX_DEVICES=50
DEVICE_ID_RE='^device-[0-9]{3,}$'

if [[ -t 1 ]]; then
    BOLD=$'\e[1m' DIM=$'\e[2m' RED=$'\e[31m' GREEN=$'\e[32m' YELLOW=$'\e[33m' RESET=$'\e[0m'
else
    BOLD="" DIM="" RED="" GREEN="" YELLOW="" RESET=""
fi
ok()   { printf '%s✔%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
info() { printf '%s%s%s\n' "$DIM" "$*" "$RESET"; }
die()  { printf '%s✘ %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }
usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; }

# Read KEY=VALUE lines from .env without executing it; the environment wins over the file.
load_env() {
    local key value
    [[ -f "$HERE/.env" ]] || return 0
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
        value=${value%%#*}; value=${value%"${value##*[![:space:]]}"}
        value=${value#\"}; value=${value%\"}
        [[ -z "${!key:-}" ]] && export "$key=$value"
    done <"$HERE/.env"
}

container_name() { printf '%s%s' "$PREFIX" "$1"; }
device_id_of()   { printf 'device-%03d' "$1"; }

fleet_containers() {        # names of every fleet container, sorted
    docker ps -a --filter "label=$LABEL_KEY=$LABEL_VALUE" --format '{{.Names}}' | sort
}

state_of() {
    if [[ -z "$(docker ps -aq --filter "name=^$1\$")" ]]; then
        echo missing
    else
        docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null
    fi
}

# Connected if the last connection-related log line is a successful connect.
connection_of() {
    local last
    last=$(docker logs "$1" 2>&1 | grep -E "Connected to broker|Lost connection|Cannot reach broker|refused" | tail -n 1)
    case "$last" in
        *"Connected to broker"*) echo connected ;;
        "") echo "not yet" ;;
        *) echo disconnected ;;
    esac
}

require_docker() {
    command -v docker >/dev/null || die "Docker is not installed."
    docker info >/dev/null 2>&1 || die "Docker is not running."
}

# Devices from other launch paths would share MQTT client IDs with the fleet, and the broker
# keeps disconnecting both.
check_conflicts() {
    local other
    other=$(docker ps --filter label=ota-demo=device --format '{{.Names}}')
    other+=$(docker ps --filter label=com.docker.compose.project=ota-fleet-devices --format ' {{.Names}}')
    if [[ -n "${other// /}" ]]; then
        die "Other simulated devices are running and would clash with the fleet's device IDs:
  $other
  Stop them first (demo: finish or Ctrl+C the demo; compose: docker compose down in this folder)."
    fi
}

valid_id() { [[ "$1" =~ $DEVICE_ID_RE ]] || die "Device IDs look like device-001 (got '$1')."; }

# --------------------------------------------------------------------------- up

cmd_up() {
    local count=10 batch=5 pause=2 force=0 build=1
    local version=${FIRMWARE_VERSION:-1.0}
    local host=${MQTT_HOST:-host.docker.internal} port=${MQTT_PORT:-1883}
    while (($#)); do
        case "$1" in
            --host) host=$2; shift ;;
            --port) port=$2; shift ;;
            --version) version=$2; shift ;;
            --batch) batch=$2; shift ;;
            --pause) pause=$2; shift ;;
            --force) force=1 ;;
            --no-build) build=0 ;;
            [0-9]*) count=$1 ;;
            *) die "Unknown option for up: $1" ;;
        esac
        shift
    done
    [[ "$count" =~ ^[0-9]+$ ]] && ((count >= 1)) || die "The fleet size must be a positive number."
    ((count <= MAX_DEVICES || force)) || die "More than $MAX_DEVICES devices needs --force (NF-4 targets 10 to 50)."
    [[ "$batch" =~ ^[0-9]+$ ]] && ((batch >= 1)) || die "--batch must be a positive number."

    check_conflicts
    if ((build)); then
        info "Building the device image ($IMAGE)..."
        docker build -q -t "$IMAGE" "$HERE" >/dev/null || die "Could not build the device image."
    fi

    # The fleet becomes exactly N devices: remove any above N.
    local name n removed=()
    for name in $(fleet_containers); do
        n=$((10#${name##*-}))
        if ((n > count)); then
            docker rm -f "$name" >/dev/null && removed+=("${name#"$PREFIX"}")
        fi
    done
    ((${#removed[@]})) && info "Removed ${#removed[@]} device(s) above $count: ${removed[*]}"

    printf '%sStarting %d devices%s (broker %s:%s, firmware %s, %d per batch)\n' \
        "$BOLD" "$count" "$RESET" "$host" "$port" "$version" "$batch"
    local i id started=0 in_batch=0 state
    for ((i = 1; i <= count; i++)); do
        id=$(device_id_of "$i")
        name=$(container_name "$id")
        state=$(state_of "$name")
        case "$state" in
            running) continue ;;
            missing)
                docker run -d --name "$name" --label "$LABEL_KEY=$LABEL_VALUE" \
                    --restart unless-stopped \
                    --add-host host.docker.internal:host-gateway \
                    --log-opt max-size=5m --log-opt max-file=2 \
                    -e DEVICE_ID="$id" -e MQTT_HOST="$host" -e MQTT_PORT="$port" \
                    -e FIRMWARE_VERSION="$version" \
                    -e HEARTBEAT_INTERVAL_SECONDS="${HEARTBEAT_INTERVAL_SECONDS:-5}" \
                    -e LOG_LEVEL="${LOG_LEVEL:-INFO}" \
                    "$IMAGE" >/dev/null || die "Could not start $id."
                ;;
            *) docker start "$name" >/dev/null || die "Could not start $id." ;;
        esac
        started=$((started + 1)); in_batch=$((in_batch + 1))
        if ((in_batch == batch && i < count)); then
            info "  started up to $id; pausing ${pause}s"
            sleep "$pause"
            in_batch=0
        fi
    done
    ok "$started device(s) started, $((count - started)) already running"

    # Wait for every device to connect to the broker.
    local deadline=$((SECONDS + 30)) connected
    while :; do
        connected=0
        for ((i = 1; i <= count; i++)); do
            [[ "$(connection_of "$(container_name "$(device_id_of "$i")")")" == connected ]] \
                && connected=$((connected + 1))
        done
        ((connected == count || SECONDS >= deadline)) && break
        sleep 2
    done
    if ((connected == count)); then
        ok "All $count devices connected to the broker at $host:$port"
    else
        warn "$connected of $count devices connected after 30 s. Not connected:"
        for ((i = 1; i <= count; i++)); do
            name=$(container_name "$(device_id_of "$i")")
            [[ "$(connection_of "$name")" == connected ]] && continue
            printf '  %s: %s\n' "$(device_id_of "$i")" "$(docker logs --tail 1 "$name" 2>&1)"
        done
        exit 1
    fi
}

# --------------------------------------------------------------------------- inspect

cmd_status() {
    local names name running=0 connected=0 total=0 state conn
    names=$(fleet_containers)
    if [[ -z "$names" ]]; then
        info "No fleet devices. Start some with: ./fleet.sh up 10"
        return 0
    fi
    printf '%-14s %-10s %-13s %s\n' DEVICE CONTAINER BROKER STARTED
    for name in $names; do
        total=$((total + 1))
        state=$(state_of "$name")
        conn="-"
        if [[ "$state" == running ]]; then
            running=$((running + 1))
            conn=$(connection_of "$name")
            [[ "$conn" == connected ]] && connected=$((connected + 1))
        fi
        printf '%-14s %-10s %-13s %s\n' "${name#"$PREFIX"}" "$state" "$conn" \
            "$(docker inspect -f '{{.State.StartedAt}}' "$name" | cut -c1-19 | tr T ' ')"
    done
    echo
    printf '%d devices: %d running, %d connected\n' "$total" "$running" "$connected"
}

cmd_logs() {
    [[ $# -ge 1 ]] || die "Usage: ./fleet.sh logs DEVICE_ID [-f]"
    valid_id "$1"
    local name; name=$(container_name "$1")
    [[ "$(state_of "$name")" != missing ]] || die "$1 is not part of the fleet."
    shift
    docker logs --tail 50 "$@" "$name"
}

cmd_stop_start() {
    local action=$1 id=${2:-}
    [[ -n "$id" ]] || die "Usage: ./fleet.sh $action DEVICE_ID"
    valid_id "$id"
    local name; name=$(container_name "$id")
    [[ "$(state_of "$name")" != missing ]] || die "$id is not part of the fleet."
    docker "$action" "$name" >/dev/null || die "Could not $action $id."
    if [[ "$action" == stop ]]; then
        ok "$id stopped; the orchestrator marks it offline after the heartbeat timeout (30 s)"
    else
        ok "$id started; it reports in with its next heartbeat"
    fi
}

cmd_down() {
    local names
    names=$(fleet_containers)
    if [[ -z "$names" ]]; then
        info "No fleet devices to remove."
        return 0
    fi
    # shellcheck disable=SC2086
    docker rm -f $names >/dev/null
    ok "Removed $(wc -w <<<"$names") device(s)"
}

# --------------------------------------------------------------------------- stats

# MemUsage like "24.5MiB / 7.6GiB" -> MiB
to_mib() {
    awk -v m="${1%% *}" 'BEGIN {
        v = m + 0; u = m; sub(/^[0-9.]+/, "", u)
        if (u == "KiB" || u == "kB") v /= 1024
        else if (u == "GiB" || u == "GB") v *= 1024
        else if (u == "B") v /= 1048576
        printf "%.1f", v }'
}

cmd_stats() {
    local duration=60 interval=5 out=""
    while (($#)); do
        case "$1" in
            --duration) duration=$2; shift ;;
            --interval) interval=$2; shift ;;
            --out) out=$2; shift ;;
            *) die "Unknown option for stats: $1" ;;
        esac
        shift
    done
    local names
    names=$(docker ps --filter "label=$LABEL_KEY=$LABEL_VALUE" --format '{{.Names}}' | sort)
    [[ -n "$names" ]] || die "No running fleet devices to measure."
    local count; count=$(wc -w <<<"$names")
    if [[ -z "$out" ]]; then
        mkdir -p "$HERE/fleet-stats"
        out="$HERE/fleet-stats/fleet-stats-$(date -u +%Y%m%dT%H%M%SZ)-${count}dev.csv"
    fi
    echo "timestamp,device,cpu_percent,mem_mib" >"$out"

    printf '%sSampling %d devices%s every %ss for %ss (host: %s CPUs, %s MiB RAM)\n' "$BOLD" "$count" \
        "$RESET" "$interval" "$duration" "$(nproc)" "$(free -m | awk '/^Mem:/ {print $2}')"
    local end=$((SECONDS + duration)) samples=0 ts line dev cpu mem total_cpu total_mem
    local sum_cpu=0 sum_mem=0 peak_cpu=0 peak_mem=0
    while ((SECONDS < end)); do
        ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        total_cpu=0 total_mem=0
        # shellcheck disable=SC2086
        while IFS=',' read -r dev cpu mem; do
            cpu=${cpu%\%}
            mem=$(to_mib "$mem")
            echo "$ts,${dev#"$PREFIX"},$cpu,$mem" >>"$out"
            total_cpu=$(awk -v a="$total_cpu" -v b="$cpu" 'BEGIN {printf "%.2f", a + b}')
            total_mem=$(awk -v a="$total_mem" -v b="$mem" 'BEGIN {printf "%.1f", a + b}')
        done < <(docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' $names)
        samples=$((samples + 1))
        sum_cpu=$(awk -v a="$sum_cpu" -v b="$total_cpu" 'BEGIN {printf "%.2f", a + b}')
        sum_mem=$(awk -v a="$sum_mem" -v b="$total_mem" 'BEGIN {printf "%.1f", a + b}')
        peak_cpu=$(awk -v a="$peak_cpu" -v b="$total_cpu" 'BEGIN {print (b > a ? b : a)}')
        peak_mem=$(awk -v a="$peak_mem" -v b="$total_mem" 'BEGIN {print (b > a ? b : a)}')
        printf '  %s  total CPU %6s%%   total memory %7s MiB\n' "${ts:11:8}" "$total_cpu" "$total_mem"
        sleep "$interval"
    done

    echo
    awk -v n="$samples" -v c="$count" -v sc="$sum_cpu" -v sm="$sum_mem" -v pc="$peak_cpu" -v pm="$peak_mem" 'BEGIN {
        printf "Devices: %d   samples: %d\n", c, n
        printf "Total CPU:    average %.2f%%, peak %.2f%%   (100%% = one CPU core)\n", sc / n, pc
        printf "Total memory: average %.1f MiB, peak %.1f MiB   (%.1f MiB per device on average)\n", sm / n, pm, sm / n / c
    }'
    ok "Samples saved to ${out#"$PWD/"}"
}

# --------------------------------------------------------------------------- main

load_env
cmd=${1:-}
[[ $# -gt 0 ]] && shift
case "$cmd" in
    up) require_docker; cmd_up "$@" ;;
    status) require_docker; cmd_status ;;
    stop|start) require_docker; cmd_stop_start "$cmd" "$@" ;;
    logs) require_docker; cmd_logs "$@" ;;
    stats) require_docker; cmd_stats "$@" ;;
    down) require_docker; cmd_down ;;
    -h|--help|help|"") usage ;;
    *) echo "Unknown command: $cmd" >&2; usage; exit 2 ;;
esac
