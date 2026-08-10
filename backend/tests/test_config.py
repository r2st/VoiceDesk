"""Settings validation, and the production guard in particular.

The guard exists because the failure it prevents is invisible. An API booted
with the example ``JWT_SECRET`` starts cleanly, passes its health checks and
serves traffic normally — while anyone holding the published repository can
mint a valid token for any tenant. These tests pin the boot failure so the
guard cannot be softened by accident.

Every case constructs ``Settings`` directly with ``_env_file=None``: the real
``.env`` sits next to the test run and would otherwise supply values and mask
the defaults under test.
"""

from __future__ import annotations

import pytest

from app.core.config import (
    DEFAULT_JWT_SECRET,
    DEFAULT_WEBHOOK_SECRET,
    InsecureConfigurationError,
    Settings,
)

#: A secret that clears the length floor, for cases testing a *different* field.
STRONG_SECRET = "s" * 40
#: A syntactically valid base64 AES-256 key.
VALID_RECORDING_KEY = "0" * 43 + "="


def make_settings(**values: object) -> Settings:
    """Build ``Settings`` from explicit values only, ignoring any on-disk ``.env``."""
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def production_settings(**overrides: object) -> Settings:
    """A production configuration that is safe unless a test breaks one field."""
    values: dict[str, object] = {
        "voicedesk_env": "production",
        "jwt_secret": STRONG_SECRET,
        "webhook_hmac_secret": "webhook-" + STRONG_SECRET,
        "recording_encryption_key": VALID_RECORDING_KEY,
        "cors_origins": ["https://app.voicedesk.test"],
    }
    values.update(overrides)
    return make_settings(**values)


class TestProductionGuard:
    def test_a_fully_configured_production_environment_boots(self):
        settings = production_settings()
        assert settings.is_production

    def test_the_example_jwt_secret_is_refused(self):
        with pytest.raises(InsecureConfigurationError, match="JWT_SECRET"):
            production_settings(jwt_secret=DEFAULT_JWT_SECRET)

    def test_a_short_jwt_secret_is_refused(self):
        """HS256 keys under 32 chars are brute-forceable from one captured token."""
        with pytest.raises(InsecureConfigurationError, match="at least 32 characters"):
            production_settings(jwt_secret="short-but-not-the-default")

    def test_a_jwt_secret_exactly_at_the_floor_is_accepted(self):
        settings = production_settings(jwt_secret="k" * 32)
        assert len(settings.jwt_secret) == 32

    def test_the_example_webhook_secret_is_refused(self):
        with pytest.raises(InsecureConfigurationError, match="WEBHOOK_HMAC_SECRET"):
            production_settings(webhook_hmac_secret=DEFAULT_WEBHOOK_SECRET)

    def test_an_empty_webhook_secret_is_refused(self):
        """Empty would make ``_verified_body`` accept unsigned provider callbacks."""
        with pytest.raises(InsecureConfigurationError, match="WEBHOOK_HMAC_SECRET"):
            production_settings(webhook_hmac_secret="")

    def test_a_missing_recording_encryption_key_is_refused(self):
        with pytest.raises(InsecureConfigurationError, match="RECORDING_ENCRYPTION_KEY"):
            production_settings(recording_encryption_key="")

    def test_wildcard_cors_is_refused(self):
        """``allow_credentials`` plus ``*`` would let any site call the API as the user."""
        with pytest.raises(InsecureConfigurationError, match="CORS_ORIGINS"):
            production_settings(cors_origins=["*"])

    def test_a_wildcard_among_real_origins_is_still_refused(self):
        with pytest.raises(InsecureConfigurationError, match="CORS_ORIGINS"):
            production_settings(cors_origins=["https://app.voicedesk.test", "*"])

    def test_every_problem_is_reported_at_once(self):
        """One boot, one complete list — not a fix-and-rediscover loop per deploy."""
        with pytest.raises(InsecureConfigurationError) as excinfo:
            production_settings(
                jwt_secret=DEFAULT_JWT_SECRET,
                webhook_hmac_secret=DEFAULT_WEBHOOK_SECRET,
                recording_encryption_key="",
                cors_origins=["*"],
            )
        message = str(excinfo.value)
        for field in (
            "JWT_SECRET",
            "WEBHOOK_HMAC_SECRET",
            "RECORDING_ENCRYPTION_KEY",
            "CORS_ORIGINS",
        ):
            assert field in message

    @pytest.mark.parametrize("env", ["production", "PRODUCTION", "prod", "Prod"])
    def test_the_guard_applies_to_every_spelling_of_production(self, env: str):
        with pytest.raises(InsecureConfigurationError):
            production_settings(voicedesk_env=env, jwt_secret=DEFAULT_JWT_SECRET)


class TestDevelopmentDefaults:
    """Development must stay zero-configuration; the guard is production-only."""

    @pytest.mark.parametrize("env", ["development", "test", "staging", "local"])
    def test_placeholder_secrets_are_allowed_outside_production(self, env: str):
        settings = make_settings(voicedesk_env=env, jwt_secret=DEFAULT_JWT_SECRET)
        assert not settings.is_production
        assert settings.jwt_secret == DEFAULT_JWT_SECRET

    def test_defaults_alone_are_a_usable_development_configuration(self):
        settings = make_settings()
        assert not settings.is_production
        assert settings.jwt_secret == DEFAULT_JWT_SECRET
        assert settings.cors_origins == ["http://localhost:3011"]


class TestCsvFields:
    """List settings arrive from the environment as comma-separated strings."""

    def test_cors_origins_accepts_a_comma_separated_string(self):
        settings = make_settings(cors_origins="https://a.test, https://b.test")
        assert settings.cors_origins == ["https://a.test", "https://b.test"]

    def test_blank_entries_are_dropped(self):
        settings = make_settings(cors_origins="https://a.test, ,https://b.test,")
        assert settings.cors_origins == ["https://a.test", "https://b.test"]

    def test_a_list_is_passed_through_unchanged(self):
        settings = make_settings(openrouter_models=["model-a", "model-b"])
        assert settings.openrouter_models == ["model-a", "model-b"]
