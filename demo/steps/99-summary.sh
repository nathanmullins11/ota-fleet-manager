# Step: recap, with every check's result.

step "Summary"

printf '   %s%-50s %s%s\n' "$BOLD" "What was shown" "Requirement" "$RESET"
printf '   %-50s %s\n' \
    "Control plane from one compose command" "NF-6" \
    "Devices register with version + heartbeat" "FR-1, FR-8" \
    "Malformed messages rejected and logged" "contract, NF-5" \
    "Faulty firmware crashes, reported (not offline)" "FR-3, FR-4, FR-8" \
    "Offline after 30 s silence, back online" "timeout req., NF-1" \
    "Signed firmware; tampering rejected" "FR-6, FR-3" \
    "Rollout of 2.0 and rollback to 1.0" "commands, FR-3, FR-5 path, NF-5"
echo

if [[ "$SIMULATE" == 1 ]]; then
    warn "Run with --simulate: device replies and the crash heartbeat were faked by the demo."
    echo
fi

passed=0 failed=0 skipped=0
for c in "${CHECKS[@]}"; do
    case "${c%%|*}" in pass) passed=$((passed + 1)) ;; fail) failed=$((failed + 1)) ;; *) skipped=$((skipped + 1)) ;; esac
done
printf '   %sChecks:%s %s%d passed%s' "$BOLD" "$RESET" "$GREEN" "$passed" "$RESET"
((failed)) && printf ', %s%s%d NOT WORKING%s' "$BOLD" "$RED" "$failed" "$RESET"
((skipped)) && printf ', %s%d skipped%s' "$YELLOW" "$skipped" "$RESET"
echo

if ((failed || skipped)); then
    echo
    for c in "${CHECKS[@]}"; do
        case "${c%%|*}" in
            fail) printf '   %s✘ NOT WORKING: %s%s\n' "$RED" "${c#*|}" "$RESET" ;;
            skip) printf '   %s– skipped: %s%s\n' "$YELLOW" "${c#*|}" "$RESET" ;;
        esac
    done
    echo
    info "Device-side behaviour (install, crash reports, rollback, refusing forged commands) comes"
    info "with the device update logic (Iteration 2). See firmware/README.md and section 4 of"
    info "deploy/mqtt/mqtt-topics-and-messages.md for what the devices must do."
else
    ok "Everything the demo checks is working."
fi

if [[ "$CP_LOCAL" == 1 ]]; then
    echo
    say "Final state of the fleet:"
    show_fleet
fi
echo
show_resources "end of the demo"
