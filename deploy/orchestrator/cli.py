"""Operator commands for rollouts. Run inside the orchestrator container, from deploy/:

  docker compose exec orchestrator python cli.py versions
  docker compose exec orchestrator python cli.py devices
  docker compose exec orchestrator python cli.py rollout start 2.0 --all
  docker compose exec orchestrator python cli.py rollout start 2.0 --devices device-001,device-002
  docker compose exec orchestrator python cli.py rollout status [ID] [--watch]
  docker compose exec orchestrator python cli.py rollout list
  docker compose exec orchestrator python cli.py rollout rollback ID [--reason TEXT]

The CLI only records requests in the database. The running orchestrator sends the
commands (within about a second) and records what the devices report, so the
orchestrator stays the single component that publishes commands and makes decisions.
"""
import argparse
import sys
import time

from config import load_config
from db import Database
from registry import RegistryError, fetch_versions
from rollout_logic import RolloutError, plan_targets
from rollouts import RolloutStore

TERMINAL_STATES = ("completed", "rolled_back")


def _table(headers: list, rows: list) -> None:
    rows = [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
              for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)))


def _time(ts) -> str:
    return ts.strftime("%H:%M:%S") if ts else ""


def sync(cfg, store: RolloutStore) -> None:
    try:
        store.sync_firmware(fetch_versions(cfg.registry_url))
    except RegistryError as exc:
        raise RolloutError(f"cannot sync firmware from the registry: {exc}") from exc


def cmd_versions(cfg, store, args) -> int:
    sync(cfg, store)
    firmware = store.firmware()
    _table(["version", "sha256", "notes"],
           [(v, sha[:16] + "…", notes) for v, (sha, notes) in
            sorted(firmware.items(), key=lambda kv: tuple(int(p) for p in kv[0].split(".")))])
    return 0


def cmd_devices(cfg, store, args) -> int:
    rows = store.db.run(lambda conn: conn.execute(
        "SELECT device_id, current_version, previous_version, status, "
        "       EXTRACT(EPOCH FROM (now() - last_heartbeat))::INTEGER "
        "  FROM devices ORDER BY device_id").fetchall())
    _table(["device", "version", "previous", "status", "last seen (s)"], rows)
    return 0


def cmd_start(cfg, store, args) -> int:
    sync(cfg, store)
    firmware = store.firmware()
    if args.version not in firmware:
        raise RolloutError(f"version {args.version} is not in the registry "
                           f"(available: {', '.join(sorted(firmware))})")
    if args.rollback_to and args.rollback_to not in firmware:
        raise RolloutError(f"rollback version {args.rollback_to} is not in the registry")
    requested = None if args.all else [d.strip() for d in args.devices.split(",") if d.strip()]

    plan = plan_targets(store.devices(), args.version, requested, args.rollback_to)
    rid = store.create_rollout(args.version, plan, origin="manual")

    print(f"Rollout {rid} created: update to {args.version}, rollback version "
          f"{plan.rollback_version}")
    print(f"  commanding:        {', '.join(plan.to_command)}")
    if plan.already_on_target:
        print(f"  already on {args.version}:   {', '.join(plan.already_on_target)}")
    if plan.skipped_offline:
        print(f"  skipped (offline): {', '.join(plan.skipped_offline)}")
    print(f"\nThe orchestrator sends the commands now. Follow it with:\n"
          f"  python cli.py rollout status {rid} --watch")
    return 0


def _latest_id(store) -> int:
    rows = store.rollouts(limit=1)
    if not rows:
        raise RolloutError("there are no rollouts yet")
    return rows[0][0]


def _print_status(store, rid: int) -> str:
    r = store.rollout(rid)
    if r is None:
        raise RolloutError(f"rollout {rid} does not exist")
    print(f"Rollout {rid}: {r['state'].upper()}   update to {r['target_version']}, "
          f"rollback version {r['rollback_version']}   started {_time(r['started_at'])}"
          + (f"   ended {_time(r['ended_at'])}" if r["ended_at"] else ""))
    print()
    _table(["device", "rollout state", "reports version", "device status", "since"],
           [(d, s, v, st, _time(t)) for d, s, v, st, t in store.device_details(rid)])
    print()
    _table(["time", "decision", "reason"],
           [(_time(t), e, reason) for t, e, reason in store.events(rid)])
    return r["state"]


def cmd_status(cfg, store, args) -> int:
    rid = args.id or _latest_id(store)
    if not args.watch:
        _print_status(store, rid)
        return 0
    while True:
        print("\033[H\033[2J", end="")
        state = _print_status(store, rid)
        if state in TERMINAL_STATES:
            return 0
        print("\n(refreshing every 2 s; Ctrl+C to stop watching, the rollout carries on)")
        time.sleep(2)


def cmd_list(cfg, store, args) -> int:
    _table(["id", "target", "rollback", "state", "started", "devices"],
           [(i, t, rb, s, _time(st), n) for i, t, rb, s, st, n in store.rollouts(limit=20)])
    return 0


def cmd_rollback(cfg, store, args) -> int:
    store.request_rollback(args.id, args.reason)
    r = store.rollout(args.id)
    print(f"Rollback of rollout {args.id} requested: devices go back to {r['rollback_version']}.")
    print(f"Follow it with:\n  python cli.py rollout status {args.id} --watch")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description="OTA rollout commands")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("versions", help="sync and list firmware in the registry").set_defaults(
        func=cmd_versions)
    sub.add_parser("devices", help="list devices").set_defaults(func=cmd_devices)

    rollout = sub.add_parser("rollout", help="start, follow or roll back a rollout")
    rsub = rollout.add_subparsers(dest="action", required=True)

    p = rsub.add_parser("start", help="update devices to a version")
    p.add_argument("version")
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--all", action="store_true", help="every online device")
    who.add_argument("--devices", help="comma-separated device IDs")
    p.add_argument("--rollback-to", help="version to roll back to (default: the version the "
                                         "devices run now)")
    p.set_defaults(func=cmd_start)

    p = rsub.add_parser("status", help="show a rollout (default: the latest)")
    p.add_argument("id", nargs="?", type=int)
    p.add_argument("--watch", action="store_true", help="refresh until the rollout finishes")
    p.set_defaults(func=cmd_status)

    rsub.add_parser("list", help="recent rollouts").set_defaults(func=cmd_list)

    p = rsub.add_parser("rollback", help="roll a rollout's devices back")
    p.add_argument("id", type=int)
    p.add_argument("--reason", default="manual rollback requested by operator")
    p.set_defaults(func=cmd_rollback)

    args = parser.parse_args(argv)
    cfg = load_config()
    db = Database(cfg)
    try:
        return args.func(cfg, RolloutStore(db), args)
    except RolloutError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
