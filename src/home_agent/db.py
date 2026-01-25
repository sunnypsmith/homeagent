from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, TypeVar

import psycopg

T = TypeVar("T")


@dataclass(frozen=True)
class DbConnectInfo:
    """
    Redacted-ish info for logs (never include passwords).
    """

    host: str
    port: int
    dbname: str
    user: str


class DbManager:
    """
    Simple, resilient DB connection wrapper for long-running services.

    Goals:
    - keep a single connection in normal operation (fast)
    - reconnect automatically if the connection dies (Postgres restart, network blip)
    - never log passwords/conninfo

    This is intentionally minimal (no pooling) because our workload is low-volume,
    but we still need robustness.
    """

    def __init__(
        self,
        *,
        conninfo: str,
        log_info: DbConnectInfo,
        connect_timeout_seconds: float = 10.0,
        reconnect_max_wait_seconds: float = 60.0,
    ) -> None:
        self._conninfo = str(conninfo)
        self._log_info = log_info
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._reconnect_max_wait_seconds = float(reconnect_max_wait_seconds)

        self._lock = threading.Lock()
        self._conn: Optional["psycopg.Connection[Any]"] = None
        self._closing = threading.Event()

    @property
    def log_info(self) -> DbConnectInfo:
        return self._log_info

    def close(self) -> None:
        # Make shutdown non-blocking: if a DB op is in progress on another thread,
        # we prefer to exit cleanly rather than hang waiting for a lock.
        self._closing.set()
        c: Optional["psycopg.Connection[Any]"] = None
        if self._lock.acquire(timeout=0.5):
            try:
                c = self._conn
                self._conn = None
            finally:
                self._lock.release()
        # Close outside the lock.
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def _connect_once(self) -> "psycopg.Connection[Any]":
        # psycopg3 supports connect_timeout via conninfo or kwargs; we use kwargs.
        return psycopg.connect(self._conninfo, autocommit=True, connect_timeout=self._connect_timeout_seconds)

    def ensure_connected(self) -> None:
        if self._closing.is_set():
            raise RuntimeError("db_closing")

        # Quick check under lock for an existing healthy connection.
        with self._lock:
            c = self._conn
            if c is not None and not c.closed:
                return
            # Drop any stale connection object.
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
                self._conn = None

        # Reconnect with capped backoff (do not hold the lock while sleeping/connecting).
        delay = 1.0
        deadline = time.monotonic() + max(1.0, self._reconnect_max_wait_seconds)
        last_err: Optional[BaseException] = None
        while True:
            if self._closing.is_set():
                raise RuntimeError("db_closing")
            try:
                new_conn = self._connect_once()
                with self._lock:
                    # If someone else connected first, keep the first one and close ours.
                    if self._conn is not None and not self._conn.closed:
                        try:
                            new_conn.close()
                        except Exception:
                            pass
                        return
                    self._conn = new_conn
                return
            except Exception as e:
                last_err = e
                if time.monotonic() >= deadline:
                    raise
                # Use an interruptible wait so Ctrl-C shutdown doesn't get stuck sleeping.
                self._closing.wait(timeout=delay)
                delay = min(delay * 2.0, 10.0)
        # (unreachable)
        raise last_err  # type: ignore[misc]

    def run(self, fn: Callable[["psycopg.Connection[Any]"], T], *, retries: int = 1) -> T:
        """
        Run a DB operation and reconnect on transient connection failures.
        """
        # We serialize access. Our current workloads are small, and this avoids
        # thread-safety surprises when used from run_in_executor threads.
        if self._closing.is_set():
            raise RuntimeError("db_closing")

        # Serialize operations so we don't share a connection concurrently across threads.
        with self._lock:
            self.ensure_connected()
            assert self._conn is not None
            conn = self._conn

            try:
                return fn(conn)
            except (psycopg.OperationalError, psycopg.InterfaceError):
                if retries <= 0 or self._closing.is_set():
                    raise
                try:
                    conn.close()
                except Exception:
                    pass
                self._conn = None
                self.ensure_connected()
                assert self._conn is not None
                return fn(self._conn)

