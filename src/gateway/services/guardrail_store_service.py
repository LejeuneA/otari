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

The omission was the in-memory overlay, which otari#1113 filled: the request
path resolves a profile synchronously, so it is served from a cache loaded at
startup and refreshed on a TTL, never a query per request.

Where that overlay differs from its two siblings is that it does not rewrite
``config.guardrails``. They must, because many call sites read
``config.providers`` and ``config.search_tools`` directly; here
:func:`local_guardrail_definition` is the only reader, so the merge lives there
and the config map stays the operator's file as written. That keeps
:func:`config_file_guardrails` honest for the four route call sites that ask
whether a name is the file's, and keeps decrypted vendor keys off a long-lived
config object.

Encryption happens here and nowhere above: a route passes plaintext in and gets
a masked row back. Standalone mode only for the stored half; the config half
loads from ``GatewayConfig`` alone and is the only definition source a hybrid
gateway has.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.config import GatewayConfig, validate_guardrail_create_kwargs
from gateway.core.database import create_session
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

    A genuine pass-through, unlike its provider and search-tool siblings, which
    have to strip an overlay their store wrote back over the config map. This
    store deliberately writes nothing there (otari#1113 settled that), so
    ``config.guardrails`` is always the operator's file as loaded, and the
    callers that ask "is this name the operator's file's?" get a true answer
    with no baseline to keep.
    """
    return config.guardrails


def config_entry_is_enabled(entry: dict[str, Any]) -> bool:
    """Whether a ``guardrails:`` entry is on. Absent means on.

    One place decides what an absent ``enabled`` means, which is why the loader
    leaves the key out rather than filling it in.
    """
    return entry.get("enabled", True) is not False


# --------------------------------------------------------------------------- #
# The overlay
# --------------------------------------------------------------------------- #

# How long a worker may serve a stale guardrail overlay before refreshing. A
# definition added or edited anywhere reaches every replica within this.
GUARDRAIL_CACHE_TTL_SECONDS: Final = 30.0

# profile name -> what to run, or None when the row owns the name but cannot run:
# it is switched off, or its secrets no longer decrypt. The distinction that
# matters is membership, not the value: a name in here belongs to the store and
# never falls through to `config.guardrails`.
_cache: dict[str, GuardrailDefinition | None] = {}
_cached_at: float | None = None


def cached_guardrails() -> dict[str, GuardrailDefinition | None]:
    """The stored overlay this worker last loaded. A copy, so a caller cannot edit it."""
    return dict(_cache)


def cache_is_stale(ttl: float = GUARDRAIL_CACHE_TTL_SECONDS) -> bool:
    """Whether the cache has never been loaded or has outlived ``ttl``.

    For the refresher and for tests. Deliberately not consulted by
    :func:`local_guardrail_definition`; see its docstring.
    """
    return _cached_at is None or (time.monotonic() - _cached_at) >= ttl


def reset_guardrail_cache() -> None:
    """Drop the overlay so the next load starts clean (startup, shutdown, tests)."""
    global _cached_at  # noqa: PLW0603

    _cache.clear()
    _cached_at = None


def _cache_value(row: GuardrailCredential) -> GuardrailDefinition | None:
    """What the overlay holds for one row: a definition, or ``None`` to shadow.

    A disabled row and a row whose secrets will not decrypt are the same answer
    here, and it is not the answer the provider overlay gives. That one omits an
    undecryptable row, which is right for a table with no ``enabled`` column:
    there is no "present but off" state to confuse it with. Here omitting it
    would let a ``guardrails:`` entry of the same name take over silently, while
    the management API keeps reporting the row as shadowing and
    ``decryptable: false``. The request path agrees with the listing instead.
    """
    if not row.enabled:
        return None
    try:
        return definition_from_row(row)
    except (SecretBoxUnavailableError, SecretDecryptionError):
        logger.warning(
            "Stored guardrail '%s' cannot run: its secrets could not be decrypted "
            "(check OTARI_SECRET_KEY). It still shadows a config guardrail of the same name.",
            row.name,
        )
        return None


def local_guardrail_definition(config: GatewayConfig, profile: str) -> GuardrailDefinition | None:
    """What this gateway would run for ``profile``, or ``None`` if it defines none.

    The request path's only read, and the reason the overlay exists. Synchronous
    and side-effect free: it reads this module's cache and ``config.guardrails``,
    and that is all. It must stay that way. A hybrid gateway never loads the
    cache and has no database to load it from, so an empty cache means "no stored
    definitions" rather than "not loaded yet"; a lazy load added here would ask
    for a session that mode never opened. For the same reason it never consults
    :func:`cache_is_stale` — converging is the refresher's job.

    Membership in the cache, not the value, is what decides whose name it is. A
    row that is present but unrunnable still shadows the config entry below it.
    """
    if profile in _cache:
        return _cache[profile]
    entry = config.guardrails.get(profile)
    if entry is None or not config_entry_is_enabled(entry):
        return None
    return definition_from_config_entry(entry)


async def refresh_guardrail_cache(db: AsyncSession) -> None:
    """Reload the overlay from the database."""
    global _cached_at  # noqa: PLW0603

    rows = (await db.execute(select(GuardrailCredential))).scalars().all()
    overlay = {row.name: _cache_value(row) for row in rows}
    _cache.clear()
    _cache.update(overlay)
    _cached_at = time.monotonic()


async def load_guardrails_at_startup(db: AsyncSession, config: GatewayConfig) -> None:
    """Prime the overlay so the first request does not race the first refresh.

    A failure here is logged rather than raised: stored guardrails are an
    addition to the config-file ones, and a gateway that serves every config
    guardrail is better than one that refuses to start.
    """
    reset_guardrail_cache()
    try:
        await refresh_guardrail_cache(db)
    except Exception:
        logger.exception("Failed to load stored guardrails; continuing with config guardrails only")
        return
    if _cache:
        logger.info("Loaded %d stored guardrail(s)", len(_cache))
    for name in sorted(set(config.guardrails) & set(_cache)):
        logger.warning(
            "Stored guardrail '%s' shadows the config.yml guardrail of the same name; "
            "the stored definition is in effect%s.",
            name,
            "" if _cache[name] is not None else ", and it is switched off or unreadable, so neither runs",
        )


async def run_guardrail_refresher(interval: float = GUARDRAIL_CACHE_TTL_SECONDS) -> None:
    """Reload the overlay forever so other writers' changes arrive.

    A write refreshes the worker that served it; this covers sibling workers and
    other replicas, which converge within ``interval``. Every error is swallowed
    and retried on the next tick so a database blip cannot freeze the overlay.
    Cancelled at shutdown.

    Takes no config, unlike its provider and search-tool siblings: there is no
    config map to rebuild, only a cache to replace.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            async with create_session() as db:
                await refresh_guardrail_cache(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Stored guardrail refresh failed; retrying in %ss", interval, exc_info=True)


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
