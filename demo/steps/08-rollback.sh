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
    if [[ "$CP_LOCAL" == 1 ]]; then
        if ! wait_until $(( ${COMMAND_TIMEOUT_SECONDS:-120} + 15 )) \
                "waiting for the orchestrator to finish the rollback" \
                rollout_state_is "$ROLLOUT_RID" rolled_back; then
            check_fail "rollback of rollout $ROLLOUT_RID did not finish" "state: $(rollout_state "$ROLLOUT_RID")"
        fi
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
