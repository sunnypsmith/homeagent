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
    ) -> None:
        self._host = host
        self._port = int(port)
        self._client = mqtt.Client(client_id=client_id, clean_session=True)
        if username:
            self._client.username_pw_set(username=username, password=password or None)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: "asyncio.Queue[MqttMessage]" = asyncio.Queue()

        def on_message(_client, _userdata, msg) -> None:
            if self._loop is None:
                return
            m = MqttMessage(topic=str(msg.topic), payload=bytes(msg.payload))
            self._loop.call_soon_threadsafe(self._queue.put_nowait, m)

        self._client.on_message = on_message

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        # Connect is blocking; run in thread.
        await asyncio.get_running_loop().run_in_executor(None, self._client.connect, self._host, self._port, 60)
        self._client.loop_start()

    async def close(self) -> None:
        try:
            self._client.loop_stop()
        finally:
            try:
                self._client.disconnect()
            except Exception:
                pass

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self._client.subscribe(topic, qos=qos)

    def publish_json(self, topic: str, payload: Any, qos: int = 0, retain: bool = False) -> None:
        data = json.dumps(payload).encode("utf-8")
        self._client.publish(topic, payload=data, qos=qos, retain=retain)

    async def next_message(self) -> MqttMessage:
        return await self._queue.get()

