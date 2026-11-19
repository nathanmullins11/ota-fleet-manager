# Step: devices that stop sending heartbeats are marked offline, then back online.

step "Silent devices are marked offline, then back online" \
     "Heartbeat timeout requirement (30 s), NF-1 Resilience (groundwork)"

OFFLINE=(device-002 device-006 device-009)
say "Stopping ${OFFLINE[*]} (their containers). Their heartbeats stop; the other devices keep"
say "reporting. The orchestrator declares a device offline after 30 s without a heartbeat."
containers=()
for d in "${OFFLINE[@]}"; do containers+=("$(container_of "$d")"); done
run_note "docker stop ${containers[*]}"
docker stop "${containers[@]}" >/dev/null
since=$(now_utc)
echo

all_status() {   # all_status STATUS: every device in OFFLINE has that status
    local d
    for d in "${OFFLINE[@]}"; do status_is "$d" "$1" || return 1; done
}

if [[ "$CP_LOCAL" == 1 ]]; then
    if wait_until 75 "waiting for the orchestrator to notice (timeout is 30 s)" all_status offline; then
        check_pass "${OFFLINE[*]} marked offline ${WAITED}s after they stopped."
        orchestrator_log "$since" "marked offline" | sed 's/^[0-9-]* [0-9:,]* //' | indent
        echo
        show_fleet
    else
        check_fail "not all of ${OFFLINE[*]} were marked offline within 75 s" \
                   "$(for d in "${OFFLINE[@]}"; do printf '%s %s; ' "$d" "$(device_status "$d")"; done)"
    fi
else
    look_at_vm "within about 30-35 s, ${OFFLINE[*]} turn offline and 'marked offline' appears in the log."
    countdown 40 "waiting for the heartbeat timeout"
fi
pause

say "Starting them again:"
run_note "docker start ${containers[*]}"
docker start "${containers[@]}" >/dev/null
echo
if [[ "$CP_LOCAL" == 1 ]]; then
    if wait_until 30 "waiting for their next heartbeats" all_status healthy; then
        check_pass "${OFFLINE[*]} back online (healthy) ${WAITED}s after restarting, on their first heartbeats."
        show_fleet
    else
        check_fail "not all of ${OFFLINE[*]} came back within 30 s" \
                   "$(for d in "${OFFLINE[@]}"; do printf '%s %s; ' "$d" "$(device_status "$d")"; done)"
    fi
else
    look_at_vm "${OFFLINE[*]} return to healthy within a few seconds."
    countdown 10 "waiting for their first heartbeats"
fi
pause
