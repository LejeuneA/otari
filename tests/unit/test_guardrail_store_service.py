"""The pure half of the guardrail store: splitting, masking and building a definition.

The database half is covered through the route, in
``tests/integration/test_guardrail_credentials_api.py``. What is here is
everything that decides *what gets written*, which is the part worth pinning
down on its own: a secret that lands in the plain column is a secret in a
response body, and no integration assertion would catch it as clearly.

``any_guardrail``'s registry is real, as it is in the catalog and runner tests.
Nothing constructs a guardrail, so nothing loads a model backend.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from gateway.core.config import GatewayConfig
from gateway.models.entities import GuardrailCredential
from gateway.services.guardrail_store_service import (
    GuardrailArgumentError,
    config_file_guardrails,
    decrypt_create_secrets,
    definition_from_config_entry,
    definition_from_row,
    resolve_create_kwargs,
)
from gateway.services.secret_box import SecretDecryptionError, encrypt_secret, generate_secret_key

_LAKERA = "lakera_guard"
_BEDROCK = "bedrock_guardrails"


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTARI_SECRET_KEY", generate_secret_key())


def _row(
    *,
    name: str = "prompt-injection",
    guardrail_name: str = _LAKERA,
    create_kwargs: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
    validate_kwargs: dict[str, Any] | None = None,
    enabled: bool = True,
) -> GuardrailCredential:
    """A stored row built in memory. Nothing here needs a session."""
    row = GuardrailCredential(
        name=name,
        guardrail_name=guardrail_name,
        create_kwargs=create_kwargs if create_kwargs is not None else {},
        validate_kwargs=validate_kwargs if validate_kwargs is not None else {},
        enabled=enabled,
    )
    row.encrypted_create_secrets = encrypt_secret(__import__("json").dumps(secrets)) if secrets else None
    return row


# --------------------------------------------------------------------------- #
# Splitting submitted arguments
# --------------------------------------------------------------------------- #


def test_a_secret_is_split_out_and_a_plain_argument_is_not() -> None:
    """The whole point: what the catalog calls secret never reaches a plain column."""
    plain, secrets = resolve_create_kwargs(_LAKERA, {"api_key": "lakera-live", "breakdown": True}, stored={})

    assert plain == {"breakdown": True}
    assert secrets == {"api_key": "lakera-live"}


def test_a_guardrail_with_several_secrets_puts_them_all_in_the_map() -> None:
    """Bedrock takes two storable secrets, which is why this is a map and not a column."""
    plain, secrets = resolve_create_kwargs(
        _BEDROCK,
        {
            "guardrail_identifier": "gr-123",
            "region_name": "us-east-1",
            "aws_access_key_id": "AKIA",
            "aws_secret_access_key": "shhh",
        },
        stored={},
    )

    assert plain == {"guardrail_identifier": "gr-123", "region_name": "us-east-1"}
    assert secrets == {"aws_access_key_id": "AKIA", "aws_secret_access_key": "shhh"}


def test_a_guardrail_with_no_secret_produces_an_empty_map() -> None:
    """``any_llm`` takes everything per call, so there is nothing to encrypt."""
    plain, secrets = resolve_create_kwargs("any_llm", {}, stored={})

    assert plain == {}
    assert secrets == {}


def test_a_secret_that_cannot_be_written_down_is_refused() -> None:
    """``boto3_session`` is a live object, not text. No row can hold one."""
    with pytest.raises(GuardrailArgumentError, match="boto3_session"):
        resolve_create_kwargs(
            _BEDROCK,
            {"guardrail_identifier": "gr-123", "boto3_session": {"region": "us-east-1"}},
            stored={},
        )


def test_an_unknown_argument_is_refused() -> None:
    """No guardrail takes ``**kwargs``, so this would be a TypeError at build time."""
    with pytest.raises(GuardrailArgumentError, match="api_ky"):
        resolve_create_kwargs(_LAKERA, {"api_ky": "typo"}, stored={})


def test_a_missing_required_argument_is_refused() -> None:
    with pytest.raises(GuardrailArgumentError, match="guardrail_identifier"):
        resolve_create_kwargs(_BEDROCK, {"region_name": "us-east-1"}, stored={})


def test_an_unknown_guardrail_is_refused() -> None:
    with pytest.raises(GuardrailArgumentError, match="lakera-guard"):
        resolve_create_kwargs("lakera-guard", {}, stored={})


# --------------------------------------------------------------------------- #
# The redaction round trip
# --------------------------------------------------------------------------- #


def test_the_mask_keeps_the_stored_secret() -> None:
    """An editor resubmits the whole object, so it sends back what it was shown."""
    plain, secrets = resolve_create_kwargs(
        _LAKERA,
        {"api_key": "***", "breakdown": True},
        stored={"api_key": "lakera-live", "breakdown": False},
    )

    assert secrets == {"api_key": "lakera-live"}
    assert plain == {"breakdown": True}


def test_a_new_value_rotates_the_stored_secret() -> None:
    _, secrets = resolve_create_kwargs(_LAKERA, {"api_key": "lakera-rotated"}, stored={"api_key": "lakera-live"})

    assert secrets == {"api_key": "lakera-rotated"}


def test_an_omitted_secret_is_cleared() -> None:
    """The map is replaced, not merged, so dropping a key drops the secret.

    An operator moving Lakera onto ``LAKERA_API_KEY`` needs a way to remove the
    stored one, and omitting it from the submitted object is that way.
    """
    _, secrets = resolve_create_kwargs(_LAKERA, {"breakdown": True}, stored={"api_key": "lakera-live"})

    assert secrets == {}


def test_the_mask_with_nothing_stored_is_refused() -> None:
    """Otherwise ``***`` would be written as if it were the key itself."""
    with pytest.raises(GuardrailArgumentError, match="api_key"):
        resolve_create_kwargs(_LAKERA, {"api_key": "***"}, stored={})


# --------------------------------------------------------------------------- #
# Reading a definition back
# --------------------------------------------------------------------------- #


def test_a_definition_merges_the_plain_and_secret_halves() -> None:
    """What the runner gets is one map again; the split is storage, not semantics."""
    row = _row(create_kwargs={"breakdown": True}, secrets={"api_key": "lakera-live"}, validate_kwargs={"payload": True})

    definition = definition_from_row(row)

    assert definition.guardrail_name == _LAKERA
    assert definition.create_kwargs == {"breakdown": True, "api_key": "lakera-live"}
    assert definition.validate_kwargs == {"payload": True}


def test_a_row_with_no_secrets_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``any_llm`` stores nothing encrypted, so it works with OTARI_SECRET_KEY unset.

    The key is dropped after the row is built, because building it is what the
    autouse fixture's key is for. Leaving it set would let this pass without
    proving anything.
    """
    row = _row(guardrail_name="any_llm", secrets=None)
    monkeypatch.delenv("OTARI_SECRET_KEY", raising=False)

    assert definition_from_row(row).create_kwargs == {}


def test_a_row_whose_secrets_will_not_decrypt_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller decides whether that is fatal or a row to skip; this only reports it."""
    row = _row(secrets={"api_key": "lakera-live"})
    monkeypatch.setenv("OTARI_SECRET_KEY", generate_secret_key())

    with pytest.raises(SecretDecryptionError):
        decrypt_create_secrets(row)


def test_a_config_entry_becomes_the_same_definition() -> None:
    """One shape reaches the runner, whichever source defined it."""
    definition = definition_from_config_entry(
        {
            "guardrail_name": _LAKERA,
            "create_kwargs": {"api_key": "from-yaml"},
            "validate_kwargs": {"payload": True},
        }
    )

    assert definition.guardrail_name == _LAKERA
    assert definition.create_kwargs == {"api_key": "from-yaml"}
    assert definition.validate_kwargs == {"payload": True}


def test_a_config_entry_may_omit_both_kwargs_maps() -> None:
    definition = definition_from_config_entry({"guardrail_name": "any_llm"})

    assert definition.create_kwargs == {}
    assert definition.validate_kwargs == {}


def test_config_guardrails_are_read_from_the_config_alone() -> None:
    """Hybrid has no database, so this must never need one."""
    config = GatewayConfig(
        master_key="k",
        guardrails={"prompt-injection": {"guardrail_name": _LAKERA, "create_kwargs": {"api_key": "k"}}},
    )

    assert set(config_file_guardrails(config)) == {"prompt-injection"}


def test_importing_the_store_loads_no_model_backend() -> None:
    """Reading the registry must stay as cheap as the catalog and runner keep it."""
    assert "torch" not in sys.modules
    assert "transformers" not in sys.modules


def test_a_full_replacement_recovers_a_row_whose_key_was_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending every secret's value must not first require reading the old ones.

    Decrypting before the split would refuse the one request that repairs a row
    after ``OTARI_SECRET_KEY`` was rotated without re-encrypting.
    """
    row = _row(secrets={"api_key": "lakera-live"})
    monkeypatch.setenv("OTARI_SECRET_KEY", generate_secret_key())

    plain, secrets = resolve_create_kwargs(_LAKERA, {"api_key": "lakera-replacement"}, stored={})

    assert secrets == {"api_key": "lakera-replacement"}
    assert plain == {}
    # The old ciphertext is still unreadable; the point is that it was not consulted.
    with pytest.raises(SecretDecryptionError):
        decrypt_create_secrets(row)
