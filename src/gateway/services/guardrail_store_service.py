"""Guardrail definitions: dashboard-configured guardrails, beside the config-file ones.

A guardrail used to be a key in a YAML file inside a sidecar container, so adding
one meant editing a file on disk and restarting that container (otari#1108). A
definition can now come from two places instead: ``config.yml`` ``guardrails:``
entries, immutable at runtime and validated at startup, and
``guardrail_credentials`` rows written through the dashboard. Both mean the same
thing to a request, and a stored row wins on a name collision, exactly as the
provider and search-tool stores already arrange.

The store's own shape is `search_tool_store_service`'s, with one difference and
one omission.

The difference is the secrets. The 40 guardrails do not share a secret shape:
``bedrock_guardrails`` takes three, ``lakera_guard`` one, ``any_llm`` none, and
one added upstream tomorrow may need four. So a submitted ``create_kwargs`` map
is split by the catalog's ``secret`` flag rather than by a column per secret; the
plain half is stored as JSON and every secret goes into one map encrypted as a
single string. What reaches the runner is the two halves merged back together,
so the split is storage and never semantics.

The omission is the in-memory overlay. Nothing on the request path reads a
definition yet, so there is no synchronous read to serve from a cache, and the
overlay plus its TTL refresher arrive with the request path in otari#1113. What
this module gives that work is :func:`definition_from_row` and
:func:`definition_from_config_entry`, which are what such a cache would hold.

Encryption happens here and nowhere above: a route passes plaintext in and gets
a masked row back. Standalone mode only for the stored half; the config half
loads from ``GatewayConfig`` alone and is the only definition source a hybrid
gateway has.
"""

from __future__ import annotations

import json
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.config import GatewayConfig, validate_guardrail_create_kwargs
from gateway.log_config import logger
from gateway.models.entities import GuardrailCredential
from gateway.models.secret_fields import REDACTED_VALUE, restore_redacted_values
from gateway.services.guardrail_catalog import _specs_for_stage
from gateway.services.guardrail_runner import GuardrailDefinition
from gateway.services.secret_box import (
    SecretBoxUnavailableError,
    SecretDecryptionError,
    decrypt_secret,
    encrypt_secret,
)


class _Unset:
    """Sentinel type: 'this field was not provided', distinct from an explicit None."""


# A field left at UNSET keeps its stored value; passing a value sets it. Lets a
# PATCH rotate a secret without restating the rest of the definition.
UNSET: Final = _Unset()


class GuardrailArgumentError(ValueError):
    """A submitted definition is one no guardrail could be built from.

    Separate from the ``ValueError`` the config validator raises, so the route
    can map exactly this to a 400 without also catching an unrelated one. Its
    message names argument names and never their values.
    """


def _secret_names(guardrail_name: str) -> set[str]:
    """The constructor arguments this guardrail treats as credentials.

    Read through the catalog rather than the registry directly, so the
    degrade-to-json rule that decides which secrets can be stored at all is
    stated once and both the form and the store see the same answer.
    """
    return {spec.name for spec in _specs_for_stage(_guardrail_name(guardrail_name), "create") if spec.secret}


def _guardrail_name(guardrail_name: str) -> Any:
    """``guardrail_name`` as the enum member the registry is keyed by."""
    from any_guardrail.base import GuardrailName

    try:
        return GuardrailName(guardrail_name)
    except ValueError as exc:
        msg = (
            f"'{guardrail_name}' is not a guardrail this gateway ships. "
            "GET /api/v1/tool-settings/guardrails/catalog lists them."
        )
        raise GuardrailArgumentError(msg) from exc


def resolve_create_kwargs(
    guardrail_name: str,
    submitted: dict[str, Any],
    *,
    stored: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a submitted constructor map against what is stored, then split it.

    ``stored`` is the previous arguments with their secrets already decrypted, so
    a value resubmitted as the redaction mask keeps what is there. An editor is
    shown ``***`` for every secret and sends the whole object back, so without
    this a round trip through the form would overwrite the key with three
    asterisks.

    The map is replaced rather than merged, which is what makes a secret
    removable: omitting one drops it. A mask with nothing behind it is refused
    rather than stored, since that is the one case where the caller believes they
    are keeping a secret that does not exist.

    Returns ``(plain, secrets)``. Raises :class:`GuardrailArgumentError` for
    anything no guardrail could be built from.
    """
    resolved = restore_redacted_values(submitted, stored) or {}
    still_masked = sorted(key for key, value in resolved.items() if value == REDACTED_VALUE)
    if still_masked:
        msg = (
            f"create_kwargs.{still_masked[0]} was sent as the redaction mask, but nothing is stored under "
            "that name. Send the value itself, or leave the argument out."
        )
        raise GuardrailArgumentError(msg)

    try:
        validate_guardrail_create_kwargs(guardrail_name, resolved, "guardrail")
    except ValueError as exc:
        raise GuardrailArgumentError(str(exc)) from None

    secret_names = _secret_names(guardrail_name)
    plain = {key: value for key, value in resolved.items() if key not in secret_names}
    secrets = {key: value for key, value in resolved.items() if key in secret_names}
    return plain, secrets


def _carries_a_mask(create_kwargs: dict[str, Any]) -> bool:
    """Whether any submitted value is the redaction mask, meaning "keep the stored one"."""
    return any(value == REDACTED_VALUE for value in create_kwargs.values())


def decrypt_create_secrets(row: GuardrailCredential) -> dict[str, Any]:
    """The row's secret constructor arguments, in clear.

    Empty when the guardrail stores none, which needs no key at all. Raises
    ``SecretBoxUnavailableError`` / ``SecretDecryptionError`` otherwise; the
    caller decides whether that skips the row or fails the request.
    """
    if not row.encrypted_create_secrets:
        return {}
    decoded = json.loads(decrypt_secret(row.encrypted_create_secrets))
    return dict(decoded) if isinstance(decoded, dict) else {}


def definition_from_row(row: GuardrailCredential) -> GuardrailDefinition:
    """The runner's view of a stored guardrail, with its secrets merged back in."""
    return GuardrailDefinition(
        guardrail_name=row.guardrail_name,
        create_kwargs={**dict(row.create_kwargs or {}), **decrypt_create_secrets(row)},
        validate_kwargs=dict(row.validate_kwargs or {}),
    )


def definition_from_config_entry(entry: dict[str, Any]) -> GuardrailDefinition:
    """The runner's view of a ``guardrails:`` entry.

    Not split, because a config entry is read-only and its secrets are already in
    the operator's own file. The entry was validated at load.
    """
    return GuardrailDefinition(
        guardrail_name=str(entry["guardrail_name"]),
        create_kwargs=dict(entry.get("create_kwargs") or {}),
        validate_kwargs=dict(entry.get("validate_kwargs") or {}),
    )


def config_file_guardrails(config: GatewayConfig) -> dict[str, dict[str, Any]]:
    """The config-file guardrails.

    A pass-through today, unlike its provider and search-tool siblings, because
    nothing overlays stored rows onto ``config.guardrails``. It exists so the
    callers that ask "is this name the operator's file's?" do not have to change
    when otari#1113 decides whether an overlay belongs here.
    """
    return config.guardrails


def config_entry_is_enabled(entry: dict[str, Any]) -> bool:
    """Whether a ``guardrails:`` entry is on. Absent means on.

    One place decides what an absent ``enabled`` means, which is why the loader
    leaves the key out rather than filling it in.
    """
    return entry.get("enabled", True) is not False


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #


async def list_guardrails(db: AsyncSession) -> list[GuardrailCredential]:
    """Every stored guardrail, ordered by name."""
    rows = (await db.execute(select(GuardrailCredential).order_by(GuardrailCredential.name))).scalars().all()
    return list(rows)


async def get_guardrail(db: AsyncSession, name: str) -> GuardrailCredential | None:
    """The stored guardrail called ``name``, or ``None``."""
    return await db.get(GuardrailCredential, name)


async def get_guardrail_for_update(db: AsyncSession, name: str) -> GuardrailCredential | None:
    """Like :func:`get_guardrail`, but locks the row ``FOR UPDATE``.

    Used by the PATCH path so a version check and the write it guards run under
    the same row lock, exactly as the provider-credential path does.
    """
    stmt = select(GuardrailCredential).where(GuardrailCredential.name == name).with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def save_guardrail(
    db: AsyncSession,
    *,
    name: str,
    guardrail_name: str | _Unset = UNSET,
    create_kwargs: dict[str, Any] | _Unset = UNSET,
    validate_kwargs: dict[str, Any] | None | _Unset = UNSET,
    enabled: bool | _Unset = UNSET,
) -> GuardrailCredential:
    """Create or update a stored guardrail (staged; caller commits).

    Each field is tri-state: left at ``UNSET`` it keeps its stored value. The
    constructor arguments are re-resolved whenever either they or the guardrail
    class is sent, so the plain/secret split can never be left describing the
    wrong class: changing the class alone re-splits the stored arguments under
    the new one, which is what turns an argument that was a secret there and is
    not here into a plain one rather than an unreadable leftover.

    Storing a secret requires ``OTARI_SECRET_KEY``
    (``SecretBoxUnavailableError``). No plaintext is logged.
    """
    existing = await db.get(GuardrailCredential, name)
    if existing is None:
        # Both columns are non-null, so a create must supply a class; the route
        # validates that before staging.
        row = GuardrailCredential(name=name, guardrail_name="", create_kwargs={}, validate_kwargs={})
        db.add(row)
    else:
        row = existing

    effective_name = guardrail_name if not isinstance(guardrail_name, _Unset) else row.guardrail_name
    if not isinstance(guardrail_name, _Unset):
        row.guardrail_name = guardrail_name

    if not isinstance(create_kwargs, _Unset) or not isinstance(guardrail_name, _Unset):
        # Only decrypt when the answer is actually needed. A caller who sends
        # every secret's value is replacing them all, and that is the one way to
        # recover a guardrail whose key was lost; decrypting first would refuse
        # the request that fixes it. Omitting create_kwargs, or masking any part
        # of it, does need what is stored.
        needs_stored = isinstance(create_kwargs, _Unset) or _carries_a_mask(create_kwargs)
        stored = {**dict(row.create_kwargs or {}), **decrypt_create_secrets(row)} if existing and needs_stored else {}
        submitted = create_kwargs if not isinstance(create_kwargs, _Unset) else stored
        plain, secrets = resolve_create_kwargs(effective_name, dict(submitted), stored=stored)
        row.create_kwargs = plain
        row.encrypted_create_secrets = (
            encrypt_secret(json.dumps(secrets, sort_keys=True)) if secrets else None
        )

    if not isinstance(validate_kwargs, _Unset):
        # Masked on the way out by key name, so an editor resubmitting the whole
        # object sends the mask for entries it never saw; those keep what is
        # stored. The same rule ``provider_store_service`` applies to client_args.
        stored_validate = existing.validate_kwargs if existing else None
        row.validate_kwargs = restore_redacted_values(validate_kwargs, stored_validate) or {}
    if not isinstance(enabled, _Unset):
        row.enabled = enabled

    return row


async def reencrypt_guardrails(db: AsyncSession) -> tuple[int, int]:
    """Re-encrypt stored guardrail secrets with the current primary OTARI_SECRET_KEY.

    Returns ``(reencrypted, unreadable)``. Rows holding no secret are ignored. A
    map that cannot be decrypted with the configured key set is left untouched
    and counted, so the operator can recover it by replacing that guardrail's
    secrets rather than losing the rest of its definition.
    """
    rows = (
        (
            await db.execute(
                select(GuardrailCredential).where(GuardrailCredential.encrypted_create_secrets.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    reencrypted = 0
    unreadable = 0
    for row in rows:
        if row.encrypted_create_secrets is None:
            continue
        try:
            plaintext = decrypt_secret(row.encrypted_create_secrets)
        except SecretDecryptionError:
            unreadable += 1
            continue
        row.encrypted_create_secrets = encrypt_secret(plaintext)
        reencrypted += 1
    return reencrypted, unreadable


async def delete_guardrail(db: AsyncSession, name: str) -> bool:
    """Delete a stored guardrail (staged; caller commits). Returns whether it existed."""
    row = await db.get(GuardrailCredential, name)
    if row is None:
        return False
    await db.delete(row)
    return True


def readable_secret_names(row: GuardrailCredential) -> set[str] | None:
    """Which secrets a row holds, or ``None`` when they cannot be read.

    What a listing needs: the names go into the masked response and ``None``
    becomes the ``decryptable: false`` flag the dashboard shows, so an operator
    sees a row whose key no longer decrypts rather than a row that looks empty.
    """
    try:
        return set(decrypt_create_secrets(row))
    except (SecretBoxUnavailableError, SecretDecryptionError):
        logger.warning(
            "Stored guardrail '%s': its secrets could not be decrypted (check OTARI_SECRET_KEY).",
            row.name,
        )
        return None
