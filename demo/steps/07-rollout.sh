# Step: a rollout of firmware 2.0 to every device, driven by the orchestrator.
#
# Real (default): the devices install 2.0 themselves and report it; anything they do not do
# is printed as NOT WORKING. The rollback is step 8.
# --simulate: the demo sends the devices' replies (and does the rollback here too).

if [[ "$SIMULATE" == 1 ]]; then
    step "Manual rollout and rollback: the orchestrator commands each device (replies SIMULATED)" \
         "Iteration 2 orchestrator commands, NF-5 Observability (FR-5 groundwork)"
    say "The orchestrator sends one command per device on ota/devices/{id}/commands, with the"
    say "firmware's download, manifest and signature URLs and its SHA-256, then follows each"
    say "device's reply. A listener on the command topics shows exactly what is sent."
    echo

    # ----------------------------------------------------------------------------- update
    if [[ "$CP_LOCAL" == 1 ]]; then
        say "Starting a rollout of firmware 2.0 to every online device:"
        run_note "python cli.py rollout start 2.0 --all"
        cli rollout start 2.0 --all 2>&1 | grep -v -E "^$|Follow it|cli.py rollout status" | indent
    else
        look_at_vm "press ${BOLD}u${RESET}${CYAN} in the live view to start a rollout of 2.0 to every online device."
    fi

    if ! wait_until 180 "waiting for update commands" command_seen update; then
        bad "No update command arrived. Is a rollout already running? (rollout list on the VM)"
        pause
        return 0
    fi
    sleep 2      # let the commands to every device arrive
    rid=$(latest_rollout_id update)
    mapfile -t targets < <(commanded_devices update "$rid")
    echo
    ok "Rollout $rid: update command received by ${#targets[@]} device(s): ${targets[*]}"
    say "The command ${targets[0]} received:"
    commands_seen update "$rid" | grep "/${targets[0]}/" | head -n 1 | show_command
    info "Real device container's log: $(docker logs "ota-demo-${targets[0]}" 2>&1 \
        | grep 'Command received' | tail -n 1 | sed 's/^.*device-agent: //' | cut -c1-90)…"
    pause

    say "${YELLOW}SIMULATED replies${RESET} (the agent will send these once it installs firmware):"
    last=${targets[${#targets[@]}-1]}
    for d in "${targets[@]}"; do
        publish_event "$d" update_started "$rid" 2.0
        if [[ "$d" == "$last" && ${#targets[@]} -gt 1 ]]; then
            publish_event "$d" update_failed "$rid" 2.0 "checksum mismatch"
            info "$d: update_started, update_failed (checksum mismatch)"
        else
            publish_event "$d" update_succeeded "$rid" 2.0
            info "$d: update_started, update_succeeded"
        fi
    done
    echo
    show_resources "during the rollout of 2.0"
    echo
    if [[ "$CP_LOCAL" == 1 ]]; then
        sleep 2
        run_note "python cli.py rollout status $rid"
        cli rollout status "$rid" | indent
        ok "Each device's outcome and every decision is recorded with its reason."
        info "Devices still report 1.0 in their heartbeats: their replies above were simulated."
    else
        look_at_vm "the Current rollout panel shows rollout $rid completed: updated devices and $last failed."
    fi
    pause

    # ----------------------------------------------------------------------------- rollback
    say "The operator decides 2.0 should not stay on the fleet, and rolls the rollout back."
    if [[ "$CP_LOCAL" == 1 ]]; then
        run_note "python cli.py rollout rollback $rid"
        cli rollout rollback "$rid" --reason "operator: 2.0 failed on $last" 2>&1 | head -n 1 | indent
    else
        look_at_vm "press ${BOLD}b${RESET}${CYAN} in the live view to roll back the latest rollout."
    fi

    if ! wait_until 180 "waiting for rollback commands" command_seen rollback "$rid"; then
        bad "No rollback command arrived."
        pause
        return 0
    fi
    sleep 2
    echo
    ok "Rollback command received by: $(commanded_devices rollback "$rid" | tr '\n' ' ')"
    commands_seen rollback "$rid" | head -n 1 | show_command

    echo
    say "${YELLOW}SIMULATED replies:${RESET}"
    for d in "${targets[@]}"; do
        publish_event "$d" rollback_started "$rid" 1.0
        publish_event "$d" rollback_succeeded "$rid" 1.0
        info "$d: rollback_started, rollback_succeeded"
    done
    echo
    if [[ "$CP_LOCAL" == 1 ]]; then
        sleep 2
        cli rollout status "$rid" | indent
        ok "Rollout $rid rolled back; the decision log shows each step and why."
    else
        look_at_vm "the Current rollout panel shows rollout $rid rolled_back, every device rolled_back."
    fi
    pause
    return 0
fi

step "Rollout 2.0: every device installs the update" \
     "Orchestrator commands, FR-3 download/verify/install/report, NF-5"

say "The orchestrator sends each device its own update command (download, manifest and"
say "signature URLs plus the SHA-256). Each device must download, verify, install and restart"
say "on 2.0, report update_succeeded, and then report 2.0 in its heartbeats."
echo
ROLLOUT_RID=""
ROLLOUT_TARGETS=()
mark=$(listener_mark)
t0=$(now_utc)
declare -A PID_BEFORE=()
for i in $(seq 1 "$DEVICE_COUNT"); do
    PID_BEFORE[$(device_id "$i")]=$(firmware_pid "$(device_id "$i")")
done
if begin_rollout 2.0 u "update every online device to 2.0" --all; then
    ROLLOUT_RID=$RID
    mapfile -t ROLLOUT_TARGETS < <(commanded_devices update "$RID")
    say "The command ${ROLLOUT_TARGETS[0]} received:"
    commands_seen update "$RID" | grep "/${ROLLOUT_TARGETS[0]}/" | head -n 1 | show_command
    pause

    for d in "${ROLLOUT_TARGETS[@]}"; do
        if expect "$(event_timeout)" "$d installs 2.0 and reports update_succeeded" "$mark" "$d" \
                event_seen "$mark" "$d" update_succeeded "$RID"; then
            expect 20 "$d's heartbeat reports version 2.0" "$mark" "$d" \
                hb_is "$mark" "$d" version 2.0 || true
        else
            DEVICES_SILENT=1
            reason=$(event_reason "$mark" "$d" update_failed "$RID")
            [[ -n "$reason" ]] && info "$d reported update_failed: $reason"
        fi
    done
    echo

    # Proof from inside the containers: each device's old firmware process was stopped and a
    # new one runs from the 2.0 folder (not just a new version label in the heartbeat).
    replaced=0 process_lines=()
    for d in "${ROLLOUT_TARGETS[@]}"; do
        now_pid=$(firmware_pid "$d")
        now_version=$(firmware_running_version "$d")
        [[ "$now_version" == 2.0 && -n "$now_pid" && "$now_pid" != "${PID_BEFORE[$d]:-}" ]] \
            && replaced=$((replaced + 1))
        process_lines+=("$d: pid ${PID_BEFORE[$d]:-?} (1.0) → pid ${now_pid:-none} (${now_version:-nothing running})")
    done
    first=${ROLLOUT_TARGETS[0]}
    if ((replaced == ${#ROLLOUT_TARGETS[@]})); then
        check_pass "Every device replaced its firmware process: ${process_lines[0]}"
    else
        check_fail "Only $replaced of ${#ROLLOUT_TARGETS[@]} devices run a new firmware 2.0 process" \
                   "$(printf '%s; ' "${process_lines[@]}")"
    fi
    downloads=$(device_log "$first" "$t0" "^[0-9:]+ 2.0 downloaded" | wc -l)
    checks_ok=$(device_log "$first" "$t0" "^[0-9:]+ 2.0 check [0-9]/4 .*: OK$" | wc -l)
    proof "$first downloaded $downloads files (manifest, signature, package) and passed $checks_ok of 4 checks before installing"
    reports=$(device_log "$first" "$t0" "Firmware 2.0 reports:" | head -n 1 | sed 's/^[0-9:]* Firmware 2.0 reports: //')
    proof "firmware 2.0's own first output on $first: ${reports:-none} (1.0 never prints uptime_seconds)"
    proof "$first's installed state: $(device_state_file "$first")"
    # Firmware state policy: 2.0 converts the device's state to schema 2, which 1.0 cannot read.
    converted=0
    for d in "${ROLLOUT_TARGETS[@]}"; do
        [[ "$(firmware_config "$d" | json_field state_schema)" == 2 ]] && converted=$((converted + 1))
    done
    if ((converted == ${#ROLLOUT_TARGETS[@]})); then
        check_pass "Firmware 2.0 converted every device's state to schema 2 (1.0 cannot read it; the agent kept a snapshot)"
    else
        check_fail "Only $converted of ${#ROLLOUT_TARGETS[@]} devices have schema 2 state after the update" \
                   "$first: $(firmware_config "$first")"
    fi
    proof "$first's firmware state: $(firmware_config "$first")"
    timing=$(rollout_timing update "$RID") && proof "rollout $RID timing: $timing"
    detail "Firmware processes on every device:" printf '%s\n' "${process_lines[@]}"
    detail "$first's log for the update:" device_log "$first" "$t0" "2.0|Command received|Event sent|Firmware 1.0 stopped"
    detail "$first's update_succeeded event exactly as sent (MQTT):" raw_message "$mark" "$first" events update_succeeded
    echo
    show_resources "during the rollout of 2.0 to ${#ROLLOUT_TARGETS[@]} devices"
    echo
    if [[ "$CP_LOCAL" == 1 ]]; then
        if ! wait_until $(( ${COMMAND_TIMEOUT_SECONDS:-120} + 15 )) \
                "waiting for the orchestrator to finish rollout $RID" rollout_state_is "$RID" completed; then
            check_fail "rollout $RID did not finish" "state: $(rollout_state "$RID")"
        fi
        run_note "python cli.py rollout status $RID"
        cli rollout status "$RID" | indent
        updated=$(cp_psql -Atc "SELECT count(*) FROM rollout_devices WHERE rollout_id = $RID AND state = 'updated';")
        if [[ "$updated" == "${#ROLLOUT_TARGETS[@]}" ]]; then
            check_pass "rollout $RID completed: ${updated} of ${#ROLLOUT_TARGETS[@]} devices updated"
        else
            check_fail "rollout $RID: only ${updated:-0} of ${#ROLLOUT_TARGETS[@]} devices updated" \
                       "devices without a reply are marked failed after the command timeout"
        fi
    else
        look_at_vm "the Current rollout panel shows rollout $RID: every device updated, then COMPLETED."
    fi
fi
pause
