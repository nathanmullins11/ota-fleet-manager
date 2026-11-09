# Step: simulated devices connect over MQTT and register with version and heartbeat.

step "Devices connect and register with their version and heartbeat" \
     "FR-1 Register and heartbeat, FR-8 Device registry"

say "Starting $DEVICE_COUNT simulated devices on this machine (one container each), firmware 1.0,"
say "heartbeat every 5 seconds, broker at $MQTT_HOST:$MQTT_PORT."
echo
for i in $(seq 1 "$DEVICE_COUNT"); do
    start_device "$i"
done
for i in $(seq 1 "$DEVICE_COUNT"); do
    if wait_until 30 "waiting for $(device_id "$i") to connect" device_connected "$i"; then
        line=$(docker logs "$(device_name "$i")" 2>&1 | grep "Connected to broker" | tail -n 1 | sed 's/^.*device-agent: //')
        ok "$(device_id "$i"): $line"
    else
        bad "$(device_id "$i") did not connect. Last log lines:"
        docker logs --tail 5 "$(device_name "$i")" 2>&1 | indent
        die "Check that the broker is reachable at $MQTT_HOST:$MQTT_PORT."
    fi
done
echo

if [[ "$CP_LOCAL" == 1 ]]; then
    if wait_until 20 "waiting for the orchestrator to record the heartbeats" \
            at_least_healthy "$DEVICE_COUNT"; then
        say "The device registry (fleet_overview), written by the orchestrator:"
        show_fleet
        ok "Every device is registered with firmware 1.0 and a heartbeat in the last few seconds."
    else
        bad "Not all devices reached the registry. Orchestrator log:"
        cp_compose logs --tail 10 orchestrator | indent
    fi
else
    look_at_vm "the Fleet table gains device-001 to device-00$DEVICE_COUNT, healthy, version 1.0, last_seen_s near 0."
fi
pause
