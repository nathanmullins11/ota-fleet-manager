"""Parse update and rollback commands (mqtt-topics-and-messages.md 4.3 and 4.4).

A bad command raises InvalidCommand. When the command names a rollout and a version,
the agent still answers with a failure event (every command gets an event); otherwise
there is nothing to address the answer to and it is only logged.
"""
import json
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from config import VERSION_RE

SUPPORTED_SCHEMA_VERSION = 1
COMMANDS = ("update", "rollback")
SHA256_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
INT8_MAX = 2**63 - 1    # rollouts.id is a BIGINT


class InvalidCommand(ValueError):
    def __init__(self, reason: str, command: Optional[str] = None,
                 rollout_id: Optional[int] = None, version: Optional[str] = None):
        super().__init__(reason)
        self.reason = reason
        # Set when known, so the agent can still send a failure event.
        self.command = command
        self.rollout_id = rollout_id
        self.version = version

    @property
    def answerable(self) -> bool:
        return None not in (self.command, self.rollout_id, self.version)


@dataclass(frozen=True)
class Command:
    command: str            # "update" or "rollback"
    rollout_id: int
    target_version: str
    download_url: str
    manifest_url: str
    signature_url: str
    sha256: str

    @property
    def failed_event(self) -> str:
        return f"{self.command}_failed"


def _absolute_http_url(value) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def parse_command(payload: bytes) -> Command:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise InvalidCommand("payload is not valid JSON") from None
    if not isinstance(data, dict):
        raise InvalidCommand("payload is not a JSON object")

    # Identify the command first, so later errors can still be answered with an event.
    command = data.get("command") if data.get("command") in COMMANDS else None
    rollout_id = data.get("rollout_id")
    if type(rollout_id) is not int or not 1 <= rollout_id <= INT8_MAX:
        rollout_id = None
    version = data.get("target_version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        version = None

    def fail(reason: str):
        raise InvalidCommand(reason, command, rollout_id, version)

    schema_version = data.get("schema_version")
    if type(schema_version) is not int or schema_version != SUPPORTED_SCHEMA_VERSION:
        fail(f"unsupported schema_version {schema_version!r}")
    if command is None:
        fail(f"command must be one of {list(COMMANDS)}")
    if rollout_id is None:
        fail("rollout_id must be a positive integer")
    if version is None:
        fail("target_version must look like '1.0' or '2.1.3'")
    for key in ("download_url", "manifest_url", "signature_url"):
        if not _absolute_http_url(data.get(key)):
            fail(f"{key} must be an absolute http(s) URL")
    sha256 = data.get("sha256")
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        fail("sha256 must be 64 lowercase hexadecimal characters")

    return Command(command=command, rollout_id=rollout_id, target_version=version,
                   download_url=data["download_url"], manifest_url=data["manifest_url"],
                   signature_url=data["signature_url"], sha256=sha256)
