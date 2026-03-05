"""Tests for the offline audio registry."""
from __future__ import annotations

import pytest

from home_agent.offline_audio import OFFLINE_AUDIO_ITEMS, OfflineAudioItem


EXPECTED_KEYS = {
    "voice_ack",
    "voice_ack_2",
    "voice_ack_3",
    "voice_reasoning",
    "voice_cancelled",
    "voice_error",
    "internet_down",
    "internet_high_latency",
    "internet_packet_loss",
}


def test_all_expected_keys_exist() -> None:
    actual_keys = {item["key"] for item in OFFLINE_AUDIO_ITEMS}
    for expected in EXPECTED_KEYS:
        assert expected in actual_keys, f"Missing key: {expected}"


def test_each_item_has_required_fields() -> None:
    for item in OFFLINE_AUDIO_ITEMS:
        assert "key" in item and item["key"], f"Item missing key: {item}"
        assert "filename" in item and item["filename"], f"Item missing filename: {item}"
        assert "text" in item and item["text"], f"Item missing text: {item}"


def test_no_duplicate_keys() -> None:
    keys = [item["key"] for item in OFFLINE_AUDIO_ITEMS]
    assert len(keys) == len(set(keys)), f"Duplicate keys found: {keys}"


def test_items_list_not_empty() -> None:
    assert len(OFFLINE_AUDIO_ITEMS) > 0


def test_filenames_are_wav() -> None:
    for item in OFFLINE_AUDIO_ITEMS:
        assert item["filename"].endswith(".wav"), f"Non-wav filename: {item['filename']}"
