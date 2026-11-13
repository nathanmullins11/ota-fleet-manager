# Step: roll the 2.0 rollout back; every device returns to 1.0.
# (--simulate does its rollback inside step 7.)

[[ "$SIMULATE" == 1 ]] && return 0

step "Rollback: every device returns to 1.0" \
     "FR-5 groundwork (rollback path), NF-1 resilience, NF-5"

if [[ -z "${ROLLOUT_RID:-}" ]]; then
    check_skip "rollback of the 2.0 rollout" "step 7 did not start a rollout"
    pause
    return 0
fi

say "The operator rolls rollout $ROLLOUT_RID back. Each device gets a rollback command to 1.0: it"
say "must restore the version it kept locally, report rollback_succeeded, and report 1.0 again."
echo
mark=$(listener_mark)
t0=$(now_utc)
declare -A PID_BEFORE=()
for d in "${ROLLOUT_TARGETS[@]}"; do PID_BEFORE[$d]=$(firmware_pid "$d"); done
if begin_rollback "$ROLLOUT_RID"; then
    commands_seen rollback "$ROLLOUT_RID" | head -n 1 | show_command
    for d in "${ROLLOUT_TARGETS[@]}"; do
        updated=0
        hb_ever_is 0 "$d" version 2.0 && updated=1
        if expect "$(event_timeout)" "$d rolls back and reports rollback_succeeded" "$mark" "$d" \
                event_seen "$mark" "$d" rollback_succeeded "$ROLLOUT_RID"; then
            if [[ "$updated" == 1 ]]; then
                expect 20 "$d's heartbeat reports 1.0 again" "$mark" "$d" \
                    hb_is "$mark" "$d" version 1.0 || true
            fi
        else
            DEVICES_SILENT=1
        fi
    done
    echo

    # Proof: each device runs a new 1.0 process, restored from its kept copy (no download).
    restored_local=0 process_lines=()
    for d in "${ROLLOUT_TARGETS[@]}"; do
        now_pid=$(firmware_pid "$d")
        now_version=$(firmware_running_version "$d")
        if [[ "$now_version" == 1.0 && "$now_pid" != "${PID_BEFORE[$d]:-}" ]] \
                && device_log "$d" "$t0" "Using the kept copy of 1.0" | grep -q . \
                && ! device_log "$d" "$t0" "Downloading 1.0" | grep -q .; then
            restored_local=$((restored_local + 1))
        fi
        process_lines+=("$d: pid ${PID_BEFORE[$d]:-?} (2.0) → pid ${now_pid:-none} (${now_version:-nothing running})")
    done
    first=${ROLLOUT_TARGETS[0]}
    if ((restored_local == ${#ROLLOUT_TARGETS[@]})); then
        check_pass "Every device restored 1.0 from its kept copy, without downloading: ${process_lines[0]}"
    else
        check_fail "Only $restored_local of ${#ROLLOUT_TARGETS[@]} devices restored 1.0 from their kept copy" \
                   "$(printf '%s; ' "${process_lines[@]}")"
    fi
    checks_ok=$(device_log "$first" "$t0" "^[0-9:]+ 1.0 check [0-9]/4 .*: OK$" | wc -l)
    proof "$first re-verified its kept copy of 1.0 against the command ($checks_ok of 4 checks OK) before running it"
    proof "$first's installed state: $(device_state_file "$first")"
    # Firmware state policy: the pre-update snapshot is restored, so 1.0 reads its own state.
    state_restored=0
    for d in "${ROLLOUT_TARGETS[@]}"; do
        if [[ "$(firmware_config "$d" | json_field state_schema)" == 1 ]] \
                && device_log "$d" "$t0" "Restored the state snapshot of 1.0" | grep -q .; then
            state_restored=$((state_restored + 1))
        fi
    done
    if ((state_restored == ${#ROLLOUT_TARGETS[@]})); then
        check_pass "Every device's pre-update state (schema 1) was restored, so 1.0 started cleanly on it"
    else
        check_fail "Only $state_restored of ${#ROLLOUT_TARGETS[@]} devices had their schema 1 state restored" \
                   "$first: $(firmware_config "$first")"
    fi
    proof "$first's firmware state: $(firmware_config "$first")"
    timing=$(rollout_timing rollback "$ROLLOUT_RID") && proof "rollback timing: $timing"
    detail "Firmware processes on every device:" printf '%s\n' "${process_lines[@]}"
    detail "$first's log for the rollback:" device_log "$first" "$t0" "1.0|Command received|Event sent|Firmware 2.0 stopped"
    echo
    if [[ "$CP_LOCAL" == 1 ]]; then
        if ! wait_until $(( ${COMMAND_TIMEOUT_SECONDS:-120} + 15 )) \
                "waiting for the orchestrator to finish the rollback" \
                rollout_state_is "$ROLLOUT_RID" rolled_back; then
            check_fail "rollback of rollout $ROLLOUT_RID did not finish" "state: $(rollout_state "$ROLLOUT_RID")"
        fi
        run_note "python cli.py rollout status $ROLLOUT_RID"
        cli rollout status "$ROLLOUT_RID" | indent
        restored=$(cp_psql -Atc "SELECT count(*) FROM rollout_devices WHERE rollout_id = $ROLLOUT_RID AND state = 'rolled_back';")
        if [[ "$restored" == "${#ROLLOUT_TARGETS[@]}" ]]; then
            check_pass "rollout $ROLLOUT_RID rolled back: ${restored} of ${#ROLLOUT_TARGETS[@]} devices restored"
        else
            check_fail "rollout $ROLLOUT_RID: only ${restored:-0} of ${#ROLLOUT_TARGETS[@]} devices rolled back" \
                       "devices without a reply are marked failed after the command timeout"
        fi
    else
        look_at_vm "the Current rollout panel shows rollout $ROLLOUT_RID: every device rolled_back."
    fi
fi
pause
