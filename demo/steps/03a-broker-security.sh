# Step: the broker is TLS-only, every client signs in, and each device is limited to its own
# topics (FR-7, NF-3; STRIDE S1, S2, I3).

step "Broker security: TLS only, signed-in clients, each device limited to its own topics" \
     "FR-7 secure communication, NF-3 security, NF-5 failed sign-ins logged"

say "Every connection to the broker is encrypted (TLS on port 8883) and signed in with the"
say "client's own account. The demo now tries what an attacker on the network would:"
echo
since=$(now_utc)
broker_host=$MQTT_HOST
[[ "$CP_LOCAL" == 1 ]] && broker_host=127.0.0.1

# 1. No plain-text MQTT at all.
if port_open "$broker_host" 1883; then
    check_fail "port 1883 (plain-text MQTT) is open" "anyone could read heartbeats, events and commands"
else
    check_pass "No plain-text MQTT: port 1883 is closed; the broker only accepts TLS on 8883"
fi

# 2. and 3. Signing in without an account, or with a wrong password.
out=$(mqtt_client "" "" "ota/devices/device-002/heartbeat" '{"probe": "anonymous"}')
if [[ $? -ne 0 ]]; then
    check_pass "An anonymous client is refused: ${out//$'\n'/ }"
else
    check_fail "the broker accepted an anonymous client" "$out"
fi
out=$(mqtt_client device-002 "wrong-password" "ota/devices/device-002/heartbeat" '{"probe": "wrong password"}')
if [[ $? -ne 0 ]]; then
    check_pass "A wrong password is refused: ${out//$'\n'/ }"
else
    check_fail "the broker accepted a wrong password" "$out"
fi

# 4. A client that does not trust our CA cannot complete the TLS handshake: the broker's
# certificate is really checked, so a fake broker could not pose as ours either.
out=$(docker run --rm "${HOST_GATEWAY[@]}" "$MQTT_IMAGE" \
      mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" --capath /etc/ssl/certs \
      -u ops -P "$MQTT_OPS_PASSWORD" -t probe -m x 2>&1)
if [[ $? -ne 0 ]]; then
    check_pass "Certificates are verified: a client that does not trust our CA cannot connect"
else
    check_fail "a client connected without trusting our CA" "$out"
fi

# 5. A device signed in with its own (real) password cannot speak for another device or send
# itself commands: the broker drops those publishes, so the listener never sees them.
marker="probe-$RANDOM$RANDOM"
mark=$(listener_mark)
pw2=$(device_password device-002)
mqtt_client device-002 "$pw2" "ota/devices/device-005/heartbeat" "{\"probe\": \"$marker\"}" >/dev/null
mqtt_client device-002 "$pw2" "ota/devices/device-002/commands" "{\"probe\": \"$marker\"}" >/dev/null
mqtt_client device-002 "$pw2" "ota/devices/device-002/heartbeat" "{\"probe\": \"$marker-own\"}" >/dev/null
sleep 3
seen=$(lines_since "$mark" | grep -c "$marker\"" || true)
own=$(lines_since "$mark" | grep -c "$marker-own" || true)
if [[ "$seen" == 0 && "$own" -ge 1 ]]; then
    check_pass "device-002 can publish only its own topics: its heartbeat as device-005 and a command to itself were dropped"
else
    check_fail "the access list did not limit device-002 to its own topics" \
               "foreign messages delivered: $seen; own heartbeat delivered: $own"
fi

# 6. Failed sign-ins are recorded (NF-5).
if [[ "$CP_LOCAL" == 1 ]]; then
    refused=$(cp_compose logs --no-log-prefix --since "$since" mosquitto 2>/dev/null \
              | grep -c -iE "not authori[sz]ed|bad user name or password" || true)
    if ((refused >= 2)); then
        check_pass "The broker logged the $refused refused sign-ins"
        cp_compose logs --no-log-prefix --since "$since" mosquitto 2>/dev/null \
            | grep -iE "not authori[sz]ed|bad user name or password" | tail -n 2 | cut -c1-130 | indent
    else
        check_fail "the refused sign-ins are not in the broker's log" "found $refused"
    fi
else
    look_at_vm "docker compose logs mosquitto shows the refused sign-ins."
fi
pause
