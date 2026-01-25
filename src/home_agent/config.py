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
    default_volume: int = Field(default=50, alias="SONOS_DEFAULT_VOLUME")
    announce_concurrency: int = Field(default=3, alias="SONOS_ANNOUNCE_CONCURRENCY")
    tail_padding_seconds: float = Field(default=3.0, alias="SONOS_TAIL_PADDING_SECONDS")
    # Optional per-speaker volume overrides.
    # Format: "10.1.2.58:35,10.1.2.72:45" (comma/semicolon delimited)
    speaker_volumes: str = Field(default="", alias="SONOS_SPEAKER_VOLUMES")

    @field_validator("announce_targets", mode="before")
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

        # Then apply explicit overrides from SONOS_SPEAKER_VOLUMES (recommended).
        explicit = _parse_sonos_speaker_volumes(self.speaker_volumes or "")

        out: Dict[str, int] = dict(embedded)
        out.update(explicit)
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

    @field_validator(
        "host",
        "username",
        "camera_names",
        "camera_rules",
        "event_filter",
        "announce_template",
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
    camect: CamectSettings = CamectSettings()
    caseta: CasetaSettings = CasetaSettings()
    camera_lighting: CameraLightingSettings = CameraLightingSettings()

