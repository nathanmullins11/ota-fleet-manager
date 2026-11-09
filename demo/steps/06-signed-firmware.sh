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
if ! tools_run demo/lib/firmware_check.py --registry "$REGISTRY_URL" --version 2.0; then
    bad "A check did not behave as expected (see above)."
fi
echo
ok "Only the genuine, correctly signed package for the requested version is accepted."
info "Devices perform these checks themselves once the update logic lands (Iteration 2)."
pause
