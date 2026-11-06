#!/usr/bin/env bash
# Cloud demo, part 1 of 2: run on the VM. Updates and starts the control plane, then
# shows a live view of it (services, fleet, crashes, orchestrator decisions) for the
# audience while demo/cloud/devices.sh drives the devices from your computer.
#
#   demo/cloud/control-plane.sh             # git pull, build, start, migrate, live view
#   demo/cloud/control-plane.sh --reset     # also wipe the database first (fresh fleet table)
#   demo/cloud/control-plane.sh --no-pull   # run the code as it is checked out
#   demo/cloud/control-plane.sh --view      # only the live view (stack already running)
#
# Ctrl+C leaves the live view; the control plane keeps running.
set -uo pipefail

PULL=1 RESET=0 VIEW_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --reset) RESET=1 ;;
        --no-pull) PULL=0 ;;
        --view) VIEW_ONLY=1 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
    esac
done

# CP_PROJECT can be set to run a separate copy of the stack (used to test this script).
export CP_LOCAL=1 CP_PROJECT=${CP_PROJECT:-}
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
ENV_FILE="$REPO_ROOT/deploy/.env"
# The visual demo's helper runs next to the control plane (demo/visual/helper/compose.yml).
export COMPOSE_EXTRA="$VISUAL_HELPER_COMPOSE"

public_ip() {
    curl -s -4 --max-time 3 https://checkip.amazonaws.com 2>/dev/null \
        || hostname -I 2>/dev/null | awk '{print $1}'
}

start_control_plane() {
    banner "OTA Update Fleet Manager — control plane (cloud VM)"

    if [[ ! -f "$ENV_FILE" ]]; then
        local pw
        pw=$(head -c 18 /dev/urandom | base64 | tr -d '/+=')
        sed "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$pw/" "$REPO_ROOT/deploy/.env.example" >"$ENV_FILE"
        chmod 600 "$ENV_FILE"
        ok "Created deploy/.env with a random database password."
    fi

    # The visual demo's helper: reachable from outside the VM, protected by a password.
    if ! grep -q "^VISUAL_DEMO_PASSWORD=" "$ENV_FILE"; then
        printf '\n# Set by demo/cloud/control-plane.sh: visual demo helper (port 8090).\nVISUAL_DEMO_PASSWORD=%s\nVISUAL_HELPER_BIND=0.0.0.0\n' \
            "$(head -c 12 /dev/urandom | base64 | tr -d '/+=')" >>"$ENV_FILE"
        ok "Created a visual demo password in deploy/.env"
    fi

    # Update commands tell devices where to download firmware, so the orchestrator needs
    # the registry's public address. Set it once if .env does not have it.
    if ! grep -q "^REGISTRY_PUBLIC_URL=" "$ENV_FILE"; then
        local addr
        addr=$(public_ip)
        if [[ -n "$addr" ]]; then
            printf '\n# Set by demo/cloud/control-plane.sh: registry address devices use.\nREGISTRY_PUBLIC_URL=http://%s:8080\n' \
                "$addr" >>"$ENV_FILE"
            ok "Set REGISTRY_PUBLIC_URL=http://$addr:8080 in deploy/.env"
        else
            warn "Could not find the public IP; set REGISTRY_PUBLIC_URL in deploy/.env yourself."
        fi
    fi

    if [[ "$PULL" == 1 ]]; then
        say "Updating the code ($(git -C "$REPO_ROOT" branch --show-current)):"
        git -C "$REPO_ROOT" pull --ff-only 2>&1 | tail -n 2 | indent \
            || die "git pull failed; fix it or re-run with --no-pull."
    fi
    info "Running $(git -C "$REPO_ROOT" log -1 --format='%h %s')"

    if [[ "$RESET" == 1 ]]; then
        warn "--reset deletes all device and heartbeat data on this VM."
        read -r -p "   Type 'reset' to continue: " answer </dev/tty
        [[ "$answer" == reset ]] || die "Cancelled."
        cp_compose down -v --remove-orphans 2>&1 | tail -n 2 | indent
    fi

    # Database first, so migrations run before the orchestrator writes to it.
    say "Starting the database and applying migrations..."
    cp_compose up -d --wait --quiet-pull postgres >/dev/null 2>&1 || die "PostgreSQL did not start."
    apply_migrations

    say "Building and starting the control plane..."
    if ! output=$(cp_compose up -d --build --wait --quiet-pull 2>&1); then
        printf '%s\n' "$output" | tail -n 15 | indent
        die "The control plane did not start."
    fi
    ok "Control plane is up."

    local ip
    ip=$(public_ip)
    echo
    local password
    password=$(grep "^VISUAL_DEMO_PASSWORD=" "$ENV_FILE" | tail -n 1 | cut -d= -f2-)
    say "On your computer, from the repository root, run one of:"
    printf '\n      %sdemo/visual/run.sh --cloud %s%s      visual demo (password: %s%s%s)\n' \
        "$BOLD" "${ip:-<vm-ip>}" "$RESET" "$BOLD" "$password" "$RESET"
    printf '      %sdemo/cloud/devices.sh %s%s          terminal demo\n\n' "$BOLD" "${ip:-<vm-ip>}" "$RESET"
    info "The visual demo needs TCP 8090 open to your IP in the security group."
    info "The live view starts in a moment. Ctrl+C leaves it; the control plane keeps running."
    sleep 4
}

render_view() {
    local ip=$1 message=$2
    printf '%s  OTA control plane  %s%s   %s%s%s\n' \
        "$BOLD$CYAN" "$ip" "$RESET" "$DIM" "$(date -u '+%H:%M:%S UTC')" "$RESET"
    printf '  %sKeys: %su%s update fleet to 2.0   %sb%s roll back latest rollout   %sq%s leave view%s\n\n' \
        "$DIM" "$RESET$BOLD" "$RESET$DIM" "$RESET$BOLD" "$RESET$DIM" "$RESET$BOLD" "$RESET$DIM" "$RESET"
    if [[ -n "$message" ]]; then
        printf '%sLast action%s\n%s\n\n' "$BOLD" "$RESET" "$message" | sed '2,$s/^/   /'
    fi

    printf '%sServices%s\n' "$BOLD" "$RESET"
    cp_compose ps --format "{{.Service}}\t{{.Status}}" 2>/dev/null \
        | awk -F'\t' '{printf "   %-20s %s\n", $1, $2}'

    printf '\n%sFleet (device registry)%s\n' "$BOLD" "$RESET"
    cp_psql -c "SELECT device_id, current_version AS version, status,
                       error_count AS errors, crash_count AS crashes,
                       seconds_since_heartbeat AS last_seen_s
                  FROM fleet_overview ORDER BY device_id;" 2>/dev/null \
        | sed -e "s/ offline / ${RED}offline${RESET} /" -e "s/ healthy / ${GREEN}healthy${RESET} /" \
              -e "s/ unhealthy / ${RED}unhealthy${RESET} /" | indent

    printf '%sRecent crashes%s\n' "$BOLD" "$RESET"
    local crashes
    crashes=$(cp_psql -Atc "SELECT to_char(received_at, 'HH24:MI:SS') || '  ' || device_id
                                   || '  v' || reported_version || '  errors=' || error_count
                                   || '  crashes=' || crash_count
                              FROM heartbeats WHERE crash_count > 0
                             ORDER BY id DESC LIMIT 3;" 2>/dev/null)
    if [[ -n "$crashes" ]]; then printf '%s\n' "$crashes" | sed "s/^/   ${RED}/; s/$/${RESET}/"
    else info "none"; fi

    render_rollout

    printf '\n%sOrchestrator decisions%s %s(rollouts, rejected messages, offline devices)%s\n' \
        "$BOLD" "$RESET" "$DIM" "$RESET"
    cp_compose logs --no-log-prefix --tail 300 orchestrator 2>/dev/null \
        | grep -E "Rejected|marked offline|Connected to broker|Starting orchestrator|Rollout|Lost connection" \
        | tail -n 10 \
        | sed -e "s/^[0-9-]* \([0-9:]*\),[0-9]* /\1 /" \
              -e "s/\(.*Rejected.*\)/${YELLOW}\1${RESET}/" \
              -e "s/\(.*marked offline.*\)/${RED}\1${RESET}/" | indent
}

# The latest rollout: state, versions, each device's state, and its last decisions.
render_rollout() {
    local rollout
    rollout=$(cp_psql -Atc "SELECT id || '  ' || upper(state) || '  ' || target_version
                                   || ' (rollback version ' || rollback_version || ')'
                              FROM rollouts ORDER BY id DESC LIMIT 1;" 2>/dev/null)
    printf '\n%sCurrent rollout%s\n' "$BOLD" "$RESET"
    if [[ -z "$rollout" ]]; then
        info "none yet (press u to start one)"
        return
    fi
    local rid=${rollout%% *}
    printf '   %s\n' "$rollout" | sed -e "s/ COMPLETED / ${GREEN}COMPLETED${RESET} /" \
        -e "s/ ROLLED_BACK / ${YELLOW}ROLLED_BACK${RESET} /" -e "s/ HALTED / ${RED}HALTED${RESET} /"
    cp_psql -Atc "SELECT string_agg(device_id || ' ' || state, '   ' ORDER BY device_id)
                    FROM rollout_devices WHERE rollout_id = $rid;" 2>/dev/null \
        | sed -e "s/ updated/ ${GREEN}updated${RESET}/g" -e "s/ failed/ ${RED}failed${RESET}/g" \
              -e "s/ rolled_back/ ${YELLOW}rolled_back${RESET}/g" | indent
    cp_psql -Atc "SELECT to_char(created_at, 'HH24:MI:SS') || '  ' || event || ': ' || coalesce(reason, '')
                    FROM (SELECT * FROM rollout_events WHERE rollout_id = $rid
                          ORDER BY id DESC LIMIT 3) e ORDER BY created_at;" 2>/dev/null \
        | cut -c1-110 | sed "s/^/      ${DIM}/; s/$/${RESET}/"
}

# Keys in the live view run the rollout CLI in the orchestrator container.
run_key_action() {
    case "$1" in
        u)
            printf '$ cli.py rollout start 2.0 --all\n'
            cp_compose exec -T orchestrator python cli.py rollout start 2.0 --all 2>&1 \
                | grep -v -E "^$|Follow it|cli.py rollout status"
            ;;
        b)
            local rid
            rid=$(cp_psql -Atc "SELECT id FROM rollouts ORDER BY id DESC LIMIT 1;" 2>/dev/null)
            if [[ -z "$rid" ]]; then
                echo "There is no rollout to roll back."
                return
            fi
            printf '$ cli.py rollout rollback %s\n' "$rid"
            cp_compose exec -T orchestrator python cli.py rollout rollback "$rid" \
                --reason "operator: rollback requested from the live view" 2>&1 | head -n 1
            ;;
    esac
}

leave_view() {
    tput cnorm 2>/dev/null
    echo
    info "Live view closed; the control plane is still running."
    exit 0
}

live_view() {
    local ip frame key message="" have_tty=0
    ip=$(public_ip)
    { : </dev/tty; } 2>/dev/null && have_tty=1
    trap leave_view INT
    tput civis 2>/dev/null
    while true; do
        frame=$(render_view "$ip" "$message")   # render first, then redraw at once: no flicker
        printf '\033[H\033[2J%s\n' "$frame"
        key=""
        if [[ "$have_tty" == 1 ]]; then
            read -r -s -n 1 -t 2 key </dev/tty || true
        else
            sleep 2
        fi
        case "$key" in
            u|b) message=$(run_key_action "$key") ;;
            q) leave_view ;;
        esac
    done
}

require_docker
[[ "$VIEW_ONLY" == 1 ]] || start_control_plane
live_view
