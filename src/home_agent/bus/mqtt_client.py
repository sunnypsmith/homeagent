from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Optional

import paho.mqtt.client as mqtt


@dataclass(frozen=True)
class MqttMessage:
    topic: str
    payload: bytes

    def json(self) -> Any:
        return json.loads(self.payload.decode("utf-8"))


class MqttClient:
    """
    Small MQTT helper that plays nicely with asyncio.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: Optional[str],
        password: Optional[str],
        client_id: str,
        queue_maxsize: int = 50_000,
    ) -> None:
        self._host = host
        self._port = int(port)
        self._client_id = str(client_id)
        self._client = mqtt.Client(client_id=client_id, clean_session=True)
        if username:
            self._client.username_pw_set(username=username, password=password or None)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Bounded queue prevents OOM if a publisher goes wild or downstream is slow.
        # Keep this very high by default; it's "insurance" for abnormal situations.
        self._queue: "asyncio.Queue[MqttMessage]" = asyncio.Queue(maxsize=max(1, int(queue_maxsize)))
        self._connected = False
        self._connected_event: Optional[asyncio.Event] = None
        self._subs: dict[str, int] = {}  # topic -> qos
        self._received_total = 0
        self._dropped_total = 0
        self._connect_total = 0
        self._disconnect_total = 0
        self._last_connect_rc: Optional[int] = None
        self._last_disconnect_rc: Optional[int] = None
        self._max_queue_size_seen = 0

        def _enqueue(m: MqttMessage) -> None:
            self._received_total += 1
            try:
                self._queue.put_nowait(m)
                try:
                    qs = int(self._queue.qsize())
                    if qs > self._max_queue_size_seen:
                        self._max_queue_size_seen = qs
                except Exception:
                    pass
            except asyncio.QueueFull:
                # Drop newest. For this project, it's better to stay alive than OOM.
                self._dropped_total += 1

        def on_message(_client, _userdata, msg) -> None:
            if self._loop is None:
                return
            m = MqttMessage(topic=str(msg.topic), payload=bytes(msg.payload))
            self._loop.call_soon_threadsafe(_enqueue, m)

        def on_connect(_client, _userdata, _flags, _rc) -> None:
            # paho runs callbacks on its network thread; bridge state to asyncio loop.
            self._connected = True
            self._connect_total += 1
            try:
                self._last_connect_rc = int(_rc)
            except Exception:
                self._last_connect_rc = None
            if self._loop is None:
                return

            def _mark_connected() -> None:
                if self._connected_event is not None:
                    self._connected_event.set()

            # Re-subscribe to all topics (clean_session=True).
            for topic, qos in list(self._subs.items()):
                try:
                    self._client.subscribe(topic, qos=qos)
                except Exception:
                    # best-effort; service will log/notice via lack of messages
                    pass

            self._loop.call_soon_threadsafe(_mark_connected)

        def on_disconnect(_client, _userdata, _rc) -> None:
            self._connected = False
            self._disconnect_total += 1
            try:
                self._last_disconnect_rc = int(_rc)
            except Exception:
                self._last_disconnect_rc = None

        self._client.on_message = on_message
        self._client.on_connect = on_connect
        self._client.on_disconnect = on_disconnect

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._connected_event = asyncio.Event()
        # Enable auto-reconnect delays (paho will reconnect in the background).
        try:
            self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        except Exception:
            pass
        # Use async connect so loop thread can manage reconnects.
        self._client.connect_async(self._host, self._port, 60)
        self._client.loop_start()
        # Wait for the first connection so callers can subscribe immediately.
        await asyncio.wait_for(self._connected_event.wait(), timeout=15.0)

    async def close(self) -> None:
        # Important: disconnect first so the loop thread can exit quickly.
        try:
            self._client.disconnect()
        except Exception:
            pass

        # Ensure the network loop thread stops promptly on shutdown.
        try:
            try:
                self._client.loop_stop(force=True)
            except TypeError:
                # Older paho versions may not support force=.
                self._client.loop_stop()
        except Exception:
            pass

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self._subs[str(topic)] = int(qos)
        self._client.subscribe(topic, qos=qos)

    def publish_json(self, topic: str, payload: Any, qos: int = 0, retain: bool = False) -> None:
        data = json.dumps(payload).encode("utf-8")
        self._client.publish(topic, payload=data, qos=qos, retain=retain)

    async def next_message(self) -> MqttMessage:
        return await self._queue.get()

    @property
    def is_connected(self) -> bool:
        return bool(self._connected)

    def stats(self) -> dict[str, int]:
        """
        Simple counters (helpful for periodic logging).
        """
        return {
            "connected": 1 if self._connected else 0,
            "queue_size": int(self._queue.qsize()),
            "queue_maxsize": int(self._queue.maxsize),
            "max_queue_size_seen": int(self._max_queue_size_seen),
            "received_total": int(self._received_total),
            "dropped_total": int(self._dropped_total),
            "connect_total": int(self._connect_total),
            "disconnect_total": int(self._disconnect_total),
            "last_connect_rc": int(self._last_connect_rc) if self._last_connect_rc is not None else -1,
            "last_disconnect_rc": int(self._last_disconnect_rc) if self._last_disconnect_rc is not None else -1,
        }
