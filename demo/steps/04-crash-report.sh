# Step: faulty firmware 2.1 crashes on a device; the crash is reported, not mistaken for offline.
#
# Real (default): roll out 2.1 to device-003 and check what the device does, as described in
# firmware/README.md: install, report 2.1, errors after ~10 s, crash after ~15 s (the agent
# keeps heartbeating with crash_count), then roll back to 1.0.
# --simulate: publish the heartbeat such a device would send instead.

if [[ "$SIMULATE" == 1 ]]; then
    step "Firmware crashes are recorded separately from errors (SIMULATED)" \
         "FR-4 Health signals (groundwork), FR-8 Device registry"
    say "When firmware crashes, the agent keeps running and reports it in the heartbeat's crash_count,"
    say "so the orchestrator can tell a crashed device (heartbeats continue) from an offline one."
    say "${YELLOW}SIMULATED:${RESET} the demo sends the heartbeat a device-003 running faulty 2.1 would send:"
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
    else
        look_at_vm "the Recent crashes panel shows device-003, version 2.1, 25 errors, 1 crash."
    fi
    pause
    return 0
fi

step "Faulty firmware 2.1: the crash is reported, not mistaken for offline" \
     "FR-3 install, FR-4 health signals, FR-8 device registry"

say "Rolling out the faulty firmware 2.1 to device-003 only. Expected (firmware/README.md):"
say "it installs and reports 2.1, runs healthy for ~10 s, reports errors, crashes at ~15 s, and the"
say "agent keeps sending heartbeats with crash_count while it restarts the firmware."
echo
mark=$(listener_mark)
t0=$(now_utc)
pid_before=$(firmware_pid device-003)
container_before=$(container_info device-003)
if begin_rollout 2.1 f "faulty 2.1 to device-003" --devices device-003; then
    crash_rid=$RID
    if expect "$(event_timeout)" "device-003 installs 2.1 and reports update_succeeded" \
            "$mark" device-003 event_seen "$mark" device-003 update_succeeded "$crash_rid"; then
        expect 20 "device-003's heartbeat reports version 2.1" "$mark" device-003 \
            hb_is "$mark" device-003 version 2.1 || true
        expect 35 "2.1 misbehaves: device-003's heartbeats report errors" "$mark" device-003 \
            hb_ever_above "$mark" device-003 error_count 0 || true
        expect 35 "2.1 crashes: device-003 reports crash_count > 0 (heartbeats continue)" "$mark" device-003 \
            hb_ever_above "$mark" device-003 crash_count 0 || true
        expect 10 "device-003 reports status unhealthy" "$mark" device-003 \
            hb_ever_is "$mark" device-003 status unhealthy || true

        # Proof from inside the container: the firmware process crashed, the device did not.
        restarted() { device_log device-003 "$t0" "Restarting firmware 2.1" | grep -q .; }
        wait_until 8 "waiting for the agent to restart the firmware" restarted || true
        crash_line=$(device_log device-003 "$t0" "Firmware 2.1 crashed" | head -n 1)
        restart_line=$(device_log device-003 "$t0" "Restarting firmware 2.1" | head -n 1)
        container_after=$(container_info device-003)
        # Every firmware 2.1 process the agent started, from its log: "Firmware 2.1 started (pid N)".
        mapfile -t pids_21 < <(device_log device-003 "$t0" "Firmware 2.1 started" | sed -n 's/.*(pid \([0-9]*\)).*/\1/p')
        if [[ -n "$crash_line" && "$container_after" == "$container_before" ]]; then
            check_pass "The firmware crashed, the device did not: same container since ${container_before:11:8} UTC, ${container_before##* } restarts"
        else
            check_fail "No firmware crash in device-003's own log while its container stayed up" \
                       "crash line: ${crash_line:-none}; container before/after: $container_before / $container_after"
        fi
        [[ -n "$crash_line" ]] && proof "device-003's log: $crash_line"
        [[ -n "$restart_line" ]] && proof "device-003's log: $restart_line"
        if ((${#pids_21[@]} >= 2)); then
            proof "firmware processes: pid ${pid_before:-?} (1.0) → pid ${pids_21[0]} (2.1, crashed) → pid ${pids_21[1]} (2.1, restarted by the agent)"
        else
            proof "firmware process: pid ${pid_before:-?} (1.0) before the update, now $(describe_process device-003)"
        fi
        crash_log() { device_log device-003 "$t0" | tail -n 30; }
        detail "device-003's log since the rollout started (install, checks, crash, restart):" crash_log
    else
        DEVICES_SILENT=1
        reason=$(event_reason "$mark" device-003 update_failed "$crash_rid")
        [[ -n "$reason" ]] && info "device-003 reported update_failed: $reason"
        check_skip "2.1 running, errors and crash reports on device-003" "2.1 was not installed"
    fi
    echo
    show_resources "device-003 running faulty 2.1"
    if [[ "$CP_LOCAL" == 1 ]]; then
        echo
        say "device-003's heartbeat history in the device registry (newest first):"
        cp_psql -c "SELECT reported_version AS version, status, error_count AS errors,
                           crash_count AS crashes, to_char(received_at, 'HH24:MI:SS') AS received
                      FROM heartbeats WHERE device_id = 'device-003'
                     ORDER BY id DESC LIMIT 5;" | sed '/^$/d' | indent
    else
        look_at_vm "device-003 shows 2.1, then errors, then the Recent crashes panel lists its crash."
    fi
    pause

    say "Rolling device-003 back to 1.0 so the next steps start from a clean fleet."
    rb_mark=$(listener_mark)
    rb_time=$(now_utc)
    installed=0
    event_seen "$mark" device-003 update_succeeded "$crash_rid" && installed=1
    if begin_rollback "$crash_rid"; then
        if expect "$(event_timeout)" "device-003 rolls back and reports rollback_succeeded" \
                "$rb_mark" device-003 event_seen "$rb_mark" device-003 rollback_succeeded "$crash_rid"; then
            if [[ "$installed" == 1 ]]; then
                expect 20 "device-003's heartbeat reports 1.0 again" "$rb_mark" device-003 \
                    hb_is "$rb_mark" device-003 version 1.0 || true
                kept=$(device_log device-003 "$rb_time" "Using the kept copy of 1.0" | head -n 1)
                downloaded=$(device_log device-003 "$rb_time" "Downloading 1.0")
                if [[ "$(firmware_running_version device-003)" == 1.0 && -n "$kept" && -z "$downloaded" ]]; then
                    check_pass "device-003 runs firmware 1.0 again, restored from its kept copy without a download ($(describe_process device-003))"
                else
                    check_fail "device-003 did not restore 1.0 from its kept copy" \
                               "running: $(describe_process device-003); log: ${kept:-no kept-copy line}${downloaded:+; it downloaded 1.0}"
                fi
                if [[ "$CP_LOCAL" == 1 ]]; then
                    detail "The orchestrator's record of rollout $crash_rid (NF-5 decision log):" cli rollout status "$crash_rid"
                fi
            fi
        else
            DEVICES_SILENT=1
        fi
    fi
fi
pause
