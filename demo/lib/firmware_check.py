"""Demo: list the signed firmware in the registry, then verify a package the way a device
will, and show tampered variants being rejected.

  python demo/lib/firmware_check.py --registry http://<host>:8080 [--version 2.0]

Uses verify_package() from tools/release.py, the reference for the device agent's checks.
Downloads are only modified in memory; nothing on the registry changes.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

spec = importlib.util.spec_from_file_location("release", REPO / "tools" / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

COLOR = os.environ.get("FORCE_COLOR") == "1" or sys.stdout.isatty()
GREEN, RED, DIM, BOLD, RESET = (("\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m")
                                if COLOR else ("",) * 5)


def show_versions(registry: str) -> None:
    with urllib.request.urlopen(f"{registry}/versions", timeout=10) as resp:
        versions = json.load(resp)
    print(f"   {BOLD}Firmware in the registry{RESET}")
    print(f"      {'version':<9}{'sha256':<20}{'size':>7}  notes")
    for v in versions:
        print(f"      {v['version']:<9}{v['sha256'][:16] + '…':<20}{v['size_bytes']:>6}B  {v['notes']}")
    print()


def run_cases(registry: str, version: str) -> list[dict]:
    """Verify a genuine download, then tampered variants of it (in memory).

    Returns one dict per case: title, expected (True = should be accepted), accepted, reason.
    Also used by the visual demo.
    """
    manifest, signature, package = release.load_published(version, None, registry)
    public_key = release.load_public_key(release.PUBLIC_KEY.read_bytes())
    sha = json.loads(manifest)["sha256"]

    flipped = bytearray(package)
    flipped[len(flipped) // 2] ^= 0x01
    evil = b"malicious firmware"
    forged = json.loads(manifest)
    forged.update(sha256=hashlib.sha256(evil).hexdigest(), size_bytes=len(evil))
    forged_manifest = (json.dumps(forged, indent=2, sort_keys=True) + "\n").encode()
    attacker_key = release.Ed25519PrivateKey.generate()

    cases = [
        ("Genuine package, as published", True,
         dict(manifest=manifest, signature=signature, package=package, expected_sha256=sha)),
        ("One bit of the package flipped in transit", False,
         dict(manifest=manifest, signature=signature, package=bytes(flipped))),
        ("Package replaced, manifest checksum rewritten to match", False,
         dict(manifest=forged_manifest, signature=signature, package=evil)),
        ("Same forgery, re-signed with the attacker's own key", False,
         dict(manifest=forged_manifest, signature=release.sign(forged_manifest, attacker_key),
              package=evil)),
        (f"Genuine {version} offered for an update to another version (replay)", False,
         dict(manifest=manifest, signature=signature, package=package,
              expected_version="9.9")),
    ]
    results = []
    for title, should_pass, kwargs in cases:
        kwargs.setdefault("expected_version", version)
        try:
            release.verify_package(public_key=public_key, **kwargs)
            accepted, reason = True, ""
        except release.VerificationError as exc:
            accepted, reason = False, str(exc)
        results.append({"title": title, "expected": should_pass, "accepted": accepted,
                        "reason": reason})
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True)
    parser.add_argument("--version", default="2.0")
    args = parser.parse_args()
    registry, version = args.registry.rstrip("/"), args.version

    show_versions(registry)
    manifest, signature, package = release.load_published(version, None, registry)
    print(f"   {BOLD}Downloaded {version}:{RESET} manifest, signature "
          f"{DIM}{signature.strip()[:24].decode()}…{RESET}, package ({len(package)} bytes)")
    print(f"   {DIM}A device checks: signature → version → checksum from the command → "
          f"package checksum{RESET}\n")

    unexpected = 0
    for case in run_cases(registry, version):
        if case["accepted"]:
            result = f"{GREEN}ACCEPTED{RESET}"
        else:
            result = f"{RED}REJECTED{RESET}  {DIM}{case['reason']}{RESET}"
        ok = case["accepted"] == case["expected"]
        unexpected += not ok
        mark = "✔" if ok else f"{RED}✘ unexpected{RESET}"
        print(f"   {mark} {case['title']:<62} {result}")
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
