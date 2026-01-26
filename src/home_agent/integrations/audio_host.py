from __future__ import annotations

import contextlib
import os
import time
import socket
import threading
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional
from uuid import uuid4


@dataclass(frozen=True)
class HostedAudio:
    url: str
    content_type: str


class AudioHost:
    """
    Very small local HTTP host for short-lived audio clips.

    Designed for Sonos: we need an HTTP URL the speaker can fetch.
    """

    def __init__(
        self,
        *,
        public_host: Optional[str] = None,
        bind_host: str = "0.0.0.0",
        bind_port: int = 0,
        ttl_seconds: float = 180.0,
        cleanup_interval_seconds: float = 30.0,
    ) -> None:
        self._public_host = public_host
        self._bind_host = bind_host
        self._bind_port = bind_port
        self._ttl_seconds = float(ttl_seconds)
        self._cleanup_interval_seconds = float(cleanup_interval_seconds)

        self._lock = threading.Lock()
        self._td: Optional[TemporaryDirectory] = None
        self._root: Optional[Path] = None
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._cleanup_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._base_url: Optional[str] = None
        self._expires_at: dict[str, float] = {}

    def host_bytes(self, *, data: bytes, filename: str, content_type: str, route_to_ip: str) -> HostedAudio:
        # Start the shared server lazily (first request decides the route IP used to infer a host).
        self._ensure_started(route_to_ip=route_to_ip)

        # Write a unique file name so parallel announcements don't collide.
        safe_name = _safe_filename(filename)
        unique = f"{uuid4().hex}_{safe_name}"

        assert self._root is not None
        path = self._root / unique
        path.write_bytes(data)

        # Track TTL so we can delete old clips.
        with self._lock:
            self._expires_at[unique] = time.time() + max(5.0, self._ttl_seconds)

        assert self._base_url is not None
        url = f"{self._base_url}/{unique}"
        return HostedAudio(url=url, content_type=content_type)

    def stats(self) -> dict[str, object]:
        """
        Lightweight status for heartbeats/logging.
        """
        with self._lock:
            return {
                "started": bool(self._httpd is not None and self._base_url is not None),
                "base_url": self._base_url,
                "active_files": int(len(self._expires_at)),
                "ttl_seconds": float(self._ttl_seconds),
            }

    def close(self) -> None:
        """
        Best-effort shutdown (mostly used in short-lived CLI commands).
        Long-running services typically keep the host alive for process lifetime.
        """
        with self._lock:
            self._stop.set()
            httpd = self._httpd
        if httpd is not None:
            with contextlib.suppress(Exception):
                httpd.shutdown()
            with contextlib.suppress(Exception):
                httpd.server_close()
        td = self._td
        if td is not None:
            with contextlib.suppress(Exception):
                td.cleanup()

    def _ensure_started(self, *, route_to_ip: str) -> None:
        with self._lock:
            if self._httpd is not None and self._base_url is not None and self._root is not None:
                return
            if self._stop.is_set():
                # If someone closed us, allow re-start by resetting the stop flag.
                self._stop.clear()

            self._td = TemporaryDirectory()
            self._root = Path(self._td.name)

            # Determine the local IP Sonos will reach (or use explicit public host).
            host = self._public_host or _infer_local_ip(route_to_ip)

            handler = partial(_QuietSimpleHTTPRequestHandler, directory=str(self._root))
            self._httpd = ThreadingHTTPServer((self._bind_host, int(self._bind_port or 0)), handler)
            port = int(self._httpd.server_address[1])
            self._base_url = f"http://{host}:{port}"

            self._server_thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
            self._server_thread.start()

            self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
            self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        """
        Periodically delete expired files from the shared directory.
        """
        while not self._stop.is_set():
            time.sleep(max(1.0, float(self._cleanup_interval_seconds)))
            now = time.time()

            root: Optional[Path]
            with self._lock:
                root = self._root
                expired = [name for name, ts in self._expires_at.items() if ts <= now]
                for name in expired:
                    self._expires_at.pop(name, None)

            if not root or not expired:
                continue

            for name in expired:
                p = root / name
                with contextlib.suppress(Exception):
                    p.unlink(missing_ok=True)  # py3.8+ supports missing_ok
            # Best-effort: also prune orphan files older than TTL (in case of crash between writes + map insert)
            try:
                ttl = max(5.0, float(self._ttl_seconds))
                cutoff = now - ttl
                for child in root.iterdir():
                    if not child.is_file():
                        continue
                    try:
                        st = child.stat()
                        if st.st_mtime <= cutoff:
                            child.unlink(missing_ok=True)
                    except Exception:
                        continue
            except Exception:
                continue


def _infer_local_ip(route_to_ip: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((route_to_ip, 1400))
        return s.getsockname()[0]
    finally:
        try:
            s.close()
        except Exception:
            pass


def _safe_filename(name: str) -> str:
    """
    Prevent path traversal and weird filenames; keep only a basename.
    """
    s = str(name or "audio").strip()
    s = os.path.basename(s)
    if not s:
        return "audio"
    # Avoid spaces in URLs; keep it simple.
    s = s.replace(" ", "_")
    return s


class _QuietSimpleHTTPRequestHandler(SimpleHTTPRequestHandler):
    """
    Suppress noisy tracebacks when Sonos closes connections early (common).
    """

    # Make handler quieter by default.
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def log_error(self, format: str, *args) -> None:  # noqa: A002
        return

    def copyfile(self, source, outputfile) -> None:
        try:
            super().copyfile(source, outputfile)
        except (BrokenPipeError, ConnectionResetError):
            # Client went away mid-stream; ignore.
            return

