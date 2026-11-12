# Step: the registry serves signed firmware; tampered packages fail verification.

step "Signed firmware: a genuine package verifies, tampered ones are rejected" \
     "FR-6 Signed packages, FR-3 Verify before install (device checks), NF-5"

if [[ "$REGISTRY_REACHABLE" != 1 ]]; then
    warn "Skipped: the software registry at $REGISTRY_URL is not reachable from this machine."
    warn "Open TCP 8080 to your IP in the VM's security group to include this step."
    pause
    return 0
fi

say "Downloading firmware 2.0 from the software registry and running the same checks a device"
say "will run before installing. Then the download is altered in memory to simulate attacks."
echo
if tools_run demo/lib/firmware_check.py --registry "$REGISTRY_URL" --version 2.0; then
    echo
    check_pass "Reference checks: only the genuine, correctly signed package is accepted"
else
    check_fail "Reference checks did not behave as expected (see above)" ""
fi
pause

if [[ "$SIMULATE" == 1 ]]; then
    info "(--simulate: the forged-command test against a real device is skipped.)"
    pause
    return 0
fi

# --------------------------------------------------------------------------- real device
say "Now an attack on a real device. The broker has no authentication until Iteration 3, so"
say "anyone on the network can publish to a device's command topic. Two forged update commands"
say "are sent to device-002; it must verify before installing (FR-3) and refuse both."
echo
pkg_url="$REGISTRY_URL/packages/2.0"
sha_20=$(json_field sha256 <"$REPO_ROOT/deploy/registries/software/packages/2.0/manifest.json")

# forged_command ROLLOUT_ID TARGET_VERSION SHA256: real 2.0 URLs, attacker-chosen fields.
forged_command() {
    printf '{"schema_version": 1, "command": "update", "rollout_id": %s, "target_version": "%s", "download_url": "%s", "manifest_url": "%s/manifest", "signature_url": "%s/manifest.sig", "sha256": "%s", "issued_at": "%s"}' \
        "$1" "$2" "$pkg_url" "$pkg_url" "$pkg_url" "$3" "$(now_utc)"
}

# attack ROLLOUT_ID TARGET_VERSION "description" PAYLOAD
attack() {
    local rid=$1 version=$2 what=$3 payload=$4 m reason since checks
    m=$(listener_mark)
    since=$(now_utc)
    info "Forged: $what"
    mqtt_pub "ota/devices/device-002/commands" "$payload"
    if wait_until "$(event_timeout)" "waiting for device-002 to refuse it" \
            event_seen "$m" device-002 update_failed "$rid"; then
        reason=$(event_reason "$m" device-002 update_failed "$rid")
        check_pass "device-002 refused: $what (reason: ${reason:-none given})"
        # The device's own record of which check stopped it, and what it downloaded.
        checks=$(device_log device-002 "$since" "^[0-9:]+ $version check [0-9]/4" \
                 | sed -E "s/^[0-9:]+ $version check ([0-9])\/4 ([^:]*): ([A-Z]+).*/\1 \2 \3/" \
                 | sed -e 's/ OK$/ ✔/' -e 's/ FAILED$/ ✘/' | paste -sd'|' | sed 's/|/ · /g')
        proof "device-002's checks: ${checks:-not logged}"
        if device_log device-002 "$since" "^[0-9:]+ $version downloaded package" | grep -q .; then
            proof "it downloaded the package before refusing"
        else
            proof "it stopped before downloading the package: only the manifest and signature were fetched"
        fi
        detail "device-002's log for this command:" \
            device_log device-002 "$since" "rollout $rid|Downloading $version |^[0-9:]+ $version (check|downloaded)"
    elif event_seen "$m" device-002 update_succeeded "$rid"; then
        check_fail "device-002 INSTALLED a forged package: $what" "$(observed "$m" device-002)"
    else
        DEVICES_SILENT=1
        check_fail "device-002 did not refuse (no update_failed event): $what" "$(observed "$m" device-002)"
    fi
}

attack_mark=$(listener_mark)
pid_before=$(firmware_pid device-002)
attack 900001 2.0 "genuine 2.0 package, but the command's sha256 does not match it" \
    "$(forged_command 900001 2.0 0000000000000000000000000000000000000000000000000000000000000000)"
echo
attack 900002 2.1 "genuine 2.0 package offered as an update to 2.1 (replay)" \
    "$(forged_command 900002 2.1 "$sha_20")"
echo

# Only meaningful if device-002 actually processed the commands: a device that ignores
# every command also "stays on 1.0".
if lines_since "$attack_mark" | grep -q " ota/devices/device-002/events "; then
    m=$(listener_mark)
    sleep 6
    pid_after=$(firmware_pid device-002)
    if hb_is "$m" device-002 version 1.0 && [[ -n "$pid_after" && "$pid_after" == "$pid_before" ]]; then
        check_pass "device-002 still runs 1.0, and its firmware was never stopped (same process, pid $pid_after, before and after)"
    elif hb_is "$m" device-002 version 1.0; then
        check_fail "device-002 reports 1.0, but its firmware process changed during the attacks" \
                   "pid before ${pid_before:-none}, after ${pid_after:-none}"
    else
        check_fail "device-002 does not report 1.0 after the attacks" "$(observed "$m" device-002)"
    fi
    info "The orchestrator ignores device-002's replies: it never started rollouts 900001 or 900002."
else
    check_skip "device-002 still runs 1.0 after the attacks" \
               "it did not process the forged commands at all, so this proves nothing"
fi
pause
