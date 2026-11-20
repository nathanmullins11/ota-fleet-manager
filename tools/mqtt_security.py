#!/usr/bin/env python3
"""Broker security setup: TLS certificates and client passwords for Mosquitto (FR-7, NF-3).

  python tools/mqtt_security.py setup [--out DIR] [--host NAME_OR_IP ...] [--devices N]
  python tools/mqtt_security.py device-password device-001     # reads DEVICE_SECRET

`setup` writes, into DIR (default deploy/mqtt/generated/, git-ignored):

  ca.crt, ca.key        a private certificate authority. ca.crt is given to every client so it
                        can verify the broker; ca.key never leaves this machine
  broker.crt, broker.key  the broker's certificate, valid for mosquitto, localhost,
                        host.docker.internal, 127.0.0.1 and every --host (e.g. the VM's IP)
  passwords             Mosquitto password file: device-001 .. device-N, orchestrator, ops
  device.env            DEVICE_SECRET and MQTT_OPS_PASSWORD, for the machine that launches the
                        devices and runs the demo (copy it there with ca.crt)

Existing certificates are kept (so clients keep trusting them) unless a --host is missing
from the broker certificate, in which case only the broker certificate is reissued.

Secrets come from the environment or deploy/.env:

  DEVICE_SECRET               each device's password is derived from it and the device ID
  MQTT_ORCHESTRATOR_PASSWORD  the orchestrator's broker password
  MQTT_OPS_PASSWORD           the ops account (demo and admin tooling: may use every topic)

A device's password is HMAC-SHA256(DEVICE_SECRET, device ID), first 32 hex characters: every
device has its own password, and the launcher gives each device only its own.
"""
import argparse
import base64
import datetime as dt
import hashlib
import hmac
import ipaddress
import os
import secrets
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "deploy" / "mqtt" / "generated"
ENV_FILE = REPO / "deploy" / ".env"
DEFAULT_HOSTS = ["mosquitto", "localhost", "host.docker.internal", "127.0.0.1"]
DEFAULT_DEVICES = 100
# Mosquitto 2's own format ($7$ = PBKDF2-HMAC-SHA512), as written by mosquitto_passwd.
PBKDF2_ITERATIONS = 1000
SALT_BYTES = 64


class SetupError(Exception):
    pass


# --------------------------------------------------------------------------- passwords

def device_password(secret: str, device_id: str) -> str:
    """The broker password of one device, derived from DEVICE_SECRET."""
    if not secret:
        raise SetupError("DEVICE_SECRET is not set")
    return hmac.new(secret.encode(), device_id.encode(), hashlib.sha256).hexdigest()[:32]


def mosquitto_hash(password: str, salt: bytes | None = None,
                   iterations: int = PBKDF2_ITERATIONS) -> str:
    """A password-file hash in the format mosquitto_passwd writes."""
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, iterations, 64)
    return (f"$7${iterations}${base64.b64encode(salt).decode()}"
            f"${base64.b64encode(digest).decode()}")


def password_file(device_secret: str, orchestrator_password: str, ops_password: str,
                  device_count: int) -> str:
    lines = [f"orchestrator:{mosquitto_hash(orchestrator_password)}",
             f"ops:{mosquitto_hash(ops_password)}"]
    for i in range(1, device_count + 1):
        device_id = f"device-{i:03d}"
        lines.append(f"{device_id}:{mosquitto_hash(device_password(device_secret, device_id))}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- certificates

def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _pem_key(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _san(hosts: list[str]) -> x509.SubjectAlternativeName:
    names = []
    for host in hosts:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    return x509.SubjectAlternativeName(names)


def make_ca(days: int = 3650):
    key = _key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OTA Fleet Manager broker CA")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def make_broker_cert(ca_key, ca_cert, hosts: list[str], days: int = 825):
    key = _key()
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(_san(hosts), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def cert_hosts(cert) -> set[str]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set()
    return {str(n.value) for n in san}


# --------------------------------------------------------------------------- setup

def read_env_file(path: Path) -> dict:
    values = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    except OSError:
        pass
    return values


def secret(name: str, env_file: dict) -> str:
    value = os.environ.get(name) or env_file.get(name, "")
    if not value:
        raise SetupError(f"{name} is not set (environment or deploy/.env)")
    return value


def _write(path: Path, data: bytes, mode: int) -> None:
    path.write_bytes(data)
    path.chmod(mode)


def setup(out: Path, hosts: list[str], devices: int, env: dict) -> list[str]:
    """Create or update the broker's security files. Returns what was done."""
    out.mkdir(parents=True, exist_ok=True)
    done = []
    wanted = list(dict.fromkeys(DEFAULT_HOSTS + hosts))

    ca_key_path, ca_cert_path = out / "ca.key", out / "ca.crt"
    if ca_key_path.exists() and ca_cert_path.exists():
        ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
        ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    else:
        ca_key, ca_cert = make_ca()
        _write(ca_key_path, _pem_key(ca_key), 0o600)
        _write(ca_cert_path, ca_cert.public_bytes(serialization.Encoding.PEM), 0o644)
        done.append("created a new certificate authority (ca.crt): clients must be given it")

    broker_cert_path, broker_key_path = out / "broker.crt", out / "broker.key"
    current = None
    if broker_cert_path.exists() and broker_key_path.exists():
        current = x509.load_pem_x509_certificate(broker_cert_path.read_bytes())
        if current.issuer != ca_cert.subject or not set(wanted) <= cert_hosts(current):
            current = None
    if current is None:
        key, cert = make_broker_cert(ca_key, ca_cert, wanted)
        # Readable by the broker's own user inside its container; the directory is git-ignored.
        _write(broker_key_path, _pem_key(key), 0o644)
        _write(broker_cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)
        done.append(f"issued the broker certificate for {', '.join(wanted)}")

    device_secret, ops_password = secret("DEVICE_SECRET", env), secret("MQTT_OPS_PASSWORD", env)
    _write(out / "passwords",
           password_file(device_secret, secret("MQTT_ORCHESTRATOR_PASSWORD", env),
                         ops_password, devices).encode(), 0o644)
    done.append(f"wrote the password file: device-001 to device-{devices:03d}, orchestrator, ops")
    # For the machine that launches devices (each gets only its own derived password) and runs
    # the demo's listener and test messages (the ops account).
    _write(out / "device.env",
           f"DEVICE_SECRET={device_secret}\nMQTT_OPS_PASSWORD={ops_password}\n".encode(), 0o600)
    return done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup", help="create certificates and the broker's password file")
    s.add_argument("--out", type=Path, default=DEFAULT_OUT)
    s.add_argument("--host", action="append", default=[], help="extra broker name or IP (repeat)")
    s.add_argument("--devices", type=int, default=DEFAULT_DEVICES)
    s.add_argument("--env-file", type=Path, default=ENV_FILE)
    p = sub.add_parser("device-password", help="print one device's broker password")
    p.add_argument("device_id")
    p.add_argument("--env-file", type=Path, default=ENV_FILE)
    args = ap.parse_args(argv)
    env = read_env_file(args.env_file)
    try:
        if args.command == "setup":
            for line in setup(args.out, args.host, args.devices, env):
                print(line)
        else:
            print(device_password(secret("DEVICE_SECRET", env), args.device_id))
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
