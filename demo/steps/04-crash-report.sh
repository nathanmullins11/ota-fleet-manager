# Step: faulty firmware 2.1 crashes on a device; the crash is reported, not mistaken for offline.
#
# Real (default): roll out 2.1 to three devices and check what they do, as described in
# firmware/README.md: install, report 2.1, errors after ~10 s, crash after ~15 s (the agent
# keeps heartbeating with crash_count), then roll back to 1.0.
# --simulate: publish the heartbeat such a device would send instead.

if [[ "$SIMULATE" == 1 ]]; then
    step "Firmware crashes are recorded separately from errors (SIMULATED)" \
         "FR-4 Health signals (groundwork), FR-8 Device registry"
    say "When firmware crashes, the agent keeps running and reports it in the heartbeat's crash_count,"
    say "so the orchestrator can tell a crashed device (heartbeats continue) from an offline one."
    say "${YELLOW}SIMULATED:${RESET} the demo sends the heartbeat a device-003 running faulty 2.1 would send:"
    payload="{\"schema_version\":1,\"device_id\":\"device-003\",\"version\":\"2.1\",\"status\":\"unhealthy\",\"error_count\":25,\"crash_count\":1,\"timestamp\":\"$(now_utc)\"}"
    info "$payload"
    mqtt_pub "ota/devices/device-003/heartbeat" "$payload"
    echo
    if [[ "$CP_LOCAL" == 1 ]]; then
        sleep 2
        say "device-003's heartbeat history (newest first):"
        cp_psql -c "SELECT reported_version AS version, status, error_count AS errors,
                           crash_count AS crashes, to_char(received_at, 'HH24:MI:SS') AS received
                      FROM heartbeats WHERE device_id = 'device-003'
                     ORDER BY id DESC LIMIT 4;" | sed '/^$/d' | indent
        ok "Recorded with errors and crashes in separate columns."
    else
        look_at_vm "the Recent crashes panel shows device-003, version 2.1, 25 errors, 1 crash."
    fi
    pause
    return 0
fi

step "Faulty firmware 2.1: stopped at the canary group and rolled back automatically" \
     "FR-2 staged rollout, FR-4 health evaluation, FR-5 automatic rollback, FR-8 device registry"

# Staged rollout (FR-2): the canary group is the first stage, devices in ID order.
FAULTY=(device-001 device-002 device-003)
say "Rolling out the faulty firmware 2.1 to the whole fleet in stages (3, then 3, then the rest)."
say "Only the canary group (${FAULTY[*]}) gets it first. Expected (firmware/README.md): each"
say "installs 2.1, runs healthy for ~10 s, reports errors, crashes at ~15 s. The orchestrator"
say "watches the canaries for a health window, halts as soon as one crosses a limit (here, any"
say "crash) and rolls them back; the other devices must never receive 2.1."
info "(Up to 50 errors are allowed for this rollout, so a crash is what triggers the rollback.)"
echo
mark=$(listener_mark)
t0=$(now_utc)
declare -A PID_BEFORE=() CONTAINER_BEFORE=()
for d in "${FAULTY[@]}"; do
    PID_BEFORE[$d]=$(firmware_pid "$d")
    CONTAINER_BEFORE[$d]=$(container_info "$d")
done
# The device whose crash the checks follow: whichever crashes first (they all crash ~15 s in).
CRASHED=""
first_crash() {
    local d
    for d in "${FAULTY[@]}"; do
        if hb_ever_above "$mark" "$d" crash_count 0; then CRASHED=$d; return 0; fi
    done
    return 1
}
if begin_rollout 2.1 f "faulty 2.1 to the fleet, in stages" \
        --all --stages 3,3,4 --max-errors 50; then
    crash_rid=$RID
    if expect "$(event_timeout)" "${FAULTY[0]} installs 2.1 and reports update_succeeded" \
            "$mark" "${FAULTY[0]}" event_seen "$mark" "${FAULTY[0]}" update_succeeded "$crash_rid"; then
        for d in "${FAULTY[@]:1}"; do
            expect 10 "$d installs 2.1 and reports update_succeeded" "$mark" "$d" \
                event_seen "$mark" "$d" update_succeeded "$crash_rid" || true
        done
        for d in "${FAULTY[@]}"; do
            expect 20 "$d's heartbeat reports version 2.1" "$mark" "$d" \
                hb_is "$mark" "$d" version 2.1 || true
        done
        for d in "${FAULTY[@]}"; do
            expect 35 "2.1 misbehaves: $d's heartbeats report errors" "$mark" "$d" \
                hb_ever_above "$mark" "$d" error_count 0 || true
        done
        if wait_until 35 "waiting for 2.1 to crash" first_crash; then
            check_pass "2.1 crashes: $CRASHED reports crash_count > 0 (heartbeats continue) (${WAITED}s)"
        else
            check_fail "2.1 crashes: no device reported crash_count > 0 within 35 s" \
                       "$(observed "$mark" "${FAULTY[0]}")"
            CRASHED=${FAULTY[0]}
        fi
        expect 10 "$CRASHED reports status unhealthy" "$mark" "$CRASHED" \
            hb_ever_is "$mark" "$CRASHED" status unhealthy || true

        # Proof from inside the container: the firmware process crashed, the device did not.
        restarted() { device_log "$CRASHED" "$t0" "Restarting firmware 2.1" | grep -c . >/dev/null; }
        # The rollback may stop it first: the orchestrator reacts within a heartbeat or two.
        wait_until 4 "waiting for the agent to restart the firmware" restarted || true
        crash_line=$(device_log "$CRASHED" "$t0" "Firmware 2.1 crashed" | head -n 1)
        restart_line=$(device_log "$CRASHED" "$t0" "Restarting firmware 2.1" | head -n 1)
        container_before=${CONTAINER_BEFORE[$CRASHED]}
        container_after=$(container_info "$CRASHED")
        # Every firmware 2.1 process the agent started, from its log: "Firmware 2.1 started (pid N)".
        mapfile -t pids_21 < <(device_log "$CRASHED" "$t0" "Firmware 2.1 started" | sed -n 's/.*(pid \([0-9]*\)).*/\1/p')
        if [[ -n "$crash_line" && "$container_after" == "$container_before" ]]; then
            check_pass "The firmware crashed, $CRASHED did not: same container since ${container_before:11:8} UTC, ${container_before##* } restarts"
        else
            check_fail "No firmware crash in $CRASHED's own log while its container stayed up" \
                       "crash line: ${crash_line:-none}; container before/after: $container_before / $container_after"
        fi
        [[ -n "$crash_line" ]] && proof "$CRASHED's log: $crash_line"
        [[ -n "$restart_line" ]] && proof "$CRASHED's log: $restart_line"
        if ((${#pids_21[@]} >= 2)); then
            proof "firmware processes: pid ${PID_BEFORE[$CRASHED]:-?} (1.0) → pid ${pids_21[0]} (2.1, crashed) → pid ${pids_21[1]} (2.1, restarted by the agent)"
        else
            proof "firmware process: pid ${PID_BEFORE[$CRASHED]:-?} (1.0) before the update, now $(describe_process "$CRASHED")"
        fi
        crash_log() { device_log "$CRASHED" "$t0" | tail -n 30; }
        detail "$CRASHED's log since the rollout started (install, checks, crash, restart):" crash_log
    else
        DEVICES_SILENT=1
        reason=$(event_reason "$mark" "${FAULTY[0]}" update_failed "$crash_rid")
        [[ -n "$reason" ]] && info "${FAULTY[0]} reported update_failed: $reason"
        check_skip "2.1 running, errors and crash reports" "2.1 was not installed"
        CRASHED=${FAULTY[0]}
    fi
    echo
    show_resources "${FAULTY[*]} running faulty 2.1"
    if [[ "$CP_LOCAL" == 1 ]]; then
        echo
        say "The faulty devices' latest heartbeats in the device registry (newest first):"
        cp_psql -c "SELECT device_id AS device, reported_version AS version, status,
                           error_count AS errors, crash_count AS crashes,
                           to_char(received_at, 'HH24:MI:SS') AS received
                      FROM heartbeats WHERE device_id IN ('$(IFS=,; echo "${FAULTY[*]}" | sed "s/,/','/g")')
                       AND received_at > now() - interval '40 seconds'
                     ORDER BY id DESC LIMIT 9;" | sed '/^$/d' | indent
    else
        look_at_vm "the faulty devices show 2.1, then errors, then the Recent crashes panel lists the crash."
    fi
    pause

    say "No operator action: the orchestrator judges each device's health after the update and"
    say "rolls all of them back to 1.0 on its own as soon as one crosses a limit."
    installed=0
    event_seen "$mark" "${FAULTY[0]}" update_succeeded "$crash_rid" && installed=1
    if wait_until 60 "waiting for the orchestrator's automatic rollback" \
            new_command "$mark" rollback "" "$crash_rid"; then
        sleep 1
        if [[ "$CP_LOCAL" == 1 ]]; then
            reason=$(cp_psql -Atc "SELECT halt_reason FROM rollouts WHERE id = $crash_rid;")
            # "automatic: ..." or, for a staged rollout, "automatic (stage 1 of 3): ..."
            if [[ "$reason" == automatic* ]]; then
                check_pass "The orchestrator rolled back automatically: ${reason#automatic*: }"
            else
                check_fail "rollout $crash_rid was rolled back, but not by the health rules" \
                           "halt reason: ${reason:-none}"
            fi
        else
            check_pass "The orchestrator sent rollback commands by itself (no key pressed)"
            look_at_vm "the Current rollout panel shows rollout $crash_rid HALTED, with the automatic reason."
        fi
        rolled=0
        for d in "${FAULTY[@]}"; do
            if expect "$(event_timeout)" "$d rolls back and reports rollback_succeeded" \
                    "$mark" "$d" event_seen "$mark" "$d" rollback_succeeded "$crash_rid"; then
                rolled=$((rolled + 1))
            else
                DEVICES_SILENT=1
            fi
        done
        if [[ "$installed" == 1 && "$rolled" -gt 0 ]]; then
            back=0 notes=()
            for d in "${FAULTY[@]}"; do
                wait_until 20 "waiting for $d to report 1.0" hb_is "$mark" "$d" version 1.0 || true
                kept=$(device_log "$d" "$t0" "Using the kept copy of 1.0" | head -n 1)
                downloaded=$(device_log "$d" "$t0" "Downloading 1.0")
                if [[ "$(firmware_running_version "$d")" == 1.0 && -n "$kept" && -z "$downloaded" ]] \
                        && hb_is "$mark" "$d" version 1.0; then
                    back=$((back + 1))
                else
                    notes+=("$d: $(describe_process "$d")${downloaded:+, downloaded 1.0}")
                fi
            done
            if ((back == ${#FAULTY[@]})); then
                check_pass "All ${#FAULTY[@]} canaries run firmware 1.0 again, restored from their kept copy without a download"
            else
                check_fail "only $back of ${#FAULTY[@]} devices restored 1.0 from their kept copy" "${notes[*]}"
            fi
        fi
        # Iteration 3 final test: the rest of the fleet never received 2.1 ...
        mapfile -t got_update < <(commanded_devices update "$crash_rid")
        others=$((DEVICE_COUNT - ${#FAULTY[@]}))
        if [[ "${got_update[*]}" == "${FAULTY[*]}" ]]; then
            check_pass "The other $others devices never received 2.1: update commands went only to the canary group"
        else
            check_fail "devices outside the canary group received 2.1" "update commands sent to: ${got_update[*]}"
        fi
        if [[ "$CP_LOCAL" == 1 ]]; then
            wait_until 30 "waiting for the rollback to finish" rollout_state_is "$crash_rid" rolled_back || true
            # ... and the canaries were back on 1.0 within 60 s of updating.
            took=$(cp_psql -Atc "SELECT round(EXTRACT(EPOCH FROM (
                        (SELECT min(created_at) FROM rollout_events WHERE rollout_id = $crash_rid
                            AND event = 'rollback_completed')
                      - (SELECT min(window_started_at) FROM rollout_devices WHERE rollout_id = $crash_rid))))")
            if [[ -n "$took" ]] && ((took <= 60)); then
                check_pass "Rolled back within 60 s: ${took} s from the canaries updating to the rollback completing"
            else
                check_fail "the rollback did not complete within 60 s of the canaries updating" "took: ${took:-unknown} s"
            fi
            say "The orchestrator's decision log for rollout $crash_rid:"
            cp_psql -Atc "SELECT to_char(created_at, 'HH24:MI:SS') || '  ' || event || ': ' || coalesce(reason, '')
                            FROM rollout_events WHERE rollout_id = $crash_rid ORDER BY id;" \
                | cut -c1-150 | indent
            detail "The full rollout record (cli.py rollout status):" cli rollout status "$crash_rid"
        fi
    else
        check_fail "the orchestrator did not roll back automatically within 60 s" \
                   "$(observed "$mark" "${FAULTY[0]}")"
        if [[ "$CP_LOCAL" == 1 ]]; then
            info "Rolling back by hand so the next steps start from a clean fleet."
            begin_rollback "$crash_rid" || true
        fi
    fi
fi
pause
