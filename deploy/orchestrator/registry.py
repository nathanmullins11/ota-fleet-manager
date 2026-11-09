"""Client for the software registry's /versions endpoint (standard library only)."""
import json
import re
import urllib.request
from dataclasses import dataclass

VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?", re.ASCII)
SHA256_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class FirmwareVersion:
    version: str
    sha256: str
    notes: str
    package_path: str      # relative, e.g. /packages/2.0


def parse_versions(data) -> list[FirmwareVersion]:
    """Validate the registry's /versions response. Bad entries raise rather than being skipped,
    because a wrong checksum here would go out in every command."""
    if not isinstance(data, list):
        raise RegistryError("/versions did not return a list")
    result = []
    for item in data:
        if not isinstance(item, dict):
            raise RegistryError("/versions entry is not an object")
        version, sha = item.get("version"), item.get("sha256")
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            raise RegistryError(f"invalid version in /versions: {version!r}")
        if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha):
            raise RegistryError(f"invalid sha256 for version {version}")
        notes = item.get("notes") if isinstance(item.get("notes"), str) else ""
        result.append(FirmwareVersion(version, sha, notes, f"/packages/{version}"))
    return result


def fetch_versions(registry_url: str, timeout: float = 5) -> list[FirmwareVersion]:
    try:
        with urllib.request.urlopen(f"{registry_url}/versions", timeout=timeout) as resp:
            data = json.load(resp)
    except (OSError, ValueError) as exc:
        raise RegistryError(f"cannot read {registry_url}/versions: {exc}") from exc
    return parse_versions(data)
