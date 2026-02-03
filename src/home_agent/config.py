from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _strip_quotes(s: str) -> str:
    s = (s or "").strip()
    if len(s) >= 2 and ((s[0] == s[-1]) and s[0] in ("'", '"')):
        s = s[1:-1].strip()
    return s


def _env_files() -> tuple[str, str]:
    """
    Allow running CLI commands from subdirectories (e.g. /workspace/scripts).
    We first check for a local .env, then fall back to the repo-root .env.
    """
    repo_root_env = str(Path(__file__).resolve().parents[2] / ".env")
    return (".env", repo_root_env)


class SonosSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Comma-delimited list of speaker IPs for announcements (v1: treat each target individually).
    # Keep this as a string because pydantic-settings tries to JSON-decode List[str] from .env.
    announce_targets: str = Field(default="", alias="SONOS_ANNOUNCE_TARGETS")
    # Optional alias map: "office=10.1.2.58:60,kitchen=10.1.2.242:40"
    speaker_map: str = Field(default="", alias="SONOS_SPEAKER_MAP")
    # Optional default targets (aliases or IPs). If set, overrides SONOS_ANNOUNCE_TARGETS.
    global_announce_targets: str = Field(default="", alias="SONOS_GLOBAL_ANNOUNCE_TARGETS")
    # Optional per-agent targets (aliases or IPs).
    morning_briefing_targets: str = Field(default="", alias="SONOS_MORNING_BRIEFING_TARGETS")
    wakeup_targets: str = Field(default="", alias="SONOS_WAKEUP_TARGETS")
    hourly_chime_targets: str = Field(default="", alias="SONOS_HOURLY_CHIME_TARGETS")
    fixed_announcement_targets: str = Field(default="", alias="SONOS_FIXED_ANNOUNCEMENT_TARGETS")
    default_volume: int = Field(default=50, alias="SONOS_DEFAULT_VOLUME")
    announce_concurrency: int = Field(default=3, alias="SONOS_ANNOUNCE_CONCURRENCY")
    tail_padding_seconds: float = Field(default=3.0, alias="SONOS_TAIL_PADDING_SECONDS")
    # Optional per-speaker volume overrides.
    # Format: "10.1.2.58:35,10.1.2.72:45" (comma/semicolon delimited)
    speaker_volumes: str = Field(default="", alias="SONOS_SPEAKER_VOLUMES")

    @field_validator(
        "announce_targets",
        "speaker_map",
        "global_announce_targets",
        "morning_briefing_targets",
        "wakeup_targets",
        "hourly_chime_targets",
        "fixed_announcement_targets",
        mode="before",
    )
    @classmethod
    def _normalize_announce_targets(cls, v: object) -> str:
        if v is None:
            return ""
        return _strip_quotes(str(v))

    @property
    def announce_target_ips(self) -> List[str]:
        """
        Comma-delimited list of speaker IPs for announcements.

        For convenience, each entry may optionally include a volume override:
          "10.1.2.58:35,10.1.2.72:45"

        In that case, the IP portion is used as the target, and the volume is treated
        as a per-speaker override (see `speaker_volume_map`).
        """
        if self.global_announce_targets:
            return self.resolve_targets(self.global_announce_targets)
        ips, _ = _parse_sonos_targets(self.announce_targets or "")
        return ips

    @property
    def speaker_volume_map(self) -> Dict[str, int]:
        """
        Per-speaker volume overrides keyed by IP.
        Example: SONOS_SPEAKER_VOLUMES="10.1.2.58:35,10.1.2.72:45"
        """
        # Start with any embedded ip:vol in SONOS_ANNOUNCE_TARGETS (convenience).
        _, embedded = _parse_sonos_targets(self.announce_targets or "")

        # Add any embedded volumes from the alias map.
        _, alias_vols = _parse_sonos_speaker_map(self.speaker_map or "")

        # Then apply explicit overrides from SONOS_SPEAKER_VOLUMES (recommended).
        explicit = _parse_sonos_speaker_volumes(self.speaker_volumes or "")

        out: Dict[str, int] = dict(embedded)
        out.update(alias_vols)
        out.update(explicit)
        return out

    @property
    def speaker_alias_map(self) -> Dict[str, str]:
        aliases, _ = _parse_sonos_speaker_map(self.speaker_map or "")
        return aliases

    def resolve_targets(self, raw: object) -> List[str]:
        """
        Resolve aliases to IPs for targets. Accepts a comma-delimited string or list of strings.
        """
        if raw is None:
            return []
        items: List[str] = []
        if isinstance(raw, list):
            for v in raw:
                if isinstance(v, str) and v.strip():
                    items.append(v.strip())
        else:
            s = _strip_quotes(str(raw)).strip()
            if s:
                items.extend([p.strip() for p in s.split(",") if p.strip()])

        if not items:
            return []

        aliases = self.speaker_alias_map
        out: List[str] = []
        seen: set[str] = set()
        for item in items:
            ip = aliases.get(item, item)
            ip = _strip_volume_suffix(ip)
            if ip and ip not in seen:
                seen.add(ip)
                out.append(ip)
        return out


def _parse_sonos_targets(raw_targets: str) -> tuple[List[str], Dict[str, int]]:
    """
    Parse SONOS_ANNOUNCE_TARGETS.

    Supported formats:
      - "10.1.2.58,10.1.2.72"
      - "10.1.2.58:35,10.1.2.72:45"  (ip + per-speaker volume)
    """
    s = _strip_quotes(str(raw_targets or "")).strip()
    if not s:
        return ([], {})

    ips: List[str] = []
    vols: Dict[str, int] = {}
    for part in s.split(","):
        item = part.strip()
        if not item:
            continue

        ip = item
        vol: Optional[int] = None

        if ":" in item:
            left, right = item.rsplit(":", 1)
            left = left.strip()
            right = right.strip()
            if left and right:
                try:
                    vv = int(float(right))
                    vv = max(0, min(100, vv))
                    ip = left
                    vol = vv
                except Exception:
                    # Treat as plain IP string if volume isn't parseable.
                    ip = item
                    vol = None

        if ip:
            ips.append(ip)
            if vol is not None:
                vols[ip] = vol

    return (ips, vols)


def _parse_sonos_speaker_volumes(raw: str) -> Dict[str, int]:
    """
    Parse SONOS_SPEAKER_VOLUMES.
    Format: "10.1.2.58:35,10.1.2.72:45" (comma/semicolon delimited)
    """
    s = _strip_quotes(str(raw or "")).strip()
    if not s:
        return {}

    out: Dict[str, int] = {}
    # allow comma or semicolon separators
    for chunk in s.replace(";", ",").split(","):
        item = chunk.strip()
        if not item:
            continue
        if ":" not in item:
            continue
        ip, vol_s = item.split(":", 1)
        ip = ip.strip()
        vol_s = vol_s.strip()
        if not ip or not vol_s:
            continue
        try:
            vol = int(float(vol_s))
        except Exception:
            continue
        vol = max(0, min(100, vol))
        out[ip] = vol
    return out


def _parse_sonos_speaker_map(raw: str) -> tuple[Dict[str, str], Dict[str, int]]:
    """
    Parse SONOS_SPEAKER_MAP.
    Format: "office=10.1.2.58:60,kitchen=10.1.2.242:40"
    Returns (alias->ip, ip->volume).
    """
    s = _strip_quotes(str(raw or "")).strip()
    if not s:
        return ({}, {})

    aliases: Dict[str, str] = {}
    vols: Dict[str, int] = {}
    for chunk in s.replace(";", ",").split(","):
        item = chunk.strip()
        if not item or "=" not in item:
            continue
        alias, target = item.split("=", 1)
        alias = alias.strip()
        target = target.strip()
        if not alias or not target:
            continue

        ip = target
        vol: Optional[int] = None
        if ":" in target:
            left, right = target.rsplit(":", 1)
            left = left.strip()
            right = right.strip()
            if left and right:
                try:
                    vv = int(float(right))
                    vv = max(0, min(100, vv))
                    ip = left
                    vol = vv
                except Exception:
                    ip = target
                    vol = None
        if ip:
            aliases[alias] = ip
            if vol is not None:
                vols[ip] = vol

    return (aliases, vols)


def _strip_volume_suffix(value: str) -> str:
    """
    If value looks like "ip:vol", return ip. Otherwise return original string.
    """
    s = (value or "").strip()
    if ":" in s:
        left, right = s.rsplit(":", 1)
        if left and right and right.isdigit():
            return left.strip()
    return s


class ElevenLabsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: Optional[str] = Field(default=None, alias="ELEVENLABS_API_KEY")
    voice_id: str = Field(default="Wq15xSaY3gWvazBRaGEU", alias="ELEVENLABS_VOICE_ID")
    base_url: str = Field(default="https://api.elevenlabs.io/v1", alias="ELEVENLABS_BASE_URL")
    timeout_seconds: float = Field(default=30, alias="ELEVENLABS_TIMEOUT_SECONDS")

    @field_validator("api_key", mode="before")
    @classmethod
    def _normalize_api_key(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = _strip_quotes(str(v))
        if not s:
            return None
        return s or None

    @field_validator("voice_id", mode="before")
    @classmethod
    def _normalize_voice_id(cls, v: object) -> str:
        return _strip_quotes(str(v))

    @field_validator("base_url", mode="before")
    @classmethod
    def _normalize_base_url(cls, v: object) -> str:
        return _strip_quotes(str(v)).rstrip("/")


class MqttSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(default="127.0.0.1", alias="MQTT_HOST")
    port: int = Field(default=1883, alias="MQTT_PORT")
    username: Optional[str] = Field(default=None, alias="MQTT_USERNAME")
    password: Optional[str] = Field(default=None, alias="MQTT_PASSWORD")
    base_topic: str = Field(default="homeagent", alias="MQTT_BASE_TOPIC")

    @field_validator("host", "base_topic", mode="before")
    @classmethod
    def _norm_str(cls, v: object) -> str:
        return _strip_quotes(str(v))

    @field_validator("username", "password", mode="before")
    @classmethod
    def _norm_opt(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = _strip_quotes(str(v))
        return s or None


class DbSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(default="127.0.0.1", alias="DB_HOST")
    port: int = Field(default=5432, alias="DB_PORT")
    name: str = Field(default="homeagent", alias="DB_NAME")
    user: str = Field(default="homeagent", alias="DB_USER")
    password: str = Field(default="change_me", alias="DB_PASSWORD")
    sslmode: str = Field(default="disable", alias="DB_SSLMODE")

    @field_validator("host", "name", "user", "password", "sslmode", mode="before")
    @classmethod
    def _norm(cls, v: object) -> str:
        return _strip_quotes(str(v))

    @property
    def conninfo(self) -> str:
        # psycopg3 connection string
        return (
            "host=%s port=%d dbname=%s user=%s password=%s sslmode=%s"
            % (self.host, self.port, self.name, self.user, self.password, self.sslmode)
        )


class WeatherSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    provider: str = Field(default="open_meteo", alias="WEATHER_PROVIDER")
    latitude: Optional[float] = Field(default=None, alias="WEATHER_LAT")
    longitude: Optional[float] = Field(default=None, alias="WEATHER_LON")
    units: str = Field(default="imperial", alias="WEATHER_UNITS")  # imperial|metric
    timeout_seconds: float = Field(default=10, alias="WEATHER_TIMEOUT_SECONDS")

    @field_validator("provider", "units", mode="before")
    @classmethod
    def _norm_str(cls, v: object) -> str:
        return _strip_quotes(str(v)).strip().lower()


class GCalSettings(BaseSettings):
    """
    Google Calendar via ICS feed (no OAuth).

    Use the calendar's "Secret address in iCal format" URL and treat it like a password.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=False, alias="GCAL_ENABLED")
    ics_url: str = Field(default="", alias="GCAL_ICS_URL")
    poll_seconds: int = Field(default=600, alias="GCAL_POLL_SECONDS")
    lookahead_days: int = Field(default=2, alias="GCAL_LOOKAHEAD_DAYS")

    @field_validator("ics_url", mode="before")
    @classmethod
    def _norm_url(cls, v: object) -> str:
        return _strip_quotes(str(v)).strip()


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: str = Field(default="https://api.openai.com/v1", alias="LLM_BASE_URL")
    api_key: Optional[str] = Field(default=None, alias="LLM_API_KEY")
    model: str = Field(default="gpt-4o-mini", alias="LLM_MODEL")
    timeout_seconds: float = Field(default=30, alias="LLM_TIMEOUT_SECONDS")

    @field_validator("base_url", mode="before")
    @classmethod
    def _normalize_base_url(cls, v: object) -> str:
        return _strip_quotes(str(v))


class LLMFallbackSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: str = Field(default="", alias="LLM_FALLBACK_BASE_URL")
    api_key: Optional[str] = Field(default=None, alias="LLM_FALLBACK_API_KEY")
    model: str = Field(default="", alias="LLM_FALLBACK_MODEL")
    timeout_seconds: float = Field(default=30, alias="LLM_FALLBACK_TIMEOUT_SECONDS")

    @field_validator("base_url", "model", mode="before")
    @classmethod
    def _norm_str(cls, v: object) -> str:
        return _strip_quotes(str(v)).strip()

    @field_validator("api_key", mode="before")
    @classmethod
    def _norm_opt(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = _strip_quotes(str(v)).strip()
        return s or None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


class QuietHoursSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=True, alias="QUIET_HOURS_ENABLED")
    weekday_start: str = Field(default="21:00", alias="QUIET_HOURS_WEEKDAY_START")
    weekday_end: str = Field(default="05:50", alias="QUIET_HOURS_WEEKDAY_END")
    weekend_start: str = Field(default="21:00", alias="QUIET_HOURS_WEEKEND_START")
    weekend_end: str = Field(default="06:50", alias="QUIET_HOURS_WEEKEND_END")

    @field_validator("weekday_start", "weekday_end", "weekend_start", "weekend_end", mode="before")
    @classmethod
    def _norm_time(cls, v: object) -> str:
        return _strip_quotes(str(v)).strip()


class SmtpSettings(BaseSettings):
    """
    Global SMTP settings (used by any module needing outbound email).
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(default="", alias="SMTP_HOST")
    port: int = Field(default=587, alias="SMTP_PORT")
    username: Optional[str] = Field(default=None, alias="SMTP_USERNAME")
    password: Optional[str] = Field(default=None, alias="SMTP_PASSWORD")
    from_addr: str = Field(default="", alias="SMTP_FROM")
    use_starttls: bool = Field(default=True, alias="SMTP_USE_STARTTLS")
    use_ssl: bool = Field(default=False, alias="SMTP_USE_SSL")
    timeout_seconds: float = Field(default=20.0, alias="SMTP_TIMEOUT_SECONDS")

    @field_validator("host", "from_addr", mode="before")
    @classmethod
    def _norm_str(cls, v: object) -> str:
        return _strip_quotes(str(v)).strip()

    @field_validator("username", "password", mode="before")
    @classmethod
    def _norm_opt(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = _strip_quotes(str(v)).strip()
        return s or None

    @property
    def enabled(self) -> bool:
        return bool((self.host or "").strip()) and bool((self.from_addr or "").strip())


class SunsetSceneSettings(BaseSettings):
    """
    Optional: trigger a Caseta scene at local sunset.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=False, alias="SUNSET_SCENE_ENABLED")
    scene_name: str = Field(default="", alias="SUNSET_SCENE_NAME")
    offset_minutes: int = Field(default=0, alias="SUNSET_SCENE_OFFSET_MINUTES")

    @field_validator("scene_name", mode="before")
    @classmethod
    def _norm_str(cls, v: object) -> str:
        return _strip_quotes(str(v)).strip()


class CamectSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=False, alias="CAMECT_ENABLED")
    host: str = Field(default="", alias="CAMECT_HOST")  # e.g. 10.1.2.150:443
    username: str = Field(default="", alias="CAMECT_USERNAME")
    password: Optional[str] = Field(default=None, alias="CAMECT_PASSWORD")
    camera_names: str = Field(default="", alias="CAMECT_CAMERA_NAMES")
    camera_rules: str = Field(default="", alias="CAMECT_CAMERA_RULES")
    event_filter: str = Field(default="vehicle", alias="CAMECT_EVENT_FILTER")
    throttle_seconds: int = Field(default=120, alias="CAMECT_THROTTLE_SECONDS")
    debug: bool = Field(default=False, alias="CAMECT_DEBUG")
    status_interval_seconds: int = Field(default=60, alias="CAMECT_STATUS_INTERVAL_SECONDS")
    stale_warning_seconds: int = Field(default=300, alias="CAMECT_STALE_WARNING_SECONDS")
    announce_template: str = Field(default="{kind} detected at {camera}.", alias="CAMECT_ANNOUNCE_TEMPLATE")
    # Optional: comma-delimited list of recipients to email snapshot images to (empty disables).
    email_alert_pics_to: str = Field(default="", alias="CAMECT_EMAIL_ALERT_PICS_TO")

    @field_validator(
        "host",
        "username",
        "camera_names",
        "camera_rules",
        "event_filter",
        "announce_template",
        "email_alert_pics_to",
        mode="before",
    )
    @classmethod
    def _norm_str(cls, v: object) -> str:
        return _strip_quotes(str(v)).strip()

    @field_validator("password", mode="before")
    @classmethod
    def _norm_opt(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = _strip_quotes(str(v)).strip()
        return s or None

    @property
    def camera_name_list(self) -> List[str]:
        s = (self.camera_names or "").strip()
        if not s:
            return []
        parts = [p.strip() for p in s.split(",")]
        return [p for p in parts if p]

    @property
    def camera_rules_map(self) -> Dict[str, str]:
        raw = (self.camera_rules or "").strip()
        if not raw:
            return {}

        items: List[str] = []

        def _split_rules(s: str) -> List[str]:
            out: List[str] = []
            buf: List[str] = []
            i = 0
            while i < len(s):
                ch = s[i]
                if ch == ";":
                    part = "".join(buf).strip()
                    if part:
                        out.append(part)
                    buf = []
                    i += 1
                    continue
                if ch == ",":
                    j = i + 1
                    while j < len(s) and s[j].isspace():
                        j += 1
                    k = j
                    while k < len(s) and s[k] not in ",;":
                        k += 1
                    segment = s[j:k]
                    if (":" in segment) or ("=" in segment):
                        part = "".join(buf).strip()
                        if part:
                            out.append(part)
                        buf = []
                        i += 1
                        continue
                buf.append(ch)
                i += 1
            tail = "".join(buf).strip()
            if tail:
                out.append(tail)
            return out

        for part in _split_rules(raw):
            if part:
                items.append(part)

        out: Dict[str, str] = {}
        for item in items:
            if ":" in item:
                k, v = item.split(":", 1)
            elif "=" in item:
                k, v = item.split("=", 1)
            else:
                continue
            cam = k.strip()
            tok = v.strip() if isinstance(v, str) else ""
            if cam:
                out[cam] = tok
        return out

    @property
    def email_alert_pics_to_list(self) -> List[str]:
        s = (self.email_alert_pics_to or "").strip()
        if not s:
            return []
        parts = [p.strip() for p in s.split(",")]
        return [p for p in parts if p]


class CasetaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=False, alias="CASETA_ENABLED")
    host: str = Field(default="", alias="CASETA_HOST")
    port: int = Field(default=8081, alias="CASETA_PORT")
    ca_cert_path: str = Field(default="", alias="CASETA_CA_CERT_PATH")
    cert_path: str = Field(default="", alias="CASETA_CERT_PATH")
    key_path: str = Field(default="", alias="CASETA_KEY_PATH")

    @field_validator("host", "ca_cert_path", "cert_path", "key_path", mode="before")
    @classmethod
    def _norm_str(cls, v: object) -> str:
        return _strip_quotes(str(v)).strip()


class CameraLightingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=False, alias="CAMERA_LIGHTING_ENABLED")
    only_dark: bool = Field(default=True, alias="CAMERA_LIGHTING_ONLY_DARK")
    camera_name: str = Field(default="Front_Garage", alias="CAMERA_LIGHTING_CAMERA_NAME")
    detected_obj: str = Field(default="vehicle", alias="CAMERA_LIGHTING_DETECTED_OBJ")
    caseta_device_id: str = Field(default="10", alias="CAMERA_LIGHTING_CASETA_DEVICE_ID")
    duration_seconds: int = Field(default=600, alias="CAMERA_LIGHTING_DURATION_SECONDS")
    min_retrigger_seconds: int = Field(default=30, alias="CAMERA_LIGHTING_MIN_RETRIGGER_SECONDS")

    @field_validator("camera_name", "detected_obj", "caseta_device_id", mode="before")
    @classmethod
    def _norm_str(cls, v: object) -> str:
        return _strip_quotes(str(v)).strip()


class UiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=False, alias="UI_ENABLED")
    bind_host: str = Field(default="127.0.0.1", alias="UI_BIND_HOST")
    port: int = Field(default=8001, alias="UI_PORT")
    title: str = Field(default="Home Agent", alias="UI_TITLE")
    # Format: "id|Label|Text|targets(optional)|volume(optional)|concurrency(optional);..."
    actions: str = Field(default="", alias="UI_ACTIONS")
    # Alternative, easier-to-edit format: one action per env var line.
    # (We define a finite set to keep parsing simple and dotenv-friendly.)
    action_1: str = Field(default="", alias="UI_ACTION_1")
    action_2: str = Field(default="", alias="UI_ACTION_2")
    action_3: str = Field(default="", alias="UI_ACTION_3")
    action_4: str = Field(default="", alias="UI_ACTION_4")
    action_5: str = Field(default="", alias="UI_ACTION_5")
    action_6: str = Field(default="", alias="UI_ACTION_6")
    action_7: str = Field(default="", alias="UI_ACTION_7")
    action_8: str = Field(default="", alias="UI_ACTION_8")
    action_9: str = Field(default="", alias="UI_ACTION_9")
    action_10: str = Field(default="", alias="UI_ACTION_10")
    action_11: str = Field(default="", alias="UI_ACTION_11")
    action_12: str = Field(default="", alias="UI_ACTION_12")
    action_13: str = Field(default="", alias="UI_ACTION_13")
    action_14: str = Field(default="", alias="UI_ACTION_14")
    action_15: str = Field(default="", alias="UI_ACTION_15")
    action_16: str = Field(default="", alias="UI_ACTION_16")
    action_17: str = Field(default="", alias="UI_ACTION_17")
    action_18: str = Field(default="", alias="UI_ACTION_18")
    action_19: str = Field(default="", alias="UI_ACTION_19")
    action_20: str = Field(default="", alias="UI_ACTION_20")

    @field_validator("bind_host", "title", "actions", mode="before")
    @classmethod
    def _norm_str(cls, v: object) -> str:
        return _strip_quotes(str(v)).strip()

    def actions_list(self) -> List[Dict[str, object]]:
        # Prefer per-line UI_ACTION_N entries when present.
        per_line: List[str] = []
        for i in range(1, 21):
            v = getattr(self, f"action_{i}", "") or ""
            v = _strip_quotes(str(v)).strip()
            if v:
                per_line.append(v)
        if per_line:
            return _parse_ui_action_entries(per_line)

        # Otherwise fall back to the single-string UI_ACTIONS value.
        raw = _strip_quotes(self.actions or "").strip()
        if not raw:
            return [
                {"id": "dinner", "label": "Call to Dinner", "text": "Dinner time. Please come to the table."},
                {"id": "kids_up", "label": "Kids Upstairs", "text": "Kids, please come upstairs."},
            ]

        entries = [c.strip() for c in raw.split(";") if c.strip()]
        return _parse_ui_action_entries(entries)


def _parse_ui_action_entries(entries: List[str]) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for item in entries:
        parts = [p.strip() for p in str(item).split("|")]
        if len(parts) < 3:
            continue
        action_id, label, text = parts[0], parts[1], parts[2]
        if not action_id or not label or not text:
            continue

        data: Dict[str, object] = {"id": action_id, "label": label, "text": text}

        if len(parts) >= 4 and parts[3]:
            t = [p.strip() for p in str(parts[3]).split(",")]
            t = [p for p in t if p]
            if t:
                data["targets"] = t

        if len(parts) >= 5 and parts[4]:
            try:
                v = int(float(parts[4]))
                data["volume"] = max(0, min(100, v))
            except Exception:
                pass

        if len(parts) >= 6 and parts[5]:
            try:
                c = int(float(parts[5]))
                data["concurrency"] = max(1, c)
            except Exception:
                pass

        out.append(data)
    return out

class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    name: str = Field(default="home-agent", alias="HOME_AGENT_NAME")
    log_level: str = Field(default="INFO", alias="HOME_AGENT_LOG_LEVEL")
    timezone: str = Field(default="UTC", alias="HOME_AGENT_TIMEZONE")

    llm: LLMSettings = LLMSettings()
    llm_fallback: LLMFallbackSettings = LLMFallbackSettings()
    sonos: SonosSettings = SonosSettings()
    elevenlabs: ElevenLabsSettings = ElevenLabsSettings()
    mqtt: MqttSettings = MqttSettings()
    db: DbSettings = DbSettings()
    weather: WeatherSettings = WeatherSettings()
    gcal: GCalSettings = GCalSettings()
    quiet_hours: QuietHoursSettings = QuietHoursSettings()
    smtp: SmtpSettings = SmtpSettings()
    sunset_scene: SunsetSceneSettings = SunsetSceneSettings()
    camect: CamectSettings = CamectSettings()
    caseta: CasetaSettings = CasetaSettings()
    camera_lighting: CameraLightingSettings = CameraLightingSettings()
    ui: UiSettings = UiSettings()

