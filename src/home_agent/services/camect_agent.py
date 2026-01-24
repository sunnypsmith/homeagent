from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from home_agent.bus.envelope import make_event
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger


def _iter_strings(obj: Any, *, _depth: int = 0, _max_depth: int = 6) -> Iterable[str]:
    """
    Yield lowercased string values from nested dict/list structures.
    """
    if _depth > _max_depth:
        return
    if obj is None:
        return
    if isinstance(obj, str):
        s = obj.strip().lower()
        if s:
            yield s
        return
    if isinstance(obj, (int, float, bool)):
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            # keys can be informative too (e.g. "vehicle")
            if isinstance(k, str):
                ks = k.strip().lower()
                if ks:
                    yield ks
            yield from _iter_strings(v, _depth=_depth + 1, _max_depth=_max_depth)
        return
    if isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v, _depth=_depth + 1, _max_depth=_max_depth)
        return


def _find_first_key_in_tree(
    obj: Any, keys: Tuple[str, ...], *, _depth: int = 0, _max_depth: int = 6
) -> Optional[Any]:
    """
    Find the first matching key (case-insensitive) anywhere in a nested dict/list structure.
    """
    if _depth > _max_depth or obj is None:
        return None
    if isinstance(obj, dict):
        lower_map = {str(k).lower(): k for k in obj.keys()}
        for want in keys:
            k = lower_map.get(want.lower())
            if k is not None:
                return obj.get(k)
        for v in obj.values():
            found = _find_first_key_in_tree(v, keys, _depth=_depth + 1, _max_depth=_max_depth)
            if found is not None:
                return found
        return None
    if isinstance(obj, list):
        for v in obj:
            found = _find_first_key_in_tree(v, keys, _depth=_depth + 1, _max_depth=_max_depth)
            if found is not None:
                return found
        return None
    return None


def _first_in_event(evt: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[str]:
    """
    Find the first matching key in evt (case-insensitive) and return its string value.
    """
    lower_map = {str(k).lower(): k for k in evt.keys()}
    for want in keys:
        k = lower_map.get(want.lower())
        if k is None:
            continue
        v = evt.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _matches_filter(evt: Dict[str, Any], token: str) -> bool:
    """
    Best-effort matching: look for token(s) in any string fields.
    """
    t = (token or "").strip().lower()
    if not t:
        return True

    # Prefer Camect's explicit object label when present.
    det = evt.get("detected_obj")
    if isinstance(det, str) and det.strip():
        d = det.strip().lower()
        if t == "vehicle":
            return d in ("vehicle", "car", "truck", "van", "suv")
        if t in ("person", "people", "human"):
            return d in ("person", "people", "human")
        return d == t

    hay = " ".join(_iter_strings(evt))
    if not hay:
        return False

    # v1: common synonyms for vehicle-like alerts.
    if t == "vehicle":
        for w in ("vehicle", "car", "truck", "van", "suv"):
            if w in hay:
                return True
        return False

    # v1: common synonyms for person-like alerts.
    if t in ("person", "people", "human"):
        for w in ("person", "people", "human", "man", "woman"):
            if w in hay:
                return True
        return False

    # Otherwise: simple substring.
    return t in hay


def _spoken_kind(token: str) -> str:
    t = (token or "").strip().lower()
    if t == "vehicle":
        return "Vehicle"
    if t in ("person", "people", "human"):
        return "Person"
    return t.capitalize() if t else "Event"


@dataclass(frozen=True)
class CameraMap:
    id_to_name: Dict[str, str]
    name_to_id: Dict[str, str]


def _build_camera_map(cameras: List[Dict[str, Any]]) -> CameraMap:
    id_to_name: Dict[str, str] = {}
    name_to_id: Dict[str, str] = {}
    for c in cameras or []:
        cid = str(c.get("id") or c.get("cam_id") or c.get("CamId") or "").strip()
        name = str(c.get("name") or c.get("Name") or "").strip()
        if cid and name:
            id_to_name[cid] = name
            name_to_id[name] = cid
    return CameraMap(id_to_name=id_to_name, name_to_id=name_to_id)


async def run_camect_agent() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="camect_agent")

    if not settings.camect.enabled:
        log.warning("camect_disabled", hint="Set CAMECT_ENABLED=true to run this service")
        return

    if not settings.camect.host:
        log.error("missing_config", key="CAMECT_HOST")
        return
    if not settings.camect.username:
        log.error("missing_config", key="CAMECT_USERNAME")
        return
    if not settings.camect.password:
        log.error("missing_config", key="CAMECT_PASSWORD")
        return

    rules_map = dict(settings.camect.camera_rules_map or {})
    wanted_names = set(rules_map.keys()) if rules_map else set(settings.camect.camera_name_list)
    if not wanted_names:
        log.error("missing_config", key="CAMECT_CAMERA_RULES", hint="or set CAMECT_CAMERA_NAMES")
        return

    try:
        import camect  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("camect-py not installed. Run: pip install -e '.[camect]'") from e

    # Surface camect's own connection/reconnect logs.
    try:
        camect.set_log_level(logging.INFO)
        logging.getLogger("camect").setLevel(logging.INFO)
    except Exception:
        pass

    # Connect hub (sync) and start websocket listener thread internally.
    hub = camect.Hub(settings.camect.host, settings.camect.username, settings.camect.password)
    hub_name = ""
    try:
        hub_name = str(hub.get_name() or "")
    except Exception:
        pass

    cameras = []
    try:
        cameras = list(hub.list_cameras() or [])
    except Exception:
        log.exception("list_cameras_failed")
    cmap = _build_camera_map(cameras)

    # Filter set as IDs too, when possible.
    wanted_ids: Set[str] = set()
    for nm in wanted_names:
        cid = cmap.name_to_id.get(nm)
        if cid:
            wanted_ids.add(cid)

    log.info(
        "camect_connected",
        hub=hub_name or settings.camect.host,
        cameras=len(cameras),
        filter=settings.camect.event_filter,
        rules=len(rules_map),
        throttle_seconds=settings.camect.throttle_seconds,
    )

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-camect-agent",
    )
    await mqttc.connect()

    event_topic = "%s/camera/event" % settings.mqtt.base_topic
    announce_topic = "%s/announce/request" % settings.mqtt.base_topic

    loop = asyncio.get_running_loop()
    q: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=1000)

    received_total = 0
    dropped_total = 0
    matched_total = 0
    announced_total = 0
    last_event_at = 0.0  # monotonic seconds

    def _enqueue(evt: Dict[str, Any]) -> None:
        nonlocal received_total, dropped_total
        received_total += 1
        try:
            q.put_nowait(dict(evt or {}))
        except Exception:
            dropped_total += 1

    def _on_evt(evt: Dict[str, Any]) -> None:
        # This callback is invoked on camect's internal thread; bridge into our asyncio loop.
        try:
            loop.call_soon_threadsafe(_enqueue, evt)
        except Exception:
            # Loop is shutting down; drop.
            pass

    hub.add_event_listener(_on_evt)

    last_announce_by_cam: Dict[str, float] = {}
    throttle = max(0, int(settings.camect.throttle_seconds))

    async def status_loop() -> None:
        nonlocal last_event_at
        interval = max(10, int(settings.camect.status_interval_seconds))
        stale_warn = max(30, int(settings.camect.stale_warning_seconds))
        while True:
            await asyncio.sleep(float(interval))
            age = None
            if last_event_at > 0:
                age = time.monotonic() - last_event_at
            log.info(
                "camect_status",
                received_total=received_total,
                dropped_total=dropped_total,
                matched_total=matched_total,
                announced_total=announced_total,
                queue_size=q.qsize(),
                last_event_age_seconds=round(age, 1) if age is not None else None,
            )
            if age is not None and age >= float(stale_warn):
                log.warning("camect_stale", last_event_age_seconds=round(age, 1))

    try:
        status_task = asyncio.create_task(status_loop())
        while True:
            evt = await q.get()
            last_event_at = time.monotonic()

            cam_id = _first_in_event(evt, ("cam_id", "camid", "CamId", "camera_id", "cameraid")) or ""
            cam_name = _first_in_event(evt, ("cam_name", "camera_name", "name")) or ""
            if not cam_id:
                v = _find_first_key_in_tree(evt, ("cam_id", "camid", "CamId", "camera_id", "cameraid"))
                cam_id = str(v).strip() if isinstance(v, (str, int)) and str(v).strip() else cam_id
            if not cam_name:
                v = _find_first_key_in_tree(evt, ("cam_name", "camera_name", "name"))
                cam_name = str(v).strip() if isinstance(v, str) and v.strip() else cam_name

            if settings.camect.debug:
                det = evt.get("detected_obj")
                desc = evt.get("desc")
                log.debug(
                    "camect_event",
                    camera=cam_name or None,
                    detected_obj=det if isinstance(det, str) else None,
                    type=evt.get("type") if isinstance(evt.get("type"), str) else None,
                    desc=desc if isinstance(desc, str) else None,
                )

            # Prefer mapping for canonical names.
            if cam_id and not cam_name:
                cam_name = cmap.id_to_name.get(cam_id, "")
            if cam_name and not cam_id:
                cam_id = cmap.name_to_id.get(cam_name, "")

            # If rules are configured, we must be able to attribute the event to a camera in rules.
            if rules_map:
                if cam_name:
                    if cam_name not in rules_map:
                        if settings.camect.debug:
                            log.debug("ignored_event", reason="camera_not_in_rules", camera=cam_name)
                        continue
                else:
                    # Can't attribute to a camera name; ignore.
                    if settings.camect.debug:
                        log.debug("ignored_event", reason="no_camera_name_in_event")
                    continue
            else:
                if cam_name and cam_name not in wanted_names:
                    continue
                if cam_id and wanted_ids and cam_id not in wanted_ids:
                    continue
                if (not cam_name) and (not cam_id):
                    # Can't attribute; ignore to avoid cross-camera noise.
                    if settings.camect.debug:
                        log.debug("ignored_event", reason="no_camera_in_event")
                    continue

            token = rules_map.get(cam_name) if cam_name and rules_map else settings.camect.event_filter
            if not _matches_filter(evt, token):
                if settings.camect.debug:
                    log.debug("ignored_event", reason="filter_no_match", camera=cam_name, token=token)
                continue
            matched_total += 1

            # Record every matched event.
            camera_event = make_event(
                source="camect-agent",
                typ="camera.event",
                data={
                    "provider": "camect",
                    "hub": hub_name or settings.camect.host,
                    "camera_id": cam_id or None,
                    "camera_name": cam_name or None,
                    "filter": token,
                    "event": evt,
                },
            )
            mqttc.publish_json(event_topic, camera_event)

            # Announce with throttle (per camera name/id).
            throttle_key = cam_name or cam_id or "unknown"
            now = time.monotonic()
            last = last_announce_by_cam.get(throttle_key, 0.0)
            if throttle and (now - last) < float(throttle):
                continue
            last_announce_by_cam[throttle_key] = now

            spoken_camera = cam_name or cam_id or "camera"
            kind = _spoken_kind(token)
            try:
                text = (settings.camect.announce_template or "").format(camera=spoken_camera, kind=kind)
            except Exception:
                text = "%s detected at %s." % (kind, spoken_camera)

            announce = make_event(
                source="camect-agent",
                typ="announce.request",
                data={"text": text},
            )
            mqttc.publish_json(announce_topic, announce)
            log.info("announce_published", camera=spoken_camera)
            announced_total += 1
    finally:
        try:
            status_task.cancel()
        except Exception:
            pass
        try:
            hub.del_event_listener(_on_evt)
        except Exception:
            pass
        await mqttc.close()


def main() -> int:
    asyncio.run(run_camect_agent())
    return 0

