# Step: messages that break the contract are rejected and logged with a reason.

step "Malformed messages are rejected and logged with the reason" \
     "Message contract (mqtt-topics-and-messages.md), NF-5 Observability"

say "Publishing three bad heartbeats on device-002's topic, as a faulty or hostile client might:"
since=$(now_utc)
sleep 1
bad_messages=(
    'not json at all'
    '{"schema_version":1,"device_id":"device-002","version":"1.0","status":"on-fire","error_count":0,"timestamp":"2026-11-05T12:00:00Z"}'
    '{"schema_version":1,"device_id":"device-999","version":"1.0","status":"healthy","error_count":0,"timestamp":"2026-11-05T12:00:00Z"}'
)
labels=("payload is not JSON" "status value not allowed" "device ID does not match the topic")
for n in 0 1 2; do
    info "$((n + 1)). ${labels[$n]}"
    mqtt_pub "ota/devices/device-002/heartbeat" "${bad_messages[$n]}"
done
echo

if [[ "$CP_LOCAL" == 1 ]]; then
    sleep 2
    say "Orchestrator log:"
    orchestrator_log "$since" "Rejected" | sed 's/^[0-9-]* [0-9:,]* //' | indent
    count=$(orchestrator_log "$since" "Rejected" | wc -l)
    if ((count >= 3)); then
        ok "All $count rejected with a reason; device-002's registry entry is untouched."
    else
        bad "Expected 3 rejections, saw $count."
    fi
else
    look_at_vm "three 'Rejected heartbeat message from device-002: ...' lines in the orchestrator log."
fi
pause
