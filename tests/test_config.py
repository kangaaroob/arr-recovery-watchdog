"""Configuration tests: defaults, secret files, precedence, validation errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from arr_recovery_watchdog.config import ConfigError, load_config, redact_url


def env_with_prowlarr_key(tmp_path: Path, **extra: str) -> dict[str, str]:
    key_file = tmp_path / "prowlarr_key"
    key_file.write_text("prowlarr-key-abc\n", encoding="utf-8")
    env = {"PROWLARR_API_KEY_FILE": str(key_file)}
    env.update(extra)
    return env


def test_defaults_are_applied(tmp_path: Path) -> None:
    config = load_config(env_with_prowlarr_key(tmp_path))
    assert config.gluetun_health_url == "http://127.0.0.1:9999/"
    assert config.prowlarr.base_url == "http://127.0.0.1:9696"
    assert config.prowlarr.testall_url == "http://127.0.0.1:9696/api/v1/indexer/testall"
    assert config.poll_interval_seconds == 5.0
    assert config.healthy_stability_seconds == 60.0
    assert config.prowlarr_settle_seconds == 10.0
    assert config.action_cooldown_seconds == 1800.0
    assert config.http_connect_timeout_seconds == 5.0
    assert config.http_response_timeout_seconds == 60.0
    assert config.api_retry_count == 3
    assert config.state_file == Path("/data/state.json")
    assert config.log_level == "INFO"
    assert config.dry_run is False


def test_secret_file_content_is_stripped(tmp_path: Path) -> None:
    config = load_config(env_with_prowlarr_key(tmp_path))
    assert config.prowlarr.api_key == "prowlarr-key-abc"  # trailing newline removed


def test_api_key_file_takes_precedence_over_env_var(tmp_path: Path) -> None:
    env = env_with_prowlarr_key(tmp_path, PROWLARR_API_KEY="env-key-should-lose")
    config = load_config(env)
    assert config.prowlarr.api_key == "prowlarr-key-abc"


def test_direct_env_api_key_works_without_file() -> None:
    config = load_config({"PROWLARR_API_KEY": "direct-key"})
    assert config.prowlarr.api_key == "direct-key"


def test_missing_prowlarr_key_is_a_clear_config_error() -> None:
    with pytest.raises(ConfigError, match="Prowlarr API key"):
        load_config({})


def test_missing_secret_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read secret file"):
        load_config({"PROWLARR_API_KEY_FILE": str(tmp_path / "does-not-exist")})


def test_empty_secret_file_is_a_config_error(tmp_path: Path) -> None:
    key_file = tmp_path / "empty_key"
    key_file.write_text("   \n", encoding="utf-8")
    with pytest.raises(ConfigError, match="is empty"):
        load_config({"PROWLARR_API_KEY_FILE": str(key_file)})


def test_sonarr_enabled_when_key_is_provided(tmp_path: Path) -> None:
    config = load_config(env_with_prowlarr_key(tmp_path, SONARR_API_KEY="sonarr-key"))
    assert config.sonarr is not None
    assert config.sonarr.testall_url == "http://sonarr:8989/api/v3/indexer/testall"


def test_sonarr_skipped_cleanly_without_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        config = load_config(env_with_prowlarr_key(tmp_path))
    assert config.sonarr is None
    assert config.radarr is None
    assert "Sonarr" in caplog.text


def test_sonarr_disabled_silently_with_empty_url(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        config = load_config(env_with_prowlarr_key(tmp_path, SONARR_URL="", RADARR_URL=""))
    assert config.sonarr is None
    assert config.radarr is None
    assert "Sonarr" not in caplog.text


def test_empty_prowlarr_url_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="PROWLARR_URL"):
        load_config(env_with_prowlarr_key(tmp_path, PROWLARR_URL=""))


def test_invalid_url_scheme_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="http"):
        load_config(env_with_prowlarr_key(tmp_path, PROWLARR_URL="ftp://prowlarr:9696"))


def test_invalid_number_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="POLL_INTERVAL_SECONDS"):
        load_config(env_with_prowlarr_key(tmp_path, POLL_INTERVAL_SECONDS="fast"))


def test_zero_poll_interval_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="POLL_INTERVAL_SECONDS"):
        load_config(env_with_prowlarr_key(tmp_path, POLL_INTERVAL_SECONDS="0"))


def test_negative_retry_count_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="API_RETRY_COUNT"):
        load_config(env_with_prowlarr_key(tmp_path, API_RETRY_COUNT="-1"))


def test_invalid_bool_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="DRY_RUN"):
        load_config(env_with_prowlarr_key(tmp_path, DRY_RUN="maybe"))


@pytest.mark.parametrize("value", ["true", "1", "TRUE", "yes", "on"])
def test_dry_run_true_values(tmp_path: Path, value: str) -> None:
    config = load_config(env_with_prowlarr_key(tmp_path, DRY_RUN=value))
    assert config.dry_run is True


def test_invalid_log_level_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load_config(env_with_prowlarr_key(tmp_path, LOG_LEVEL="CHATTY"))


def test_secrets_property_lists_all_configured_keys(tmp_path: Path) -> None:
    config = load_config(
        env_with_prowlarr_key(tmp_path, SONARR_API_KEY="sonarr-key", RADARR_API_KEY="radarr-key")
    )
    assert set(config.secrets) == {"prowlarr-key-abc", "sonarr-key", "radarr-key"}


def test_redact_url_strips_credentials() -> None:
    assert redact_url("http://user:pass@host:1234/path") == "http://host:1234/path"
    assert redact_url("http://plain-host:1234/") == "http://plain-host:1234/"
