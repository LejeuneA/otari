"""The guardrail overlay: what a request resolves a profile name to.

The overlay is the piece that lets the request path ask "is this profile defined
here?" without a query per request. It holds what a refresh last read from
``guardrail_credentials``, and the resolver merges the operator's ``guardrails:``
config block underneath it.

The database half (a real refresh, a real write) is covered through the route in
``tests/integration/test_guardrail_credentials_api.py``. What is here is the
resolution rule, which is worth pinning on its own because getting it wrong is
silent: a stored row that stops shadowing its config twin means a guardrail an
operator switched off starts running again.

``any_guardrail``'s registry is real, as it is in the store and runner tests.
Nothing constructs a guardrail, so nothing loads a model backend.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pytest

from gateway.core.config import GatewayConfig
from gateway.models.entities import GuardrailCredential
from gateway.services.guardrail_runner import GuardrailDefinition
from gateway.services.guardrail_store_service import (
    _cache,
    _cache_value,
    cache_is_stale,
    cached_guardrails,
    local_guardrail_definition,
    reset_guardrail_cache,
)
from gateway.services.secret_box import encrypt_secret, generate_secret_key

_LAKERA = "lakera_guard"
_PROFILE = "prompt-injection"


@pytest.fixture(autouse=True)
def _drop_the_shared_cache() -> Any:
    """Never let one test's overlay answer another's lookup.

    The cache is a module global, so a primed entry outlives the test that
    primed it and would resolve a name the next test expects to be unknown.
    """
    reset_guardrail_cache()
    yield
    reset_guardrail_cache()


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTARI_SECRET_KEY", generate_secret_key())


def _config(**guardrails: dict[str, Any]) -> GatewayConfig:
    return GatewayConfig(guardrails=dict(guardrails))


def _entry(guardrail_name: str = _LAKERA, **rest: Any) -> dict[str, Any]:
    return {"guardrail_name": guardrail_name, **rest}


def _prime(**overlay: GuardrailDefinition | None) -> None:
    """Put the overlay in the state a refresh would have left it in."""
    _cache.update(overlay)


@contextmanager
def _gateway_warnings(caplog: pytest.LogCaptureFixture) -> Generator[None]:
    """Capture the gateway logger, which does not propagate to root by default."""
    gateway_logger = logging.getLogger("gateway")
    gateway_logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger="gateway")
    try:
        yield
    finally:
        gateway_logger.removeHandler(caplog.handler)


def _row(
    *,
    name: str = _PROFILE,
    guardrail_name: str = _LAKERA,
    create_kwargs: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
    enabled: bool = True,
) -> GuardrailCredential:
    row = GuardrailCredential(
        name=name,
        guardrail_name=guardrail_name,
        create_kwargs=create_kwargs if create_kwargs is not None else {},
        validate_kwargs={},
        enabled=enabled,
    )
    row.encrypted_create_secrets = encrypt_secret(json.dumps(secrets)) if secrets else None
    return row


# --------------------------------------------------------------------------- #
# The config half
# --------------------------------------------------------------------------- #


def test_a_config_entry_resolves_to_a_definition() -> None:
    config = _config(**{_PROFILE: _entry(create_kwargs={"api_key": "k"}, validate_kwargs={"threshold": 0.5})})

    definition = local_guardrail_definition(config, _PROFILE)

    assert definition == GuardrailDefinition(
        guardrail_name=_LAKERA,
        create_kwargs={"api_key": "k"},
        validate_kwargs={"threshold": 0.5},
    )


def test_a_config_entry_without_enabled_is_on() -> None:
    """The loader leaves the key out rather than filling it in, so absent means on."""
    config = _config(**{_PROFILE: _entry()})

    assert local_guardrail_definition(config, _PROFILE) is not None


def test_a_disabled_config_entry_resolves_to_nothing() -> None:
    config = _config(**{_PROFILE: _entry(enabled=False)})

    assert local_guardrail_definition(config, _PROFILE) is None


def test_an_unknown_profile_resolves_to_nothing() -> None:
    assert local_guardrail_definition(_config(), _PROFILE) is None


# --------------------------------------------------------------------------- #
# A stored row owns its name
# --------------------------------------------------------------------------- #


def test_a_stored_row_wins_over_a_config_entry_of_the_same_name() -> None:
    config = _config(**{_PROFILE: _entry(create_kwargs={"api_key": "from-file"})})
    _prime(**{_PROFILE: GuardrailDefinition(guardrail_name=_LAKERA, create_kwargs={"api_key": "stored"})})

    definition = local_guardrail_definition(config, _PROFILE)

    assert definition is not None
    assert definition.create_kwargs == {"api_key": "stored"}


def test_a_disabled_stored_row_shadows_an_enabled_config_entry() -> None:
    """Switching a stored guardrail off must not hand its name back to the file.

    The config entry may have been written from different arguments, and the
    listing already reports it as shadowed whatever the row's `enabled` says.
    Falling through would make that listing a lie and quietly resume a check an
    operator had switched off.
    """
    config = _config(**{_PROFILE: _entry(create_kwargs={"api_key": "from-file"})})
    _prime(**{_PROFILE: None})

    assert local_guardrail_definition(config, _PROFILE) is None


def test_a_stored_row_shadows_a_config_entry_it_does_not_collide_with() -> None:
    """Shadowing is per name, so an unrelated config entry is untouched."""
    config = _config(other=_entry(), **{_PROFILE: _entry()})
    _prime(**{_PROFILE: None})

    assert local_guardrail_definition(config, _PROFILE) is None
    assert local_guardrail_definition(config, "other") is not None


# --------------------------------------------------------------------------- #
# What a refresh caches for one row
# --------------------------------------------------------------------------- #


def test_an_enabled_row_caches_its_definition() -> None:
    value = _cache_value(_row(create_kwargs={"model": "m"}, secrets={"api_key": "sk-secret"}))

    assert value == GuardrailDefinition(
        guardrail_name=_LAKERA,
        create_kwargs={"model": "m", "api_key": "sk-secret"},
        validate_kwargs={},
    )


def test_a_disabled_row_caches_nothing_to_run() -> None:
    """Cached as None rather than skipped, which is what makes it shadow."""
    assert _cache_value(_row(enabled=False)) is None


def test_a_row_whose_secrets_will_not_decrypt_caches_nothing_to_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken row still owns its name.

    The provider overlay drops such a row, which is right there because a
    provider has no "present but off" state. Here it would let a config entry of
    the same name resurrect, and the listing already reports the row as
    `decryptable: false` and shadowing. The request path agrees with it.
    """
    row = _row(secrets={"api_key": "sk-secret"})
    monkeypatch.setenv("OTARI_SECRET_KEY", generate_secret_key())  # the row was sealed under the old one

    with _gateway_warnings(caplog):
        assert _cache_value(row) is None

    assert _PROFILE in caplog.text
    assert "sk-secret" not in caplog.text


# --------------------------------------------------------------------------- #
# Hybrid: an empty overlay is a valid answer, not a cache miss
# --------------------------------------------------------------------------- #


def test_an_empty_overlay_still_sees_the_config_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hybrid has no database, and the config block is its only definition source.

    The resolver must never treat an empty cache as "not loaded yet" and reach
    for a session. Monkeypatching `create_session` to raise is what pins that:
    a lazy load added later fails here rather than in production.
    """

    def _no_database(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the resolver must not open a session")

    monkeypatch.setattr("gateway.services.guardrail_store_service.create_session", _no_database)
    config = _config(**{_PROFILE: _entry()})

    assert cached_guardrails() == {}
    assert local_guardrail_definition(config, _PROFILE) is not None


def test_a_stale_cache_is_served_rather_than_reloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Staleness is the refresher's business, never the resolver's."""

    def _no_database(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the resolver must not open a session")

    monkeypatch.setattr("gateway.services.guardrail_store_service.create_session", _no_database)
    _prime(**{_PROFILE: GuardrailDefinition(guardrail_name=_LAKERA)})

    assert cache_is_stale() is True, "nothing has stamped a load"
    assert local_guardrail_definition(_config(), _PROFILE) is not None


# --------------------------------------------------------------------------- #
# Cache bookkeeping
# --------------------------------------------------------------------------- #


def test_reset_empties_the_overlay() -> None:
    _prime(**{_PROFILE: GuardrailDefinition(guardrail_name=_LAKERA)})

    reset_guardrail_cache()

    assert cached_guardrails() == {}
    assert cache_is_stale() is True


def test_cached_guardrails_is_a_copy() -> None:
    _prime(**{_PROFILE: None})

    cached_guardrails()[_PROFILE] = GuardrailDefinition(guardrail_name=_LAKERA)

    assert local_guardrail_definition(_config(), _PROFILE) is None
