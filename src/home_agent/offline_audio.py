from __future__ import annotations

from typing import List, TypedDict


class OfflineAudioItem(TypedDict):
    key: str
    filename: str
    text: str


OFFLINE_AUDIO_ITEMS: List[OfflineAudioItem] = [
    {
        "key": "internet_down",
        "filename": "internet_down.wav",
        "text": (
            "Your attention please. The internet egress is down. "
            "Repeating. The internet egress is down."
        ),
    },
    {
        "key": "internet_high_latency",
        "filename": "internet_high_latency.wav",
        "text": (
            "Your attention please. The internet egress has high latency. "
            "Repeating. The internet egress has high latency."
        ),
    },
    {
        "key": "internet_packet_loss",
        "filename": "internet_packet_loss.wav",
        "text": (
            "Your attention please. The internet egress has significant packet loss. "
            "Repeating. The internet egress has significant packet loss."
        ),
    },
]
