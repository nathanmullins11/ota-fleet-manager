# Step: a device that stops sending heartbeats is marked offline, then back online.

step "A silent device is marked offline, then back online" \
     "Heartbeat timeout requirement (30 s), NF-1 Resilience (groundwork)"

say "Stopping device-002's container. Its heartbeats stop; the other devices keep reporting."
run_note "docker stop $(device_name 2)"
docker stop "$(device_name 2)" >/dev/null
since=$(now_utc)
echo

if [[ "$CP_LOCAL" == 1 ]]; then
    if wait_until 75 "waiting for the orchestrator to notice (timeout is 30 s)" \
            status_is device-002 offline; then
        check_pass "device-002 marked offline ${WAITED}s after it stopped."
        orchestrator_log "$since" "marked offline" | sed 's/^[0-9-]* [0-9:,]* //' | indent
        echo
        show_fleet
    else
        check_fail "device-002 was not marked offline within 75 s."
    fi
else
    look_at_vm "within about 30-35 s, device-002 turns offline and 'marked offline' appears in the log."
    countdown 40 "waiting for the heartbeat timeout"
fi
pause

say "Starting device-002 again:"
run_note "docker start $(device_name 2)"
docker start "$(device_name 2)" >/dev/null
echo
if [[ "$CP_LOCAL" == 1 ]]; then
    if wait_until 30 "waiting for its next heartbeat" status_is device-002 healthy; then
        check_pass "device-002 back online (healthy) ${WAITED}s after restarting, on its first heartbeat."
        show_fleet
    else
        check_fail "device-002 did not come back within 30 s."
    fi
else
    look_at_vm "device-002 returns to healthy within a few seconds."
    countdown 10 "waiting for its first heartbeat"
fi
pause
