# Step: a manual rollout of 2.0 and a rollback to 1.0, driven by the orchestrator.
#
# The commands are real: the orchestrator sends them and the device containers receive
# them. The devices' replies are simulated, because the agent does not install firmware
# yet (Iteration 2, device update logic). Remove the publish_event calls once it does.

step "Manual rollout and rollback: the orchestrator commands each device" \
     "Iteration 2 orchestrator commands, NF-5 Observability (FR-5 groundwork)"

say "The orchestrator sends one command per device on ota/devices/{id}/commands, with the"
say "firmware's download, manifest and signature URLs and its SHA-256, then follows each"
say "device's reply. A listener on the command topics shows exactly what is sent."
start_command_listener
sleep 1
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

say "${YELLOW}Simulated replies${RESET} (the agent will send these once it installs firmware):"
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
say "${YELLOW}Simulated replies:${RESET}"
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
