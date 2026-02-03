#!/usr/bin/env python3
"""
Discover Sonos devices on the local network and optionally write config to .env.

This is intended to be a *standalone* utility people can run during setup.

Quick usage:
  - SSDP discovery (best when multicast works):
      python3 scripts/sonos_discover.py

  - Subnet scan (use when SSDP/multicast is blocked):
      python3 scripts/sonos_discover.py --subnet 192.168.1.0/24

  - Write selection into .env:
      python3 scripts/sonos_discover.py --write
      python3 scripts/sonos_discover.py --subnet 192.168.1.0/24 --write

Requires:
  pip install -e ".[sonos]"
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from typing import Iterable, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import urlopen
from xml.etree import ElementTree


@dataclass(frozen=True)
class SonosDevice:
    ip: str
    name: str
    uid: str
    model: str = ""


def main() -> int:
    epilog = """Examples:
  python3 scripts/sonos_discover.py
  python3 scripts/sonos_discover.py --write
  python3 scripts/sonos_discover.py --subnet 192.168.1.0/24
  python3 scripts/sonos_discover.py --subnet 192.168.1.0/24 --timeout 2 --max-workers 256 --write

Notes:
  - SSDP mode requires multicast/UPnP to work on your network.
  - Subnet mode probes http://<ip>:1400/xml/device_description.xml and does not rely on multicast.
  - This script updates SONOS_SPEAKER_MAP + SONOS_GLOBAL_ANNOUNCE_TARGETS in your env file (it does not print or modify other keys).
"""
    parser = argparse.ArgumentParser(
        description="Discover Sonos devices and (optionally) write SONOS_SPEAKER_MAP to an env file.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--timeout", type=int, default=6, help="Discovery timeout seconds (default: 6)")
    parser.add_argument(
        "--subnet",
        type=str,
        default=None,
        help="Optional CIDR to scan by IP (e.g. 192.168.1.0/24). If set, scans by IP instead of SSDP.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=128,
        help="Concurrency for subnet scanning (default: 128). Ignored for SSDP.",
    )
    parser.add_argument("--env-file", type=str, default=".env", help="Path to env file (default: .env)")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually update the env file (default is dry-run / print instructions only)",
    )
    parser.add_argument(
        "--no-tone",
        action="store_true",
        help="Skip playing a test tone after you select a device",
    )
    parser.add_argument(
        "--tone-seconds",
        type=float,
        default=1.0,
        help="Test tone duration seconds (default: 1.0)",
    )
    parser.add_argument(
        "--tone-frequency",
        type=int,
        default=880,
        help="Test tone frequency in Hz (default: 880)",
    )
    parser.add_argument(
        "--serve-host",
        type=str,
        default=None,
        help=(
            "Host/IP to embed in the test-tone URL (useful in Docker). "
            "Example: --serve-host 10.1.2.10 (your host machine's LAN IP)"
        ),
    )
    parser.add_argument(
        "--serve-port",
        type=int,
        default=0,
        help=(
            "Port to bind the temporary tone HTTP server to. "
            "Use a fixed port when running in Docker with -p. Default: random free port."
        ),
    )
    args = parser.parse_args()

    if args.subnet:
        devices = discover_by_subnet(
            subnet=args.subnet,
            timeout=args.timeout,
            max_workers=args.max_workers,
        )
    else:
        devices = discover_ssdp(timeout=args.timeout)

    if not devices:
        print("No Sonos devices discovered.")
        print("Tips:")
        print("- Make sure you're on the same LAN/VLAN as Sonos")
        if args.subnet:
            print("- Make sure the subnet is correct and reachable")
            print("- Some networks block device description requests")
            print("- Try increasing --timeout, or reducing --max-workers on slow routers")
        else:
            print("- Multicast/SSDP must be allowed (UPnP)")
            print("- If multicast is blocked, try: --subnet 192.168.1.0/24")
        return 2

    print("Discovered Sonos devices:")
    for i, d in enumerate(devices, start=1):
        # name is the Sonos "room"/zone name (usually set in the Sonos app)
        if d.model:
            print("%2d) %-24s  %-14s  %-15s  %s" % (i, d.name, d.model, d.ip, d.uid))
        else:
            print("%2d) %-24s  %-15s  %s" % (i, d.name, d.ip, d.uid))

    chosen_devices = choose_devices(devices)
    if not chosen_devices:
        print("No selection made. Exiting.")
        return 1

    if not args.no_tone:
        if _prompt_yes_no(
            "Play a test tone on %d selected device(s) now?" % len(chosen_devices),
            default_yes=True,
        ):
            failures = 0
            for d in chosen_devices:
                ok = play_test_tone(
                    d,
                    seconds=args.tone_seconds,
                    frequency_hz=args.tone_frequency,
                    serve_host=args.serve_host,
                    serve_port=args.serve_port,
                )
                if not ok:
                    failures += 1
            if failures:
                print("%d/%d test tone(s) failed." % (failures, len(chosen_devices)))

    env_path = Path(args.env_file)
    speaker_map, global_targets = _build_speaker_map(chosen_devices, default_volume=40)
    if args.write:
        update_env_file(
            env_path,
            {
                "SONOS_SPEAKER_MAP": speaker_map,
                "SONOS_GLOBAL_ANNOUNCE_TARGETS": global_targets,
            },
        )
        print("Updated %s:" % env_path)
        print("  SONOS_SPEAKER_MAP=%s" % speaker_map)
        print("  SONOS_GLOBAL_ANNOUNCE_TARGETS=%s" % global_targets)
        print("Next (optional): adjust per-speaker volumes or update SONOS_DEFAULT_VOLUME")
    else:
        print("\nDry-run (no files changed). Add this to %s:" % env_path)
        print("SONOS_SPEAKER_MAP=%s" % speaker_map)
        print("SONOS_GLOBAL_ANNOUNCE_TARGETS=%s" % global_targets)

    return 0


def discover_ssdp(timeout: int) -> List[SonosDevice]:
    try:
        import soco  # type: ignore
    except Exception:
        print("SoCo is not installed.")
        print('Install with: pip install -e ".[sonos]"')
        return []

    found = soco.discover(timeout=timeout)  # returns a set of SoCo instances or None
    if not found:
        return []

    devices: List[SonosDevice] = []
    for s in sorted(found, key=lambda x: (getattr(x, "player_name", "") or "", getattr(x, "ip_address", "") or "")):
        ip = getattr(s, "ip_address", None) or ""
        name = getattr(s, "player_name", None) or "Unknown"
        uid = getattr(s, "uid", None) or "Unknown"
        if ip:
            model = ""
            try:
                info = getattr(s, "speaker_info", lambda: {})()
                model = info.get("model_name") or info.get("modelNumber") or ""
            except Exception:
                model = ""
            devices.append(SonosDevice(ip=ip, name=name, uid=uid, model=model))
    return devices


def discover_by_subnet(subnet: str, timeout: int, max_workers: int) -> List[SonosDevice]:
    """
    Scan a subnet by IP and probe for Sonos device description on port 1400.
    This is useful when SSDP/multicast is blocked.
    """
    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except ValueError as e:
        print("Invalid --subnet: %s" % e)
        return []

    # Avoid ridiculous scans by accident.
    if net.num_addresses > 8192:
        print("Refusing to scan %s addresses (subnet too large)." % net.num_addresses)
        print("Use a smaller CIDR (e.g. /24) or run SSDP discovery without --subnet.")
        return []

    ips: List[str] = [str(ip) for ip in net.hosts()]
    probe = partial(_probe_sonos_device, timeout=timeout)

    # Use threads (urllib is blocking). Keep it simple and dependency-free.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    devices: List[SonosDevice] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(probe, ip) for ip in ips]
        for fut in as_completed(futs):
            dev = fut.result()
            if dev is not None:
                devices.append(dev)

    # Stable ordering
    devices.sort(key=lambda d: (d.name or "", d.ip))
    return devices


def _probe_sonos_device(ip: str, timeout: int) -> Optional[SonosDevice]:
    """
    Sonos speakers expose UPnP device description at:
      http://<ip>:1400/xml/device_description.xml
    """
    url = "http://%s:1400/xml/device_description.xml" % ip
    try:
        with urlopen(url, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            data = resp.read(512 * 1024)  # cap
    except (URLError, OSError):
        return None

    # Parse XML (namespaces vary).
    try:
        root = ElementTree.fromstring(data)
    except Exception:
        return None

    # Device description XML is usually:
    # <root><device><friendlyName>...</friendlyName><modelName>...</modelName><UDN>uuid:...</UDN>...
    # friendlyName is often the user-visible zone name, but we also try /status/zp for ZoneName.
    model = _find_text_any_ns(root, "modelName") or ""
    udn = _find_text_any_ns(root, "UDN") or "Unknown"
    name = _probe_zone_name(ip=ip, timeout=timeout) or _find_text_any_ns(root, "friendlyName") or "Sonos"

    # Basic heuristic: most Sonos devices mention "Sonos" in manufacturer/model.
    manufacturer = _find_text_any_ns(root, "manufacturer") or ""
    if "sonos" not in (manufacturer + " " + model + " " + name).lower():
        return None

    return SonosDevice(ip=ip, name=name, uid=udn, model=model)


def _find_text_any_ns(root: ElementTree.Element, tag: str) -> Optional[str]:
    # Search without caring about namespaces.
    for el in root.iter():
        if el.tag.endswith("}" + tag) or el.tag == tag:
            if el.text:
                return el.text.strip()
    return None


def _probe_zone_name(ip: str, timeout: int) -> Optional[str]:
    """
    Fetch Sonos zone name (room name) via /status/zp (unicast HTTP, no multicast needed).
    """
    url = "http://%s:1400/status/zp" % ip
    try:
        with urlopen(url, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            data = resp.read(256 * 1024)
    except (URLError, OSError):
        return None

    try:
        root = ElementTree.fromstring(data)
    except Exception:
        return None

    # Commonly: <ZPInfo><ZoneName>Kitchen</ZoneName>...</ZPInfo>
    name = _find_text_any_ns(root, "ZoneName")
    if name:
        return name
    # fallback: sometimes Name/RoomName may appear
    return _find_text_any_ns(root, "RoomName") or _find_text_any_ns(root, "Name")


def choose_devices(devices: List[SonosDevice]) -> List[SonosDevice]:
    while True:
        raw = input(
            "\nSelect device number(s) (e.g. 4 or 1,4,7) (or Enter to cancel): "
        ).strip()
        if raw == "":
            return []

        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            return []

        idxs: List[int] = []
        ok = True
        for p in parts:
            try:
                idx = int(p)
            except ValueError:
                ok = False
                break
            idxs.append(idx)
        if not ok:
            print("Please enter a number or a comma-delimited list like 1,4,7.")
            continue

        # Validate and de-dupe while preserving order
        seen = set()
        chosen: List[SonosDevice] = []
        for idx in idxs:
            if not (1 <= idx <= len(devices)):
                ok = False
                break
            if idx in seen:
                continue
            seen.add(idx)
            chosen.append(devices[idx - 1])
        if not ok:
            print("Out of range (1..%d)." % len(devices))
            continue

        return chosen


def _build_speaker_map(devices: List[SonosDevice], *, default_volume: int) -> tuple[str, str]:
    """
    Build SONOS_SPEAKER_MAP and SONOS_GLOBAL_ANNOUNCE_TARGETS strings.
    """
    aliases: List[str] = []
    seen: dict[str, int] = {}
    entries: List[str] = []

    for d in devices:
        alias = _normalize_alias(d.name or d.ip)
        # de-dupe aliases by suffixing _2, _3, ...
        count = seen.get(alias, 0) + 1
        seen[alias] = count
        if count > 1:
            alias = f"{alias}_{count}"
        aliases.append(alias)
        entries.append("%s=%s:%d" % (alias, d.ip, int(default_volume)))

    speaker_map = ",".join(entries)
    global_targets = ",".join(aliases)
    return (speaker_map, global_targets)


def _normalize_alias(name: str) -> str:
    s = (name or "").strip().lower()
    if not s:
        return "speaker"
    # replace non-alnum with underscores and collapse repeats
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return s or "speaker"


_ENV_KV_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


def update_env_file(path: Path, updates: dict) -> None:
    """
    Updates/creates KEY=value lines in an env file without printing or touching other keys.
    """
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines(True)
    else:
        lines = []

    keys = set(updates.keys())
    seen = set()
    out_lines: List[str] = []

    for line in lines:
        m = _ENV_KV_RE.match(line)
        if not m:
            out_lines.append(line)
            continue
        k = m.group(1)
        if k in keys:
            out_lines.append("%s=%s\n" % (k, updates[k]))
            seen.add(k)
        else:
            out_lines.append(line)

    missing = [k for k in updates.keys() if k not in seen]
    if missing:
        if out_lines and not out_lines[-1].endswith("\n"):
            out_lines[-1] = out_lines[-1] + "\n"
        if out_lines and out_lines[-1].strip() != "":
            out_lines.append("\n")
        out_lines.append("# --- Sonos (added by sonos_discover.py) ---\n")
        for k in missing:
            out_lines.append("%s=%s\n" % (k, updates[k]))

    path.write_text("".join(out_lines), encoding="utf-8")


def _prompt_yes_no(prompt: str, default_yes: bool) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    while True:
        raw = input("%s %s " % (prompt, suffix)).strip().lower()
        if raw == "":
            return default_yes
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("Please answer y/n.")


def play_test_tone(
    device: SonosDevice,
    seconds: float,
    frequency_hz: int,
    serve_host: Optional[str],
    serve_port: int,
) -> bool:
    """
    Play a short generated WAV tone on the selected Sonos and restore prior state.
    Requires SoCo.
    """
    try:
        from soco import SoCo  # type: ignore
        from soco.snapshot import Snapshot  # type: ignore
    except Exception:
        print("SoCo is required to play a test tone.")
        print('Install with: pip install -e ".[sonos]"')
        return False

    spk = SoCo(device.ip)
    # If the speaker is grouped, play on the group coordinator so it actually outputs.
    try:
        coordinator = spk.group.coordinator
    except Exception:
        coordinator = spk

    snap = Snapshot(coordinator)

    with TemporaryDirectory() as td:
        tone_path = Path(td) / "tone.wav"
        _write_wav_tone(tone_path, seconds=seconds, frequency_hz=frequency_hz)

        # Serve the tone from a tiny local HTTP server.
        url = _serve_file_and_get_url(
            file_path=tone_path,
            speaker_ip=device.ip,
            serve_host=serve_host,
            serve_port=serve_port,
        )
        if not url:
            return False

        try:
            snap.snapshot()

            # Make it audible; Snapshot will restore.
            try:
                current_vol = int(getattr(coordinator, "volume", 0) or 0)
                if current_vol < 50:
                    coordinator.volume = 50
            except Exception:
                pass

            coordinator.play_uri(url, title="Home Agent test tone", start=True)

            # Wait briefly for playback to actually start, then let the tone play.
            _wait_for_playing(coordinator, timeout_seconds=2.0)
            sleep(max(0.2, seconds + 0.75))
        except Exception as e:
            print("Failed to play tone: %s" % type(e).__name__)
            return False
        finally:
            try:
                snap.restore()
            except Exception:
                # Best-effort restore.
                try:
                    spk.stop()
                except Exception:
                    pass

    print("Played test tone on '%s' (%s)." % (device.name, device.ip))
    return True


def _wait_for_playing(soco_device, timeout_seconds: float) -> None:
    """
    Best-effort: wait until Sonos reports PLAYING (or timeout).
    """
    deadline = float(timeout_seconds)
    step = 0.1
    waited = 0.0
    while waited < deadline:
        try:
            info = soco_device.get_current_transport_info()
            state = (info or {}).get("current_transport_state") or ""
            if str(state).upper() == "PLAYING":
                return
        except Exception:
            return
        sleep(step)
        waited += step


def _serve_file_and_get_url(
    file_path: Path,
    speaker_ip: str,
    serve_host: Optional[str],
    serve_port: int,
) -> Optional[str]:
    """
    Serve a single file over HTTP so Sonos can fetch it.
    Returns a URL like http://<local_ip>:<port>/<filename>
    """
    import socket
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    def infer_local_ip() -> str:
        # Determine the local IP used to reach the speaker.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((speaker_ip, 1400))
            return s.getsockname()[0]
        finally:
            try:
                s.close()
            except Exception:
                pass

    local_ip = serve_host or infer_local_ip()

    # In Docker bridge mode, infer_local_ip is often a container IP (e.g. 172.17.x.x)
    # that your Sonos cannot reach. Provide a clear warning and instructions.
    try:
        ip_obj = ipaddress.ip_address(local_ip)
        if serve_host is None and (
            ip_obj.is_loopback or str(ip_obj).startswith("172.17.") or str(ip_obj).startswith("172.18.")
        ):
            print("\nTone server would use %s (likely not reachable from Sonos in Docker bridge mode)." % local_ip)
            print("Fix options:")
            print("- Run container with host networking: docker run --network host ...")
            print("- Or publish a fixed port and pass your host LAN IP:")
            print("    docker run -p 18000:18000 ...")
            print("    python3 scripts/sonos_discover.py ... --serve-host <HOST_LAN_IP> --serve-port 18000")
            return None
    except Exception:
        pass

    handler = partial(SimpleHTTPRequestHandler, directory=str(file_path.parent))
    httpd = ThreadingHTTPServer(("0.0.0.0", int(serve_port or 0)), handler)
    port = httpd.server_address[1]

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    # Sonos will fetch quickly; we shut down after playback via process exit.
    return "http://%s:%d/%s" % (local_ip, port, file_path.name)


def _write_wav_tone(path: Path, seconds: float, frequency_hz: int) -> None:
    import math
    import struct
    import wave

    sample_rate = 44100
    n_samples = int(sample_rate * max(0.05, seconds))
    amplitude = 0.85  # 0..1 (make it clearly audible)

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)

        frames = bytearray()
        for i in range(n_samples):
            t = float(i) / sample_rate
            # Tiny fade in/out to avoid clicks.
            fade = min(1.0, i / (sample_rate * 0.02), (n_samples - 1 - i) / (sample_rate * 0.02))
            v = float(fade) * amplitude * math.sin(2.0 * math.pi * float(frequency_hz) * t)
            frames += struct.pack("<h", int(v * 32767.0))

        wf.writeframes(frames)


if __name__ == "__main__":
    sys.exit(main())

