# Step: recap.

step "Summary"

printf '   %s%-50s %s%s\n' "$BOLD" "What was shown" "Requirement" "$RESET"
printf '   %-50s %s\n' \
    "Control plane from one compose command" "NF-6" \
    "Devices register with version + heartbeat" "FR-1, FR-8" \
    "Malformed messages rejected and logged" "contract, NF-5" \
    "Crashes recorded apart from errors" "FR-4 groundwork" \
    "Offline after 30 s silence, back online" "timeout req., NF-1" \
    "Signed firmware; tampering rejected" "FR-6, FR-3 checks" \
    "Manual rollout + rollback, per-device commands" "It. 2 commands, NF-5"
echo
info "Next: devices install the update and reply themselves (Iteration 2),"
info "then staged rollout with automatic rollback (Iteration 3)."

if [[ "$CP_LOCAL" == 1 ]]; then
    echo
    say "Final state of the fleet:"
    show_fleet
fi
