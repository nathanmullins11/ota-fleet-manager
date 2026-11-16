# Step: the control plane is up, started by one compose command.

step "Control plane up from one Docker Compose command" "NF-6 Reproducibility"

if [[ "$CP_LOCAL" == 1 ]]; then
    say "Starting the broker, device registry, software registry, orchestrator and dashboard:"
    run_note "docker compose up -d --build --wait"
    pause 1
    if ! output=$(cp_compose up -d --build --wait --quiet-pull 2>&1); then
        printf '%s\n' "$output" | tail -n 15 | indent
        die "The control plane did not start."
    fi
    cp_compose ps --format "table {{.Service}}\t{{.Status}}" | indent
    echo
    check_pass "All five control plane services are running (health checks passed)."
    # The dashboard reads the database through the read-only login the orchestrator creates.
    dashboard_reads() {
        curl -sf -u "admin:$GRAFANA_ADMIN_PASSWORD" \
            "http://localhost:3000/api/datasources/uid/ota-postgres/health" | grep -c '"OK"' >/dev/null
    }
    if wait_until 40 "waiting for the dashboard to reach the database" dashboard_reads; then
        check_pass "Dashboard is live at http://localhost:3000 and reads the fleet database (read-only login)"
    else
        check_fail "the dashboard cannot read the fleet database" \
                   "$(curl -s -u "admin:$GRAFANA_ADMIN_PASSWORD" http://localhost:3000/api/datasources/uid/ota-postgres/health)"
    fi
    info "Open http://localhost:3000 to follow the rest of the demo live."
else
    say "The control plane runs on the VM, started by demo/cloud/control-plane.sh."
    check_pass "Broker reachable at $MQTT_HOST:$MQTT_PORT"
    if [[ "$REGISTRY_REACHABLE" == 1 ]]; then
        check_pass "Software registry reachable at $REGISTRY_URL"
    else
        warn "Software registry at $REGISTRY_URL is not reachable from here (port 8080 closed?)."
    fi
    look_at_vm "the Services panel shows mosquitto, postgres, software-registry and grafana as healthy."
    info "Dashboard: http://$MQTT_HOST:3000 (open port 3000 to your IP in the security group)."
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
