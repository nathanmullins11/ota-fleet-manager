"""Database access: the orchestrator is the only component that writes to the registry.

Each Database object owns one connection and must be used from one thread only.
main.py creates one for the MQTT callback thread and one for the main thread.
"""
import logging

import psycopg
from psycopg import sql

from config import Config
from messages import Heartbeat

log = logging.getLogger("orchestrator.db")

UPSERT_DEVICE = """
    INSERT INTO devices (device_id, current_version, status, error_count, crash_count,
                         cpu_percent, memory_mb, last_heartbeat)
    VALUES (%(device_id)s, %(version)s, %(status)s, %(error_count)s, %(crash_count)s,
            %(cpu_percent)s, %(memory_mb)s, now())
    ON CONFLICT (device_id) DO UPDATE
      SET current_version = EXCLUDED.current_version,
          status          = EXCLUDED.status,
          error_count     = EXCLUDED.error_count,
          crash_count     = EXCLUDED.crash_count,
          cpu_percent     = EXCLUDED.cpu_percent,
          memory_mb       = EXCLUDED.memory_mb,
          last_heartbeat  = EXCLUDED.last_heartbeat,
          updated_at      = now()
"""

INSERT_HEARTBEAT = """
    INSERT INTO heartbeats (device_id, reported_version, status, error_count, crash_count,
                            cpu_percent, memory_mb, reported_at)
    VALUES (%(device_id)s, %(version)s, %(status)s, %(error_count)s, %(crash_count)s,
            %(cpu_percent)s, %(memory_mb)s, %(timestamp)s)
"""

MARK_OFFLINE = """
    UPDATE devices
       SET status = 'offline', updated_at = now()
     WHERE last_heartbeat < now() - make_interval(secs => %s)
       AND status <> 'offline'
    RETURNING device_id
"""


class Database:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._conn = None

    def _connect(self):
        c = self._cfg
        self._conn = psycopg.connect(
            host=c.db_host, port=c.db_port, dbname=c.db_name,
            user=c.db_user, password=c.db_password, connect_timeout=5,
            # Autocommit: plain reads do not leave a transaction open, and every
            # `with conn.transaction()` block is a real BEGIN ... COMMIT.
            autocommit=True)
        log.info("Connected to database %s at %s:%s", c.db_name, c.db_host, c.db_port)

    def _run(self, work):
        """Run work(conn), reconnecting once if the connection was lost."""
        for attempt in (1, 2):
            try:
                if self._conn is None or self._conn.closed:
                    self._connect()
                return work(self._conn)
            except psycopg.OperationalError as exc:
                log.warning("Database connection problem (attempt %d): %s", attempt, exc)
                self.close()
                if attempt == 2:
                    raise

    def run(self, work):
        """Run work(conn) on this object's connection; see _run. Used by rollouts.py."""
        return self._run(work)

    def record_heartbeat(self, hb: Heartbeat) -> None:
        params = {
            "device_id": hb.device_id, "version": hb.version, "status": hb.status,
            "error_count": hb.error_count, "crash_count": hb.crash_count,
            "cpu_percent": hb.cpu_percent, "memory_mb": hb.memory_mb,
            "timestamp": hb.timestamp,
        }

        def work(conn):
            with conn.transaction():          # both statements succeed or neither does
                conn.execute(UPSERT_DEVICE, params)
                conn.execute(INSERT_HEARTBEAT, params)

        self._run(work)

    def record_rejected(self, device_id: str, topic: str, kind: str, reason: str,
                        payload: bytes = b"") -> None:
        """A message the system refused, for the dashboard's security panel."""
        text = payload[:300].decode("utf-8", errors="replace") if payload else None

        def work(conn):
            with conn.transaction():
                conn.execute("INSERT INTO rejected_messages (device_id, topic, kind, reason, "
                             "payload) VALUES (%s, %s, %s, %s, %s)",
                             (device_id, topic, kind, reason[:500], text))

        self._run(work)

    def mark_stale_offline(self, timeout_seconds: int) -> list:
        """Mark devices with no recent heartbeat as offline; return their IDs."""
        def work(conn):
            with conn.transaction():
                return [row[0] for row in conn.execute(MARK_OFFLINE, (timeout_seconds,))]

        return self._run(work)

    def ensure_dashboard_reader(self, user: str, password: str) -> None:
        """Create or update the dashboard's login: it can read every table and view, now and
        later, and write nothing. Safe to run on every start."""
        def work(conn):
            role = sql.Identifier(user)
            with conn.transaction():
                exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                                      (user,)).fetchone()
                verb = sql.SQL("ALTER ROLE" if exists else "CREATE ROLE")
                conn.execute(sql.SQL("{} {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                     "PASSWORD {}").format(verb, role, sql.Literal(password)))
                conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(self._cfg.db_name), role))
                conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
                conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}")
                             .format(role))
                # Tables and views created later by this owner (migrations) too.
                conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                                     "GRANT SELECT ON TABLES TO {}").format(role))
                # Belt and braces: the login's sessions are read-only even if a grant slips.
                conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on")
                             .format(role))

        self._run(work)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
