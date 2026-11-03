"""Database access: the orchestrator is the only component that writes to the registry.

Each Database object owns one connection and must be used from one thread only.
main.py creates one for the MQTT callback thread and one for the main thread.
"""
import logging

import psycopg

from config import Config
from messages import Heartbeat

log = logging.getLogger("orchestrator.db")

UPSERT_DEVICE = """
    INSERT INTO devices (device_id, current_version, status, error_count, last_heartbeat)
    VALUES (%(device_id)s, %(version)s, %(status)s, %(error_count)s, now())
    ON CONFLICT (device_id) DO UPDATE
      SET current_version = EXCLUDED.current_version,
          status          = EXCLUDED.status,
          error_count     = EXCLUDED.error_count,
          last_heartbeat  = EXCLUDED.last_heartbeat,
          updated_at      = now()
"""

INSERT_HEARTBEAT = """
    INSERT INTO heartbeats (device_id, reported_version, status, error_count, reported_at)
    VALUES (%(device_id)s, %(version)s, %(status)s, %(error_count)s, %(timestamp)s)
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
            user=c.db_user, password=c.db_password, connect_timeout=5)
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

    def record_heartbeat(self, hb: Heartbeat) -> None:
        params = {
            "device_id": hb.device_id, "version": hb.version, "status": hb.status,
            "error_count": hb.error_count, "timestamp": hb.timestamp,
        }

        def work(conn):
            with conn.transaction():          # both statements succeed or neither does
                conn.execute(UPSERT_DEVICE, params)
                conn.execute(INSERT_HEARTBEAT, params)

        self._run(work)

    def mark_stale_offline(self, timeout_seconds: int) -> list:
        """Mark devices with no recent heartbeat as offline; return their IDs."""
        def work(conn):
            with conn.transaction():
                return [row[0] for row in conn.execute(MARK_OFFLINE, (timeout_seconds,))]

        return self._run(work)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
