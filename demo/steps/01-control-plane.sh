# Step: the control plane is up, started by one compose command.

step "Control plane up from one Docker Compose command" "NF-6 Reproducibility"

if [[ "$CP_LOCAL" == 1 ]]; then
    say "Starting the broker, device registry, software registry and orchestrator:"
    run_note "docker compose up -d --build --wait"
    pause 1
    if ! output=$(cp_compose up -d --build --wait --quiet-pull 2>&1); then
        printf '%s\n' "$output" | tail -n 15 | indent
        die "The control plane did not start."
    fi
    cp_compose ps --format "table {{.Service}}\t{{.Status}}" | indent
    echo
    check_pass "All four control plane services are running (health checks passed)."
else
    say "The control plane runs on the VM, started by demo/cloud/control-plane.sh."
    check_pass "Broker reachable at $MQTT_HOST:$MQTT_PORT"
    if [[ "$REGISTRY_REACHABLE" == 1 ]]; then
        check_pass "Software registry reachable at $REGISTRY_URL"
    else
        warn "Software registry at $REGISTRY_URL is not reachable from here (port 8080 closed?)."
    fi
    look_at_vm "the Services panel shows mosquitto, postgres and software-registry as healthy."
fi

# From here on the demo watches every device's heartbeats, events and commands over MQTT.
start_listener
if wait_until 10 "connecting the demo's MQTT listener" listener_running && sleep 2 && listener_running; then
    info "The demo's MQTT listener is watching heartbeats, events and commands."
else
    check_fail "the demo's MQTT listener could not connect to the broker at $MQTT_HOST:$MQTT_PORT" \
               "$(docker logs --tail 2 "$LISTENER" 2>&1 | tr '\n' ' ')"
fi
pause
