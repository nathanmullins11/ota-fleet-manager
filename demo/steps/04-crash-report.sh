# Step: a heartbeat reporting a firmware crash is recorded separately from errors.

step "Firmware crashes are recorded separately from errors" \
     "FR-4 Health signals (groundwork), FR-8 Device registry"

say "When firmware crashes, the agent keeps running and reports it in the heartbeat's crash_count."
say "So the orchestrator can tell a crashed device (heartbeats continue) from an offline one"
say "(heartbeats stop). The agent's crash reporting arrives with the update logic, so this step"
say "sends the heartbeat a device-003 running faulty 2.1 would send:"
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
    info "device-003's next real heartbeat (1.0, healthy) takes its row back to normal."
else
    look_at_vm "the Recent crashes panel shows device-003, version 2.1, 25 errors, 1 crash."
fi
pause
