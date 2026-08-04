"""Configuration loading and validation from environment variables."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("watchdog.config")

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

PROWLARR_TESTALL_PATH = "/api/v1/indexer/testall"
V3_TESTALL_PATH = "/api/v3/indexer/testall"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigError(Exception):
    """Raised when the environment configuration is invalid."""


@dataclass(frozen=True)
class ArrApp:
    """One Arr application target (Prowlarr, Sonarr, or Radarr)."""

    name: str
    base_url: str
    api_key: str
    testall_path: str

    @property
    def testall_url(self) -> str:
        return self.base_url.rstrip("/") + self.testall_path


@dataclass(frozen=True)
class Config:
    gluetun_health_url: str
    prowlarr: ArrApp
    sonarr: ArrApp | None
    radarr: ArrApp | None
    poll_interval_seconds: float
    healthy_stability_seconds: float
    prowlarr_settle_seconds: float
    action_cooldown_seconds: float
    http_connect_timeout_seconds: float
    http_response_timeout_seconds: float
    api_retry_count: int
    state_file: Path
    log_level: str
    dry_run: bool

    @property
    def secrets(self) -> list[str]:
        """All configured secret values, for log redaction."""
        apps = [self.prowlarr, self.sonarr, self.radarr]
        return [app.api_key for app in apps if app is not None and app.api_key]


def redact_url(url: str) -> str:
    """Return the URL with any userinfo credentials removed."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable-url>"
    if "@" in parts.netloc:
        host = parts.netloc.rsplit("@", 1)[1]
        return urlunsplit(parts._replace(netloc=host))
    return url


def _get_str(env: Mapping[str, str], name: str, default: str) -> str:
    raw = env.get(name)
    return default if raw is None else raw.strip()


def _get_url(env: Mapping[str, str], name: str, default: str) -> str:
    value = _get_str(env, name, default)
    if value and not value.startswith(("http://", "https://")):
        raise ConfigError(f"{name} must start with http:// or https://, got {redact_url(value)!r}")
    return value


def _get_float(
    env: Mapping[str, str], name: str, default: float, *, allow_zero: bool = False
) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0 or (value == 0 and not allow_zero):
        bound = ">= 0" if allow_zero else "> 0"
        raise ConfigError(f"{name} must be {bound}, got {raw!r}")
    return value


def _get_int(env: Mapping[str, str], name: str, default: int, *, minimum: int = 0) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {raw!r}")
    return value


def _get_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}")


def _read_api_key(env: Mapping[str, str], prefix: str) -> str | None:
    """Resolve <prefix>_API_KEY, with <prefix>_API_KEY_FILE taking precedence."""
    file_var = f"{prefix}_API_KEY_FILE"
    file_path = env.get(file_var, "").strip()
    if file_path:
        try:
            value = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            message = f"{file_var}: cannot read secret file {file_path!r}: {reason}"
            raise ConfigError(message) from exc
        if not value:
            raise ConfigError(f"{file_var}: secret file {file_path!r} is empty")
        return value
    return env.get(f"{prefix}_API_KEY", "").strip() or None


def _optional_app(
    env: Mapping[str, str], prefix: str, name: str, default_url: str
) -> ArrApp | None:
    url = _get_url(env, f"{prefix}_URL", default_url)
    api_key = _read_api_key(env, prefix)
    if not url:
        logger.info("%s_URL is empty; %s is disabled", prefix, name)
        return None
    if api_key is None:
        logger.warning(
            "%s has a URL but no API key (%s_API_KEY_FILE or %s_API_KEY); it will be skipped "
            "during recovery. Set %s_URL to an empty string to disable it silently.",
            name,
            prefix,
            prefix,
            prefix,
        )
        return None
    return ArrApp(name=name, base_url=url, api_key=api_key, testall_path=V3_TESTALL_PATH)


def load_config(env: Mapping[str, str]) -> Config:
    """Build a validated Config from environment variables.

    Raises ConfigError with an actionable message on any invalid setting.
    """
    gluetun_health_url = _get_url(env, "GLUETUN_HEALTH_URL", "http://127.0.0.1:9999/")
    if not gluetun_health_url:
        raise ConfigError("GLUETUN_HEALTH_URL must not be empty")

    prowlarr_url = _get_url(env, "PROWLARR_URL", "http://127.0.0.1:9696")
    if not prowlarr_url:
        raise ConfigError("PROWLARR_URL must not be empty; Prowlarr is required")
    prowlarr_key = _read_api_key(env, "PROWLARR")
    if prowlarr_key is None:
        raise ConfigError(
            "No Prowlarr API key configured. Set PROWLARR_API_KEY_FILE to a mounted secret "
            "file (recommended) or PROWLARR_API_KEY."
        )
    prowlarr = ArrApp(
        name="Prowlarr",
        base_url=prowlarr_url,
        api_key=prowlarr_key,
        testall_path=PROWLARR_TESTALL_PATH,
    )

    log_level = _get_str(env, "LOG_LEVEL", "INFO").upper()
    if log_level not in VALID_LOG_LEVELS:
        valid = ", ".join(VALID_LOG_LEVELS)
        raise ConfigError(f"LOG_LEVEL must be one of {valid}, got {log_level!r}")

    return Config(
        gluetun_health_url=gluetun_health_url,
        prowlarr=prowlarr,
        sonarr=_optional_app(env, "SONARR", "Sonarr", "http://sonarr:8989"),
        radarr=_optional_app(env, "RADARR", "Radarr", "http://radarr:7878"),
        poll_interval_seconds=_get_float(env, "POLL_INTERVAL_SECONDS", 5.0),
        healthy_stability_seconds=_get_float(env, "HEALTHY_STABILITY_SECONDS", 60.0),
        prowlarr_settle_seconds=_get_float(env, "PROWLARR_SETTLE_SECONDS", 10.0, allow_zero=True),
        action_cooldown_seconds=_get_float(env, "ACTION_COOLDOWN_SECONDS", 1800.0, allow_zero=True),
        http_connect_timeout_seconds=_get_float(env, "HTTP_CONNECT_TIMEOUT_SECONDS", 5.0),
        http_response_timeout_seconds=_get_float(env, "HTTP_RESPONSE_TIMEOUT_SECONDS", 60.0),
        api_retry_count=_get_int(env, "API_RETRY_COUNT", 3),
        state_file=Path(_get_str(env, "STATE_FILE", "/data/state.json")),
        log_level=log_level,
        dry_run=_get_bool(env, "DRY_RUN", False),
    )
