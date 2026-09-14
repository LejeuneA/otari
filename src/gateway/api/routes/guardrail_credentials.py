"""Runtime guardrail management for the dashboard (``/api/v1/guardrail-credentials``).

A guardrail used to live in a YAML file inside a sidecar container, so adding one
meant editing a file on disk and restarting that container. These endpoints are
the route in that replaces it (otari#1108), and they are deliberately the same
shape as ``/api/v1/search-tools``: rows in ``guardrail_credentials``, secrets
encrypted at rest and never returned, sitting beside the config-file entries,
which stay honored and stay read-only here.

One thing differs from the search-tool sibling, and it follows from guardrails
not sharing a secret shape. A caller sends a single ``create_kwargs`` map mixing
plain arguments and credentials; the service splits it by the catalog's ``secret``
flag. So a read gives back ``create_kwargs`` as stored plus a ``create_secrets``
map of names to the redaction mask, never a value.

Operator-gated and standalone-only (the router is not mounted in hybrid, which
defines its guardrails in ``config.yml`` instead).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.api.deps import get_config, get_db, require_deployment_operator
from gateway.core.config import GatewayConfig
from gateway.log_config import logger
from gateway.models.entities import GuardrailCredential
from gateway.models.guardrails import GuardrailConfig
from gateway.services.guardrail_runner import get_guardrail_runner
from gateway.services.guardrail_store_service import (
    UNSET,
    GuardrailArgumentError,
    config_entry_is_enabled,
    config_file_guardrails,
    definition_from_row,
    delete_guardrail,
    get_guardrail,
    get_guardrail_for_update,
    list_guardrails,
    readable_secret_names,
    reencrypt_guardrails,
    save_guardrail,
)
from gateway.services.guardrails import GuardrailsNotReachableError
from gateway.services.secret_box import SecretBoxUnavailableError, SecretDecryptionError

router = APIRouter(
    prefix="/guardrail-credentials",
    tags=["guardrail-credentials"],
    dependencies=[Depends(require_deployment_operator)],
)

# Long enough for a realistic prompt, short enough that the check is a check and
# not a load test. The request path's own limits are the ones that matter in
# production; this endpoint only proves a definition works.
_MAX_TEST_INPUT = 20_000


class StoredGuardrailSchema(BaseModel):
    """A runtime-stored guardrail. Secrets are never returned, only their names."""

    name: str
    guardrail_name: str
    create_kwargs: dict[str, Any] = Field(
        default_factory=dict, description="Non-secret constructor arguments, as stored."
    )
    create_secrets: dict[str, str] = Field(
        default_factory=dict,
        description="Which constructor secrets are set, each masked. Empty when they cannot be decrypted.",
    )
    validate_kwargs: dict[str, Any] = Field(default_factory=dict, description="Arguments sent on every check.")
    enabled: bool = True
    created_at: str | None = None
    updated_at: str | None = None
    decryptable: bool = Field(
        default=True,
        description=(
            "False when the stored secrets cannot be read with the current OTARI_SECRET_KEY. "
            "Such a guardrail cannot run, so the dashboard flags it for the operator to fix."
        ),
    )
    shadows_config: bool = Field(
        default=False,
        description="True when a config-file guardrail of the same name exists; the stored one is in effect.",
    )

    @classmethod
    def from_model(cls, row: GuardrailCredential, *, shadows_config: bool = False) -> "StoredGuardrailSchema":
        secret_names = readable_secret_names(row)
        return cls(
            **row.to_public_dict(secret_names=secret_names or ()),
            decryptable=secret_names is not None,
            shadows_config=shadows_config,
        )


class ConfigGuardrailSchema(BaseModel):
    """A guardrail declared in the config file. Read-only: it cannot be edited here."""

    name: str
    guardrail_name: str
    enabled: bool
    shadowed: bool = Field(
        default=False,
        description="True when a stored guardrail of the same name overrides this entry.",
    )


class GuardrailCredentialsResponse(BaseModel):
    """Every guardrail a profile can name, by where it came from."""

    stored: list[StoredGuardrailSchema]
    config: list[ConfigGuardrailSchema]


class CreateGuardrailRequest(BaseModel):
    """Create a stored guardrail. Secrets in ``create_kwargs`` are write-only."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "prompt-injection",
                "guardrail_name": "lakera_guard",
                "create_kwargs": {"api_key": "lakera-live-..."},
            }
        }
    )

    name: str = Field(min_length=1, description="The name a guardrail entry puts in its 'profile' field.")
    guardrail_name: str = Field(
        description="The any-guardrail class, as listed by GET /api/v1/tool-settings/guardrails/catalog."
    )
    create_kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="Constructor arguments, secrets included. Secrets are stored encrypted and never returned.",
    )
    validate_kwargs: dict[str, Any] = Field(default_factory=dict, description="Arguments sent on every check.")
    enabled: bool = True


class UpdateGuardrailRequest(BaseModel):
    """Update a stored guardrail. Omitted fields are unchanged."""

    guardrail_name: str | None = None
    create_kwargs: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Replaces the stored arguments. Send a secret as '***' to keep it, a new value to rotate it, "
            "or leave it out to clear it."
        ),
    )
    validate_kwargs: dict[str, Any] | None = None
    enabled: bool | None = None
    expected_updated_at: str | None = Field(
        default=None,
        description="Optimistic concurrency: if set, the update 412s unless it matches the stored updated_at.",
    )


class TestGuardrailRequest(BaseModel):
    """Run a stored guardrail once against a sample input."""

    input_text: str = Field(min_length=1, max_length=_MAX_TEST_INPUT)
    validate_kwargs: dict[str, Any] = Field(
        default_factory=dict, description="Merged over the stored ones for this call only."
    )


class TestGuardrailResponse(BaseModel):
    """What one guardrail said about the sample input."""

    ok: bool = Field(description="Whether the guardrail ran at all. False means it could not be evaluated.")
    valid: bool | None = Field(
        default=None,
        description="True when the input passed, false when it was flagged, null when the verdict was inconclusive.",
    )
    explanation: str | None = None
    score: float | None = None
    error: str | None = Field(default=None, description="Why the guardrail could not run, when ok is false.")


class ReencryptGuardrailsResponse(BaseModel):
    """Result of re-encrypting stored guardrail secrets with the primary secret key."""

    reencrypted: int = Field(description="Number of stored guardrails whose secrets were re-encrypted.")
    unreadable: int = Field(description="Number left untouched because their secrets could not be decrypted.")


def _validate_name(name: str) -> None:
    """A guardrail name is a URL path segment here, so it constrains what it may be.

    ``min_length`` on the request model is not enough: it runs before the strip,
    so a name of only spaces clears it and then becomes empty. An empty name
    cannot be addressed by any later path, so the row could never be edited or
    deleted through this API again.
    """
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Guardrail name must not be blank.",
        )
    if "/" in name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Guardrail name '{name}' must not contain '/' (it is used as a URL path segment).",
        )


async def _commit(db: AsyncSession, *, conflict_detail: str | None = None) -> None:
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent create can slip past the pre-check and collide on the
        # primary key here; surface that as the intended 409, not a 500.
        await db.rollback()
        if conflict_detail is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=conflict_detail) from None
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database error") from None
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database error") from None


def _apply_write(name: str) -> None:
    """Make a committed guardrail change take effect on this worker.

    The runner caches what it builds, keyed on the arguments it was built with,
    so an edited guardrail would otherwise keep answering from the instance made
    out of its old ones. Evicting also releases a local model the old arguments
    had loaded, which nothing else would: the runner drops an entry only when no
    profile resolves to it (otari#1119).

    Sibling workers keep their own instance until they restart, the same
    cross-worker gap the provider overlay has. A definition is deployment
    config, so that is the expected shape rather than a surprise.
    """
    get_guardrail_runner().evict(name)


_UNDECRYPTABLE = (
    "its stored secrets cannot be decrypted with the current OTARI_SECRET_KEY. "
    "Send create_kwargs with every secret's value to replace them."
)


@router.get("")
async def list_all_guardrails(
    db: Annotated[AsyncSession, Depends(get_db)],
    config: Annotated[GatewayConfig, Depends(get_config)],
) -> GuardrailCredentialsResponse:
    """List every guardrail a 'profile' can name.

    ``stored`` are the editable rows written through this API; ``config`` are the
    config-file entries, which are still honored and are reported so the operator
    can see the whole set. Secrets are never returned, only their names.
    """
    from_config = config_file_guardrails(config)
    stored = await list_guardrails(db)
    stored_names = {row.name for row in stored}
    return GuardrailCredentialsResponse(
        stored=[StoredGuardrailSchema.from_model(row, shadows_config=row.name in from_config) for row in stored],
        config=[
            ConfigGuardrailSchema(
                name=name,
                guardrail_name=str(entry.get("guardrail_name") or ""),
                enabled=config_entry_is_enabled(entry),
                shadowed=name in stored_names,
            )
            for name, entry in sorted(from_config.items())
        ],
    )


@router.post("/reencrypt")
async def reencrypt_stored_guardrail_secrets(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ReencryptGuardrailsResponse:
    """Re-encrypt stored guardrail secrets with the primary OTARI_SECRET_KEY.

    The guardrail half of the key rotation procedure; run it alongside the
    provider and search-tool ones. Rows that cannot be decrypted are left
    untouched and must be recovered by replacing that guardrail's secrets.
    """
    try:
        rows = await list_guardrails(db)
        reencrypted, unreadable = await reencrypt_guardrails(db)
        await db.commit()
    except SecretBoxUnavailableError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database error") from None
    # The ciphertext changed but the plaintext did not, so nothing the runner
    # holds is stale. Evicted anyway: a row that was unreadable before is
    # readable now, and the instance built from the old arguments is the one
    # thing that would keep it looking broken.
    for row in rows:
        _apply_write(row.name)
    return ReencryptGuardrailsResponse(reencrypted=reencrypted, unreadable=unreadable)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_guardrail(
    request: CreateGuardrailRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    config: Annotated[GatewayConfig, Depends(get_config)],
) -> StoredGuardrailSchema:
    """Add a guardrail at runtime. Storing a secret requires OTARI_SECRET_KEY."""
    name = request.name.strip()
    _validate_name(name)
    conflict = f"A stored guardrail '{name}' already exists; use PATCH to update it."
    if await get_guardrail(db, name) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=conflict)
    try:
        row = await save_guardrail(
            db,
            name=name,
            guardrail_name=request.guardrail_name,
            create_kwargs=request.create_kwargs,
            validate_kwargs=request.validate_kwargs,
            enabled=request.enabled,
        )
    except GuardrailArgumentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except SecretBoxUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    await _commit(db, conflict_detail=conflict)
    shadows_config = name in config_file_guardrails(config)
    if shadows_config:
        logger.warning(
            "Stored guardrail '%s' shadows the config.yml guardrail of the same name; the stored entry now wins.",
            name,
        )
    _apply_write(name)
    await db.refresh(row)
    return StoredGuardrailSchema.from_model(row, shadows_config=shadows_config)


@router.patch("/{name}")
async def update_guardrail(
    name: str,
    request: UpdateGuardrailRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    config: Annotated[GatewayConfig, Depends(get_config)],
) -> StoredGuardrailSchema:
    """Update a stored guardrail. Omitted fields are left as-is.

    ``create_kwargs`` replaces the stored arguments rather than merging into
    them, so a secret is removed by leaving it out and kept by sending it back as
    ``***``. The row is locked ``FOR UPDATE`` so the ``expected_updated_at`` check
    and the write it guards are atomic.
    """
    existing = await get_guardrail_for_update(db, name)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No stored guardrail '{name}'.")
    if request.expected_updated_at is not None:
        current = existing.updated_at.isoformat() if existing.updated_at else None
        if current != request.expected_updated_at:
            raise HTTPException(
                status_code=status.HTTP_412_PRECONDITION_FAILED,
                detail="This guardrail was modified since you loaded it; reload and retry.",
            )

    # Distinguish "field omitted" (keep) from a value that was sent. An explicit
    # null is meaningless for the non-nullable columns, so it reads as unchanged
    # rather than being rejected.
    sent = request.model_fields_set
    submitted_create = request.create_kwargs if "create_kwargs" in sent and request.create_kwargs is not None else UNSET
    try:
        row = await save_guardrail(
            db,
            name=name,
            guardrail_name=request.guardrail_name if "guardrail_name" in sent and request.guardrail_name else UNSET,
            create_kwargs=submitted_create,
            validate_kwargs=request.validate_kwargs if "validate_kwargs" in sent else UNSET,
            enabled=request.enabled if "enabled" in sent and request.enabled is not None else UNSET,
        )
    except GuardrailArgumentError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except SecretBoxUnavailableError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except SecretDecryptionError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Guardrail '{name}' cannot be updated in place: {_UNDECRYPTABLE}",
        ) from None

    await _commit(db)
    _apply_write(name)
    await db.refresh(row)
    return StoredGuardrailSchema.from_model(row, shadows_config=name in config_file_guardrails(config))


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_stored_guardrail(
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    config: Annotated[GatewayConfig, Depends(get_config)],
) -> None:
    """Delete a stored guardrail. A config-file guardrail cannot be deleted here."""
    if not await delete_guardrail(db, name):
        detail = f"No stored guardrail '{name}'."
        if name in config_file_guardrails(config):
            detail = f"Guardrail '{name}' is defined in the config file and cannot be deleted through the API."
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    await _commit(db)
    _apply_write(name)


@router.post("/{name}/test")
async def test_stored_guardrail(
    name: str,
    request: TestGuardrailRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TestGuardrailResponse:
    """Run a stored guardrail once, so an operator sees it work before relying on it.

    A guardrail that cannot run answers ``ok: false`` with the reason rather than
    an error status: "it did not work, and here is why" is the result the form
    asked for. The reason is the runner's own message, which names types and
    argument names but never an argument's value, and this route is
    operator-only. A disabled guardrail is still testable, since checking one
    before turning it on is the point.
    """
    row = await get_guardrail(db, name)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No stored guardrail '{name}'.")

    try:
        definition = definition_from_row(row)
    except (SecretBoxUnavailableError, SecretDecryptionError):
        # A failure to run, like any other, rather than a 400: the question asked
        # was whether this guardrail works, and one shape of answer is easier to
        # act on than two.
        return TestGuardrailResponse(ok=False, error=f"Guardrail '{name}' cannot run: {_UNDECRYPTABLE}")

    cfg = GuardrailConfig(profile=name, mode="monitor", validate_kwargs=request.validate_kwargs)
    try:
        result = await get_guardrail_runner().run(definition=definition, cfg=cfg, input_text=request.input_text)
    except GuardrailsNotReachableError as exc:
        logger.info("Test of stored guardrail '%s' could not be evaluated", name)
        return TestGuardrailResponse(ok=False, error=str(exc))

    return TestGuardrailResponse(
        ok=True,
        valid=result.valid,
        explanation=str(result.explanation) if result.explanation is not None else None,
        score=float(result.score) if isinstance(result.score, (int, float)) else None,
    )
