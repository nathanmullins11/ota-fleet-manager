# Step: simulated devices connect over MQTT and register with version and heartbeat.

step "Devices connect and register with their version and heartbeat" \
     "FR-1 Register and heartbeat, FR-8 Device registry"

say "Starting $DEVICE_COUNT simulated devices on this machine (one container each), firmware 1.0,"
say "heartbeat every 5 seconds, broker at $MQTT_HOST:$MQTT_PORT."
echo
boot_time=$(now_utc)
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
for i in $(seq 1 "$DEVICE_COUNT"); do
    d=$(device_id "$i")
    expect 15 "$d's heartbeat reports firmware 1.0" 0 "$d" hb_is 0 "$d" version 1.0 || true
done
echo

# Proof from inside the containers: the heartbeat's "1.0" is a real firmware process.
running=0 missing=()
for i in $(seq 1 "$DEVICE_COUNT"); do
    d=$(device_id "$i")
    if [[ "$(firmware_running_version "$d")" == 1.0 ]]; then running=$((running + 1)); else missing+=("$d"); fi
done
if ((running == DEVICE_COUNT)); then
    check_pass "Every device runs firmware 1.0 as a real process (device-001: $(describe_process device-001))"
else
    check_fail "Only $running of $DEVICE_COUNT devices run a firmware 1.0 process" "none in: ${missing[*]}"
fi
proof "device-001 verified its factory firmware before running it: $(device_log device-001 "$boot_time" "check [0-9]/4" \
    | grep -c ": OK$") of 4 checks OK"
detail "device-001's log at start-up:" device_log device-001 "$boot_time" "factory|check|Firmware|firmware:|Starting"
detail "A heartbeat exactly as device-001 sent it (MQTT, ota/devices/device-001/heartbeat):" \
    raw_message 0 device-001 heartbeat
echo

if [[ "$CP_LOCAL" == 1 ]]; then
    if wait_until 20 "waiting for the orchestrator to record the heartbeats" \
            at_least_healthy "$DEVICE_COUNT"; then
        say "The device registry (fleet_overview), written by the orchestrator:"
        show_fleet
        check_pass "Every device is registered with firmware 1.0 and a recent heartbeat"
    else
        check_fail "Not every device reached the device registry" \
                   "$(cp_compose logs --tail 3 orchestrator 2>&1 | tr '\n' ' ')"
    fi
else
    look_at_vm "the Fleet table gains device-001 to $(device_id "$DEVICE_COUNT"), healthy, version 1.0, last_seen_s near 0."
fi
echo
sleep 3      # let a few heartbeats flow before measuring
show_resources "$DEVICE_COUNT devices idle, a heartbeat every 5 s each"
pause
