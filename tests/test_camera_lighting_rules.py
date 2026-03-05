"""Tests for camera lighting rule parsing."""
from __future__ import annotations

import pytest

from home_agent.services.camera_lighting_agent import (
    _parse_camera_name_list,
    _parse_device_id_list,
    _parse_token_list,
    _expand_detected_obj_tokens,
)


def _parse_rules(rules_raw: str):
    """Replicate the rule parsing logic from run_camera_lighting_agent."""
    rules = []
    for rule_str in rules_raw.split(";"):
        rule_str = rule_str.strip()
        if not rule_str:
            continue
        parts = rule_str.split(":")
        if len(parts) >= 2:
            cams = _parse_camera_name_list(parts[0])
            devs = _parse_device_id_list(parts[1])
            objs = _expand_detected_obj_tokens(_parse_token_list(parts[2])) if len(parts) > 2 else set()
            if cams and devs:
                rules.append({"cameras": cams, "devices": devs, "objects": objs})
    return rules


def test_parse_single_rule() -> None:
    rules = _parse_rules("cam1,cam2:10:person,vehicle")
    assert len(rules) == 1
    assert rules[0]["cameras"] == {"cam1", "cam2"}
    assert rules[0]["devices"] == ["10"]
    assert "person" in rules[0]["objects"]
    assert "vehicle" in rules[0]["objects"]
    assert "car" in rules[0]["objects"]  # expanded from vehicle


def test_parse_multiple_rules() -> None:
    rules = _parse_rules("front:10:vehicle;back:20:person")
    assert len(rules) == 2
    assert rules[0]["cameras"] == {"front"}
    assert rules[0]["devices"] == ["10"]
    assert rules[1]["cameras"] == {"back"}
    assert rules[1]["devices"] == ["20"]


def test_fallback_legacy_config() -> None:
    """When no rules, the agent uses camera_name + detected_obj + caseta_device_id."""
    cams = _parse_camera_name_list("Front_Garage")
    objs = _expand_detected_obj_tokens(_parse_token_list("vehicle"))
    devs = _parse_device_id_list("10")

    assert "front_garage" in cams
    assert "vehicle" in objs
    assert "car" in objs
    assert devs == ["10"]


def test_rule_matching_correct_camera_and_object() -> None:
    rules = _parse_rules("driveway:5:person")
    cam_name = "Driveway"
    evt_obj = "person"

    matched_devices = []
    for rule in rules:
        if rule["cameras"] and cam_name.lower() not in rule["cameras"]:
            continue
        if rule["objects"] and evt_obj not in rule["objects"]:
            continue
        matched_devices.extend(rule["devices"])

    assert matched_devices == ["5"]


def test_rule_matching_wrong_camera() -> None:
    rules = _parse_rules("driveway:5:person")
    cam_name = "Backyard"
    evt_obj = "person"

    matched_devices = []
    for rule in rules:
        if rule["cameras"] and cam_name.lower() not in rule["cameras"]:
            continue
        if rule["objects"] and evt_obj not in rule["objects"]:
            continue
        matched_devices.extend(rule["devices"])

    assert matched_devices == []


def test_rule_matching_wrong_object() -> None:
    rules = _parse_rules("driveway:5:person")
    cam_name = "Driveway"
    evt_obj = "cat"

    matched_devices = []
    for rule in rules:
        if rule["cameras"] and cam_name.lower() not in rule["cameras"]:
            continue
        if rule["objects"] and evt_obj not in rule["objects"]:
            continue
        matched_devices.extend(rule["devices"])

    assert matched_devices == []


def test_expand_vehicle_tokens() -> None:
    tokens = _expand_detected_obj_tokens({"vehicle"})
    assert tokens >= {"vehicle", "car", "truck", "van", "suv"}


def test_expand_person_tokens() -> None:
    tokens = _expand_detected_obj_tokens({"person"})
    assert tokens >= {"person", "people", "human"}


def test_parse_camera_name_list_normalization() -> None:
    result = _parse_camera_name_list("Front_Garage,  Back Yard")
    assert "front_garage" in result
    assert "back yard" in result


def test_parse_device_id_list_deduplication() -> None:
    result = _parse_device_id_list("10,20,10")
    assert result == ["10", "20"]
