from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional


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
    ) -> None:
        self._public_host = public_host
        self._bind_host = bind_host
        self._bind_port = bind_port

    def host_bytes(self, *, data: bytes, filename: str, content_type: str, route_to_ip: str) -> HostedAudio:
        td = TemporaryDirectory()
        root = Path(td.name)
        path = root / filename
        path.write_bytes(data)

        handler = partial(SimpleHTTPRequestHandler, directory=str(root))
        httpd = ThreadingHTTPServer((self._bind_host, int(self._bind_port or 0)), handler)
        port = httpd.server_address[1]

        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        host = self._public_host or _infer_local_ip(route_to_ip)
        url = "http://%s:%d/%s" % (host, port, filename)

        # We intentionally do not shut down immediately; caller should keep the
        # process alive while playback occurs. TemporaryDirectory will be cleaned
        # when the process exits; for longer-lived hosting we’ll implement a shared
        # server + cache later.
        _leak_keeper(httpd, td)
        return HostedAudio(url=url, content_type=content_type)


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


def _leak_keeper(httpd: ThreadingHTTPServer, td: TemporaryDirectory) -> None:
    """
    Keep references alive (simple v1 lifecycle).
    """
    if not hasattr(_leak_keeper, "_refs"):
        _leak_keeper._refs = []  # type: ignore[attr-defined]
    _leak_keeper._refs.append((httpd, td))  # type: ignore[attr-defined]

