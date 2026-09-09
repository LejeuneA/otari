"""Connections: OAuth grants an application's users give this deployment for third-party apps.

The app-level half of what Octonous's ``OAuthService`` does, brought to otari
so an application built on the gateway can let *its* users connect Slack,
GitHub or Google, and so a gateway-run tool, an MCP server or an overlay can
then act on those accounts with a credential the gateway holds. The user is
whoever the application says it is: the ``user`` string on its requests,
scoped to the workspace its API key belongs to (:class:`EndUser`). The protocol
mechanics (authorization URL, PKCE, the code exchange, refresh, revocation,
identity fetch) come from apron-auth and its per-provider presets; what lives
here is what a deployment owns: which apps are configured, where the browser
is sent back to, the pending state between the two halves of a flow, and the
encrypted rows the tokens end up in.

Security posture, carried over from Octonous's ``OAUTH_FLOW.md``:

- PKCE on by default (the presets decide per provider), with the verifier
  stored encrypted alongside the state and never sent to the browser.
- The callback is hosted here and trusts only the ``state``: it names the end
  user, the provider and where to send the browser afterwards, all recorded
  when the application (not the browser) started the flow. It is consumed
  atomically so a code can be exchanged once.
- Tokens are Fernet-encrypted with ``OTARI_SECRET_KEY`` at rest and never
  serialized; :meth:`ConnectedAccountService.access_token` is the one way out
  and is for in-process callers.
- The redirect URI is derived from ``public_base_url`` and never taken from a
  request, so a browser cannot choose where a provider sends a code.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit

from apron_auth import OAuthClient
from apron_auth.errors import OAuthError
from apron_auth.models import OAuthPendingState, ProviderConfig, TokenSet
from apron_auth.protocols import RevocationHandler
from apron_auth.providers import atlassian as apron_atlassian
from apron_auth.providers import github as apron_github
from apron_auth.providers import google as apron_google
from apron_auth.providers import hubspot as apron_hubspot
from apron_auth.providers import linear as apron_linear
from apron_auth.providers import microsoft as apron_microsoft
from apron_auth.providers import notion as apron_notion
from apron_auth.providers import salesforce as apron_salesforce
from apron_auth.providers import slack as apron_slack
from apron_auth.providers import typeform as apron_typeform
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.config import CONNECTED_APP_PROVIDERS, GatewayConfig
from gateway.log_config import logger
from gateway.models.entities import ConnectedAccount, ConnectedAccountOAuthState, EndUser
from gateway.services.secret_box import (
    SecretBoxUnavailableError,
    SecretDecryptionError,
    decrypt_secret,
    decrypt_under,
    encrypt_secret,
    encrypt_under,
    generate_data_key,
)
from gateway.services.tenancy.errors import (
    ConnectedAccountBindingError,
    ConnectedAccountExchangeError,
    ConnectedAccountFlowNotFoundError,
    ConnectedAccountLimitReachedError,
    ConnectedAccountNotFoundError,
    ConnectedAccountReturnUrlError,
    ConnectedAccountStateInvalidError,
    ConnectedAppNotConfiguredError,
    SecretBoxUnavailableTenancyError,
)

#: A pending flow is good for this long. OAuth guidance says under ten minutes;
#: Octonous used exactly ten.
OAUTH_STATE_TTL = timedelta(minutes=10)
#: Refresh a token this close to expiry rather than hand out one about to die.
REFRESH_LEEWAY = timedelta(seconds=60)
#: Ceiling on accounts per end user, a sanity bound rather than a product limit.
MAX_CONNECTED_ACCOUNTS_PER_USER = 50
#: The ``user`` field on a request, and the external id here, share this bound.
MAX_EXTERNAL_USER_ID = 255
#: An application's own partition of a provider (``key``), long enough for a
#: readable name like ``google:drive`` and short enough to index.
MAX_CONNECTION_KEY = 64
#: A resolved flow is answerable this long after it started, so an application
#: that polls slowly (or reloads its page) still learns the outcome. Longer
#: than ``OAUTH_STATE_TTL`` on purpose: the row stays queryable well after it
#: has stopped being usable, since consumption and expiry are what make it
#: unusable, not the sweep.
FLOW_RETENTION = timedelta(hours=1)

_PRESETS: dict[str, Any] = {
    "atlassian": apron_atlassian.preset,
    "github": apron_github.preset,
    "google": apron_google.preset,
    "hubspot": apron_hubspot.preset,
    "linear": apron_linear.preset,
    "microsoft": apron_microsoft.preset,
    "notion": apron_notion.preset,
    "salesforce": apron_salesforce.preset,
    "slack": apron_slack.preset,
    "typeform": apron_typeform.preset,
}
assert set(_PRESETS) == set(CONNECTED_APP_PROVIDERS), "CONNECTED_APP_PROVIDERS and _PRESETS must name the same apps"

_PROVIDER_LABELS = {
    "atlassian": "Atlassian",
    "github": "GitHub",
    "google": "Google",
    "hubspot": "HubSpot",
    "linear": "Linear",
    "microsoft": "Microsoft",
    "notion": "Notion",
    "salesforce": "Salesforce",
    "slack": "Slack",
    "typeform": "Typeform",
}


# ---------------------------------------------------------------------------
# Public shapes
# ---------------------------------------------------------------------------


class ScopePublic(BaseModel):
    scope: str
    label: str
    description: str
    access_type: str
    required: bool


class ConnectedAppPublic(BaseModel):
    """One app an end user may connect."""

    provider: str
    label: str
    scopes: list[str] = Field(description="The scopes this deployment asks for by default.")
    scope_details: list[ScopePublic] = Field(description="Labels and descriptions for the scopes the preset documents.")
    connected_accounts: int = Field(description="How many accounts the named user has connected for this app.")


class ConnectedAccountPublic(BaseModel):
    """A connected account without its tokens."""

    id: uuid.UUID
    user: str | None = Field(
        description="The application's id for the user who owns it; null for a connection shared by the workspace."
    )
    owner: Literal["user", "workspace"] = Field(description="Whose credential this is.")
    connected_by: str | None = Field(
        default=None, description="Who consented, for a shared connection: the application's id for that user."
    )
    provider: str
    key: str = Field(description="The application's partition of this provider, empty when it makes no distinction.")
    account_identifier: str | None
    account_label: str | None = Field(description="What the provider calls the account: a name, an email, a workspace.")
    label: str | None = Field(description="The user's own label, when set.")
    status: Literal["active", "needs_reauth"] = Field(
        description="'needs_reauth' once the provider has rejected the grant: ask the user to connect again."
    )
    invalid_reason: str | None = Field(default=None, description="Why it needs reconnecting, when it does.")
    scopes: list[str]
    extra_scopes: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Scopes of the secondary tokens, keyed as they are on the credential (Slack: 'user').",
    )
    account_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="What the provider says about the account: subject, email, name, workspace id and name.",
    )
    expires_at: datetime | None
    has_refresh_token: bool
    created_at: datetime
    updated_at: datetime


class ConnectedAccountsPublic(BaseModel):
    data: list[ConnectedAccountPublic]
    count: int


class AuthorizeRequest(BaseModel):
    user: str = Field(min_length=1, max_length=MAX_EXTERNAL_USER_ID, description="Your application's id for the user.")
    key: str = Field(
        default="",
        max_length=MAX_CONNECTION_KEY,
        description=(
            "Your own partition of this provider, when one connection per provider is not enough: an application "
            "that shows Gmail and Google Drive as separate things connects Google twice, under keys of its "
            "choosing, each with its own scopes, account and disconnect. Leave empty when the provider is the "
            "whole story."
        ),
    )
    scopes: list[str] | None = Field(
        default=None,
        max_length=64,
        description="Scopes to ask for instead of the deployment's defaults for this app.",
    )
    scope_mode: Literal["exact", "add"] = Field(
        default="exact",
        description=(
            "How 'scopes' combines with what the user already granted here. 'exact' asks for exactly that list; "
            "'add' asks for it together with the scopes the existing connection holds, which is what a scope "
            "upgrade wants — asking for only the new ones drops the rest at most providers."
        ),
    )
    expected_account_identifier: str | None = Field(
        default=None,
        max_length=320,
        description=(
            "Refuse the grant unless the provider names this account. Pass the 'account_identifier' of the "
            "connection being re-authorized so a user who picks a different account on the consent screen is "
            "told, instead of quietly ending up with two."
        ),
    )
    shared: bool = Field(
        default=False,
        description=(
            "Store the grant for the whole workspace instead of this user: every user of your application in "
            "this workspace then resolves it, and the token endpoint falls back to it when a user has none of "
            "their own. Consent still comes from this user, and they are recorded as having given it."
        ),
    )
    return_url: str | None = Field(
        default=None,
        max_length=2048,
        description=(
            "Where to send the browser once the account is connected (or the attempt failed). "
            "https, or http on localhost. Otari appends connection=ok&provider=...&connection_id=..., "
            "or connection=error&provider=...&reason=.... Defaults to a page on this deployment."
        ),
    )


class AuthorizePublic(BaseModel):
    authorization_url: str = Field(description="Send the user's browser here; Otari handles the rest of the flow.")
    flow_id: uuid.UUID = Field(
        description=(
            "This flow, for GET /v1/connections/flows/{flow_id}: how it ended, once it has. Safe to hold and to "
            "put in your own page's URL — it is not the OAuth state and cannot be used to complete the flow."
        )
    )
    expires_at: datetime = Field(description="When the link stops working; start again after that.")


class FlowPublic(BaseModel):
    """How one connect flow is going, for an application that opened a popup and is waiting."""

    flow_id: uuid.UUID
    provider: str
    key: str
    user: str
    status: Literal["pending", "connected", "failed", "expired"] = Field(
        description=(
            "'pending' while the user is still at the provider, 'connected' with a connection_id once stored, "
            "'failed' with a reason, 'expired' if the link timed out unused."
        )
    )
    connection_id: uuid.UUID | None = Field(default=None, description="The connection, once there is one.")
    reason: str | None = Field(default=None, description="Why it failed, when it did.")
    expires_at: datetime = Field(description="When the link stops working.")


class ConnectedAccountUpdate(BaseModel):
    label: Annotated[str | None, Field(max_length=64)] = None


class RejectedReport(BaseModel):
    """Why a consumer believes a credential is dead, for the record on the connection."""

    reason: Annotated[str | None, Field(max_length=200)] = Field(
        default=None, description="What the provider said, e.g. 'slack: token_revoked'. Shown as invalid_reason."
    )


class AccessToken(BaseModel):
    """A live credential, with enough about the account to act on it.

    Everything a consumer needs to build a provider client in one answer: the
    token, the secondary tokens next to it, which scopes each of them holds,
    and who the account is. A Slack consumer needs exactly this — a bot token,
    a user token, the two scope sets apart, and the workspace the pair belongs
    to (``account_metadata['tenancy_id']`` and ``['tenancy_name']``).
    """

    connection_id: uuid.UUID
    provider: str
    key: str
    token: str
    token_type: str
    expires_at: datetime | None
    scopes: list[str] = Field(default_factory=list, description="Scopes the primary token holds.")
    extra: dict[str, str] = Field(
        default_factory=dict, description="Secondary tokens, e.g. Slack's user token under 'user'."
    )
    extra_scopes: dict[str, list[str]] = Field(
        default_factory=dict, description="Scopes of those secondary tokens, keyed the same way."
    )
    account_identifier: str | None = None
    account_label: str | None = None
    account_metadata: dict[str, Any] = Field(
        default_factory=dict, description="Who the provider says the account is; a Slack workspace id lives here."
    )


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------


def redirect_uri(config: GatewayConfig, provider: str) -> str:
    """Where the provider sends the browser back to; derived, never supplied.

    A plain path with no fragment, which ``gateway.main`` turns into the
    dashboard hash route that finishes the flow, for the same RFC 6749 reason
    ``oauth_service.redirect_uri`` gives.
    """
    base = (config.public_base_url or "").rstrip("/")
    return f"{base}/connected-accounts/{provider}/callback"


def default_return_url(config: GatewayConfig) -> str:
    """Where a flow started without a ``return_url`` lands: a page on this deployment."""
    base = (config.public_base_url or "").rstrip("/")
    return f"{base}/#/connections"


def validate_return_url(url: str) -> None:
    """Refuse a ``return_url`` a browser could be sent to unsafely.

    Supplied by the authenticated application, not the browser, so this is
    the same trust as a redirect URI in config. Still: https only, except
    http on localhost for development, and no fragment (it would swallow the
    query Otari appends).

    Raises:
        ConnectedAccountReturnUrlError: If the URL is not acceptable.

    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host:
        ok = True
    elif parts.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}:
        ok = True
    else:
        ok = False
    if not ok or parts.fragment:
        raise ConnectedAccountReturnUrlError(url)


def with_query(url: str, **params: str) -> str:
    """``url`` with ``params`` appended as query parameters.

    For a hash-routed page (``…/#/connections``, the default return URL) the
    parameters go inside the fragment, which is the only part a hash router
    reads; a ``return_url`` an application supplies may carry no fragment
    (:func:`validate_return_url`), so its parameters go in the real query.
    """
    query = urlencode({key: value for key, value in params.items() if value})
    if not query:
        return url
    parts = urlsplit(url)
    if parts.fragment:
        separator = "&" if "?" in parts.fragment else "?"
        return f"{url}{separator}{query}"
    return f"{url}&{query}" if parts.query else f"{url}?{query}"


def provider_config(config: GatewayConfig, provider: str, scopes: list[str] | None = None) -> ProviderConfig:
    """apron-auth's ``ProviderConfig`` for ``provider`` on this deployment.

    Raises:
        ConnectedAppNotConfiguredError: For an unknown app, one without client
            credentials, or a deployment with no ``public_base_url``.

    """
    entry = config.connected_app(provider)
    if entry is None or provider not in _PRESETS:
        raise ConnectedAppNotConfiguredError(provider)
    kwargs: dict[str, Any] = {
        "client_id": entry["client_id"],
        "client_secret": entry["client_secret"],
        "scopes": list(scopes if scopes is not None else entry.get("scopes") or []),
        "redirect_uri": redirect_uri(config, provider),
    }
    if provider == "slack":
        kwargs["user_scopes"] = list(entry.get("user_scopes") or [])
    built, _revocation = _PRESETS[provider](**kwargs)
    return built  # type: ignore[no-any-return]


def _revocation_handler(config: GatewayConfig, provider: str) -> RevocationHandler | None:
    entry = config.connected_app(provider)
    if entry is None:
        return None
    kwargs: dict[str, Any] = {"client_id": entry["client_id"], "client_secret": entry["client_secret"], "scopes": []}
    if provider == "slack":
        kwargs["user_scopes"] = []
    _built, revocation = _PRESETS[provider](**kwargs)
    return revocation  # type: ignore[no-any-return]


class DatabaseStateStore:
    """apron-auth's ``StateStore`` over ``connected_account_oauth_states``.

    Bound to the end user the flow is for and the provider it is with, both
    recorded when the application started it. ``consume`` refuses a state that
    does not match, and reports it the same way as an unknown one so a stolen
    state cannot be told apart from a stale one.
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        end_user_id: uuid.UUID,
        provider: str,
        return_url: str | None = None,
        flow_id: uuid.UUID | None = None,
        connection_key: str = "",
        shared: bool = False,
        expected_account_identifier: str | None = None,
    ) -> None:
        self._db = db
        self._end_user_id = end_user_id
        self._provider = provider
        self._return_url = return_url
        #: Minted by the caller so it can be answered to the application
        #: before the browser has been anywhere.
        self.flow_id = flow_id or uuid.uuid4()
        self._connection_key = connection_key
        self._shared = shared
        self._expected_account_identifier = expected_account_identifier

    async def save(self, state: OAuthPendingState) -> None:
        now = datetime.now(UTC)
        # Opportunistic sweep, so the table does not grow with every abandoned
        # consent screen. Rows are kept past their usable life (FLOW_RETENTION)
        # so a slow poller still learns how its flow ended; being unexpired is
        # checked on use, not here.
        await self._db.execute(
            delete(ConnectedAccountOAuthState).where(ConnectedAccountOAuthState.created_at < now - FLOW_RETENTION)
        )
        try:
            verifier = encrypt_secret(state.code_verifier) if state.code_verifier else None
        except SecretBoxUnavailableError as error:
            raise SecretBoxUnavailableTenancyError() from error
        scopes = state.metadata.get("scopes")
        self._db.add(
            ConnectedAccountOAuthState(
                state=state.state,
                flow_id=self.flow_id,
                end_user_id=self._end_user_id,
                provider=self._provider,
                connection_key=self._connection_key,
                shared=self._shared,
                expected_account_identifier=self._expected_account_identifier,
                redirect_uri=state.redirect_uri,
                return_url=self._return_url,
                encrypted_code_verifier=verifier,
                requested_scopes=list(scopes) if isinstance(scopes, list) else None,
                created_at=datetime.fromtimestamp(state.created_at, tz=UTC),
            )
        )
        await self._db.commit()

    async def consume(self, state_key: str) -> OAuthPendingState | None:
        now = datetime.now(UTC)
        # One UPDATE decides: unconsumed, unexpired, this user's, this provider's.
        result = await self._db.execute(
            update(ConnectedAccountOAuthState)
            .where(
                ConnectedAccountOAuthState.state == state_key,
                ConnectedAccountOAuthState.end_user_id == self._end_user_id,
                ConnectedAccountOAuthState.provider == self._provider,
                ConnectedAccountOAuthState.consumed_at.is_(None),
                ConnectedAccountOAuthState.created_at >= now - OAUTH_STATE_TTL,
            )
            .values(consumed_at=now)
        )
        await self._db.commit()
        consumed = getattr(result, "rowcount", 0)
        if consumed != 1:
            return None
        row = (
            await self._db.execute(
                select(ConnectedAccountOAuthState).where(ConnectedAccountOAuthState.state == state_key)
            )
        ).scalar_one()
        verifier = decrypt_secret(row.encrypted_code_verifier) if row.encrypted_code_verifier else None
        return OAuthPendingState(
            state=row.state,
            redirect_uri=row.redirect_uri,
            code_verifier=verifier,
            created_at=row.created_at.timestamp(),
            metadata={"scopes": row.requested_scopes} if row.requested_scopes is not None else {},
        )


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class ConnectedAccountService:
    """Connect, list, refresh and disconnect an application's users' third-party accounts.

    Every method that acts for a user takes the workspace (the API key's) and
    the application's ``user`` string; the :class:`EndUser` row is created on
    first use. The workspace is the isolation boundary: two applications
    naming a user ``alice`` never see each other's grants.
    """

    def __init__(self, db: AsyncSession, config: GatewayConfig) -> None:
        self._db = db
        self._config = config

    # -- wiring (overridable in tests) ----------------------------------------

    def _client(
        self,
        provider: str,
        *,
        end_user_id: uuid.UUID,
        scopes: list[str] | None = None,
        return_url: str | None = None,
        store: DatabaseStateStore | None = None,
    ) -> OAuthClient:
        built = provider_config(self._config, provider, scopes)
        return OAuthClient(
            built,
            state_store=store
            or DatabaseStateStore(self._db, end_user_id=end_user_id, provider=provider, return_url=return_url),
            revocation_handler=_revocation_handler(self._config, provider),
            identity_handler=_identity_handler(provider, built),
        )

    # -- end users -------------------------------------------------------------

    async def end_user(self, workspace_id: uuid.UUID, user: str, *, create: bool) -> EndUser | None:
        """The end user row for ``user`` in ``workspace_id``, created on first use when ``create``."""
        row = (
            await self._db.execute(
                select(EndUser).where(EndUser.workspace_id == workspace_id, EndUser.external_id == user)
            )
        ).scalar_one_or_none()
        if row is None and create:
            row = EndUser(workspace_id=workspace_id, external_id=user)
            self._db.add(row)
            await self._db.commit()
            await self._db.refresh(row)
        return row

    # -- reads -----------------------------------------------------------------

    async def list_apps(self, workspace_id: uuid.UUID, user: str | None = None) -> list[ConnectedAppPublic]:
        counts: dict[str, int] = {}
        end_user = await self.end_user(workspace_id, user, create=False) if user else None
        if user:
            # One grouped query for every app, so a page listing the catalogue
            # costs the same whether the deployment offers two apps or ten.
            # Shared connections count: they are connections this user
            # resolves, and a UI that showed none would tell them to connect
            # something they can already use.
            owners = [ConnectedAccount.workspace_id == workspace_id]
            if end_user is not None:
                owners.append(ConnectedAccount.end_user_id == end_user.id)
            grouped = await self._db.execute(
                select(ConnectedAccount.provider, func.count()).where(or_(*owners)).group_by(ConnectedAccount.provider)
            )
            counts = {str(provider): int(count) for provider, count in grouped.all()}
        apps = []
        for provider in self._config.connected_app_providers:
            built = provider_config(self._config, provider)
            apps.append(
                ConnectedAppPublic(
                    provider=provider,
                    label=_PROVIDER_LABELS.get(provider, provider),
                    scopes=list(built.scopes),
                    scope_details=[
                        ScopePublic(
                            scope=meta.scope,
                            label=meta.label,
                            description=meta.description,
                            access_type=meta.access_type,
                            required=meta.required,
                        )
                        for meta in built.scope_metadata
                    ],
                    connected_accounts=counts.get(provider, 0),
                )
            )
        return apps

    async def list_accounts(
        self,
        workspace_id: uuid.UUID,
        user: str,
        provider: str | None = None,
        *,
        key: str | None = None,
        include_shared: bool = True,
    ) -> ConnectedAccountsPublic:
        """This user's connections, and by default the workspace's shared ones too.

        One call answers for every provider and partition, which is what a page
        listing "your apps" needs; ``provider`` and ``key`` narrow it.
        """
        end_user = await self.end_user(workspace_id, user, create=False)
        owners = []
        if end_user is not None:
            owners.append(ConnectedAccount.end_user_id == end_user.id)
        if include_shared:
            owners.append(ConnectedAccount.workspace_id == workspace_id)
        if not owners:
            return ConnectedAccountsPublic(data=[], count=0)
        statement = select(ConnectedAccount).where(or_(*owners))
        if provider:
            statement = statement.where(ConnectedAccount.provider == provider)
        if key is not None:
            statement = statement.where(ConnectedAccount.connection_key == key)
        rows = list(
            (
                await self._db.execute(statement.order_by(ConnectedAccount.provider, ConnectedAccount.created_at))
            ).scalars()
        )
        names = await self._external_ids({row.connected_by_end_user_id for row in rows})
        data = [
            _public(
                row,
                None if row.workspace_id is not None else user,
                connected_by=names.get(row.connected_by_end_user_id),
            )
            for row in rows
        ]
        return ConnectedAccountsPublic(data=data, count=len(data))

    async def _external_ids(self, end_user_ids: set[uuid.UUID | None]) -> dict[uuid.UUID | None, str]:
        """Map end user ids to the strings the application knows them by, in one query."""
        wanted = {identifier for identifier in end_user_ids if identifier is not None}
        if not wanted:
            return {}
        rows = await self._db.execute(select(EndUser.id, EndUser.external_id).where(EndUser.id.in_(wanted)))
        mapped: dict[uuid.UUID | None, str] = {identifier: external for identifier, external in rows.all()}
        return mapped

    async def get_account(self, workspace_id: uuid.UUID, user: str, account_id: uuid.UUID) -> ConnectedAccountPublic:
        row = await self._row(workspace_id, user, account_id)
        names = await self._external_ids({row.connected_by_end_user_id})
        return _public(
            row,
            None if row.workspace_id is not None else user,
            connected_by=names.get(row.connected_by_end_user_id),
        )

    # -- the flow --------------------------------------------------------------

    async def authorize(
        self,
        workspace_id: uuid.UUID,
        user: str,
        provider: str,
        *,
        key: str = "",
        scopes: list[str] | None = None,
        scope_mode: str = "exact",
        expected_account_identifier: str | None = None,
        shared: bool = False,
        return_url: str | None = None,
    ) -> AuthorizePublic:
        """Start a flow for ``user``: the consent URL to send their browser to.

        ``key`` partitions one provider into as many connections as the
        application distinguishes. ``scope_mode="add"`` asks for ``scopes``
        together with what the existing connection in that partition already
        holds, which is what a scope upgrade means: most providers replace the
        grant with what the authorization request names, so asking for only the
        new scope silently drops the others. ``expected_account_identifier``
        binds the flow to one account, and ``shared`` stores the result for the
        workspace instead of this user.

        Raises:
            ConnectedAppNotConfiguredError: If the app is not configured here.
            ConnectedAccountReturnUrlError: If ``return_url`` is not acceptable.
            ConnectedAccountLimitReachedError: If the user holds too many accounts.
            SecretBoxUnavailableTenancyError: If tokens could not be stored anyway.

        """
        provider_config(self._config, provider)  # refuse an unconfigured app before writing anything
        if return_url is not None:
            validate_return_url(return_url)
        if not _secret_box_ready():
            raise SecretBoxUnavailableTenancyError()
        end_user = await self.end_user(workspace_id, user, create=True)
        assert end_user is not None
        count = (
            await self._db.execute(select(func.count()).where(ConnectedAccount.end_user_id == end_user.id))
        ).scalar_one()
        if count >= MAX_CONNECTED_ACCOUNTS_PER_USER:
            raise ConnectedAccountLimitReachedError(MAX_CONNECTED_ACCOUNTS_PER_USER)
        asked = await self._scopes_to_ask(
            workspace_id, end_user, provider, key=key, scopes=scopes, scope_mode=scope_mode, shared=shared
        )
        store = DatabaseStateStore(
            self._db,
            end_user_id=end_user.id,
            provider=provider,
            return_url=return_url,
            connection_key=key,
            shared=shared,
            expected_account_identifier=expected_account_identifier,
        )
        client = self._client(provider, end_user_id=end_user.id, scopes=asked, return_url=return_url, store=store)
        url, pending = await client.get_authorization_url(metadata={"scopes": asked} if asked else None)
        return AuthorizePublic(
            authorization_url=url,
            flow_id=store.flow_id,
            expires_at=datetime.fromtimestamp(pending.created_at, tz=UTC) + OAUTH_STATE_TTL,
        )

    async def _scopes_to_ask(
        self,
        workspace_id: uuid.UUID,
        end_user: EndUser,
        provider: str,
        *,
        key: str,
        scopes: list[str] | None,
        scope_mode: str,
        shared: bool,
    ) -> list[str] | None:
        """The scope list to put on the authorization request.

        ``exact`` is what the caller said (None meaning the deployment's
        defaults). ``add`` unions it with the grant already in this partition,
        so an upgrade keeps what the user has already consented to.
        """
        if scope_mode != "add":
            return scopes
        existing = await self._resolve_row(workspace_id, end_user, provider, key=key, prefer_shared=shared)
        held = list(existing.scopes or []) if existing is not None else []
        if not held:
            return scopes
        base = scopes if scopes is not None else list(provider_config(self._config, provider).scopes)
        merged = list(dict.fromkeys([*held, *base]))
        return merged

    async def flow(self, workspace_id: uuid.UUID, flow_id: uuid.UUID) -> FlowPublic:
        """How a flow this application started ended, by the id ``authorize`` returned.

        The application's own alternative to holding the OAuth state: it can
        tell "the user finished" from "the user closed the window" without
        being able to complete or replay the flow. Scoped to the workspace, so
        one application cannot ask about another's.

        Raises:
            ConnectedAccountFlowNotFoundError: If no such flow belongs to this workspace.

        """
        row = (
            await self._db.execute(
                select(ConnectedAccountOAuthState, EndUser.external_id)
                .join(EndUser, EndUser.id == ConnectedAccountOAuthState.end_user_id)
                .where(ConnectedAccountOAuthState.flow_id == flow_id, EndUser.workspace_id == workspace_id)
            )
        ).first()
        if row is None:
            raise ConnectedAccountFlowNotFoundError(flow_id)
        pending, external_id = row
        expires_at = pending.created_at + OAUTH_STATE_TTL
        status = pending.status
        if status == "pending" and expires_at <= datetime.now(UTC):
            # Never stored as "expired": time decides this, and a row nobody
            # asks about should not need a sweep to become truthful.
            status = "expired"
        return FlowPublic(
            flow_id=pending.flow_id,
            provider=pending.provider,
            key=pending.connection_key,
            user=external_id,
            status=status,
            connection_id=pending.connected_account_id,
            reason=pending.failure_reason,
            expires_at=expires_at,
        )

    async def complete(self, *, state: str, code: str) -> tuple[ConnectedAccountPublic, str]:
        """Finish a flow from the hosted callback: exchange the code and store the grant.

        Returns the account and the URL to send the browser to. Trusts nothing
        but ``state``, which was minted here and names the end user, the
        provider and the return URL.

        Raises:
            ConnectedAccountStateInvalidError: If ``state`` is unknown, used or expired.
            ConnectedAccountExchangeError: If the provider refuses the exchange.

        """
        pending = (
            await self._db.execute(
                select(ConnectedAccountOAuthState).where(
                    ConnectedAccountOAuthState.state == state,
                    ConnectedAccountOAuthState.consumed_at.is_(None),
                    ConnectedAccountOAuthState.created_at >= datetime.now(UTC) - OAUTH_STATE_TTL,
                )
            )
        ).scalar_one_or_none()
        if pending is None:
            raise ConnectedAccountStateInvalidError()
        end_user = (await self._db.execute(select(EndUser).where(EndUser.id == pending.end_user_id))).scalar_one()
        provider = pending.provider
        client = self._client(provider, end_user_id=end_user.id, scopes=pending.requested_scopes)
        try:
            tokens = await client.exchange_code(code, state=state)
        except OAuthError as error:
            if _looks_like_missing_state(error):
                raise ConnectedAccountStateInvalidError() from error
            logger.warning("connection exchange with %s failed: %s", provider, type(error).__name__)
            await self._fail_flow(pending, "exchange")
            raise ConnectedAccountExchangeError(provider, "code exchange") from error
        identifier, account_label, metadata = await self._identity(client, provider, tokens)
        expected = pending.expected_account_identifier
        if expected is not None and (identifier is None or identifier != expected):
            # Nothing is stored: the user picked another account, and writing
            # this grant would either upgrade the wrong row or leave the
            # application with two it did not ask for. The tokens are dropped
            # rather than revoked — the user consented, they simply consented
            # for the wrong account, and they may well be using it elsewhere.
            await self._fail_flow(pending, "binding_mismatch")
            raise ConnectedAccountBindingError(expected, identifier)
        row = await self._upsert(
            end_user,
            provider,
            tokens,
            identifier,
            account_label,
            metadata,
            key=pending.connection_key,
            shared=pending.shared,
        )
        pending.status = "connected"
        pending.connected_account_id = row.id
        await self._db.commit()
        return (
            _public(row, None if pending.shared else end_user.external_id, connected_by=end_user.external_id),
            pending.return_url or default_return_url(self._config),
        )

    async def _fail_flow(self, pending: ConnectedAccountOAuthState, reason: str) -> None:
        """Record how a flow ended so the application can ask, then keep going with the error."""
        pending.status = "failed"
        pending.failure_reason = reason
        await self._db.commit()

    async def return_url_for_state(self, state: str, *, failure: str | None = None) -> str:
        """Where to send a browser whose flow failed, from the state it carries, else the default page.

        Records the failure on the flow while it is here, so an application
        polling ``flows/{flow_id}`` learns that the user denied consent or the
        provider sent no code — outcomes the browser knows about and the
        application otherwise would not.
        """
        row = (
            await self._db.execute(select(ConnectedAccountOAuthState).where(ConnectedAccountOAuthState.state == state))
        ).scalar_one_or_none()
        if row is None:
            return default_return_url(self._config)
        if failure is not None and row.status == "pending":
            row.status = "failed"
            row.failure_reason = failure[:200]
            await self._db.commit()
        return row.return_url or default_return_url(self._config)

    async def update(
        self, workspace_id: uuid.UUID, user: str, account_id: uuid.UUID, body: ConnectedAccountUpdate
    ) -> ConnectedAccountPublic:
        row = await self._row(workspace_id, user, account_id)
        row.label = body.label
        await self._db.commit()
        await self._db.refresh(row)
        return _public(row, None if row.workspace_id is not None else user)

    async def disconnect(self, workspace_id: uuid.UUID, user: str, account_id: uuid.UUID) -> None:
        """Delete the grant, revoking it at the provider first when that is possible.

        Revocation is best effort: a provider that is down should not keep a
        user from removing a credential they no longer want this deployment to
        hold. The row is deleted either way.
        """
        row = await self._row(workspace_id, user, account_id)
        await self._revoke_quietly(row)
        await self._db.delete(row)
        await self._db.commit()

    async def _revoke_quietly(self, row: ConnectedAccount) -> None:
        """Best-effort revocation at the provider; never raises.

        A provider that is down, or an app whose credentials have since been
        removed from config, must not keep a user from having a credential
        deleted here.
        """
        try:
            token = self._decrypt_row(row, row.encrypted_access_token)
        except (SecretDecryptionError, SecretBoxUnavailableError):
            return
        try:
            await self._client(row.provider, end_user_id=row.end_user_id or uuid.uuid4()).revoke_token(token)
        except (OAuthError, ConnectedAppNotConfiguredError) as error:
            logger.info("revocation at %s skipped: %s", row.provider, type(error).__name__)

    # -- credentials ---------------------------------------------------------------

    async def access_token(self, workspace_id: uuid.UUID, user: str, account_id: uuid.UUID) -> AccessToken:
        """A live token for ``account_id``, refreshed first if it is about to expire.

        Raises:
            ConnectedAccountNotFoundError: If the account is not this user's.
            ConnectedAccountExchangeError: If a needed refresh fails.

        """
        row = await self._row(workspace_id, user, account_id)
        return await self._live_token(row)

    async def access_token_for_provider(
        self, workspace_id: uuid.UUID, user: str, provider: str, *, key: str = ""
    ) -> AccessToken | None:
        """The token for this user's account with ``provider`` in partition ``key``.

        Resolution order, which is the whole point of shared connections: the
        user's own grant first, then the workspace's shared one. None when
        neither exists. Within one owner and partition the oldest account wins;
        a caller that means a specific account names it by id
        (:meth:`access_token`), which is what an application that lets a user
        pick between two accounts must do.
        """
        end_user = await self.end_user(workspace_id, user, create=False)
        row = await self._resolve_row(workspace_id, end_user, provider, key=key)
        return None if row is None else await self._live_token(row)

    async def _resolve_row(
        self,
        workspace_id: uuid.UUID,
        end_user: EndUser | None,
        provider: str,
        *,
        key: str = "",
        prefer_shared: bool = False,
    ) -> ConnectedAccount | None:
        """The connection a user resolves for one provider and partition, own before shared.

        ``prefer_shared`` flips the order for a caller that is about to write a
        shared grant and wants the shared row it will be upgrading.
        """

        async def own() -> ConnectedAccount | None:
            if end_user is None:
                return None
            return (
                await self._db.execute(
                    select(ConnectedAccount)
                    .where(
                        ConnectedAccount.end_user_id == end_user.id,
                        ConnectedAccount.provider == provider,
                        ConnectedAccount.connection_key == key,
                    )
                    .order_by(ConnectedAccount.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()

        async def shared() -> ConnectedAccount | None:
            return (
                await self._db.execute(
                    select(ConnectedAccount)
                    .where(
                        ConnectedAccount.workspace_id == workspace_id,
                        ConnectedAccount.provider == provider,
                        ConnectedAccount.connection_key == key,
                    )
                    .order_by(ConnectedAccount.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()

        first, second = (shared, own) if prefer_shared else (own, shared)
        return await first() or await second()

    async def report_rejected(
        self, workspace_id: uuid.UUID, user: str, account_id: uuid.UUID, *, reason: str | None = None
    ) -> ConnectedAccountPublic:
        """Tell otari the provider refused this credential, and let it try once to recover.

        The contract for a consumer that got a 401 or 403 from the provider: a
        stored token can be dead while its recorded expiry is still in the
        future (the user revoked the grant, or the provider rotated it), and
        nothing otari can see says so. One forced refresh is attempted; if it
        works the connection stays active and the caller can retry, and if it
        does not the connection is marked ``needs_reauth`` so every later
        reader — the application's own listing included — can prompt the user
        instead of failing the same way again.
        """
        row = await self._row(workspace_id, user, account_id)
        if row.encrypted_refresh_token:
            try:
                tokens = await self._client(row.provider, end_user_id=row.end_user_id or uuid.uuid4()).refresh_token(
                    self._decrypt_row(row, row.encrypted_refresh_token)
                )
            except (OAuthError, SecretDecryptionError) as error:
                logger.info("forced refresh at %s failed: %s", row.provider, type(error).__name__)
            else:
                _apply_tokens(row, tokens, keep_refresh=True)
                row.status = "active"
                row.invalid_reason = None
                row.invalid_at = None
                await self._db.commit()
                await self._db.refresh(row)
                return _public(row, None if row.workspace_id is not None else user)
        row.status = "needs_reauth"
        row.invalid_reason = (reason or "the provider rejected the credential")[:200]
        row.invalid_at = datetime.now(UTC)
        await self._db.commit()
        await self._db.refresh(row)
        return _public(row, None if row.workspace_id is not None else user)

    async def forget_user(self, workspace_id: uuid.UUID, user: str) -> int:
        """Delete everything otari holds for one of the application's users; return how many grants went.

        What an application calls when its own user deletes their account, so
        "delete my data" reaches across the boundary instead of stopping at the
        application's database. Each grant is revoked at the provider first,
        best effort, then the end user row goes and takes its grants and
        pending flows with it. A shared workspace connection this user
        happened to consent to is NOT deleted: it belongs to the workspace and
        others are still acting through it; the record of who connected it is
        cleared by the SET NULL on that column.
        """
        end_user = await self.end_user(workspace_id, user, create=False)
        if end_user is None:
            return 0
        rows = list(
            (
                await self._db.execute(select(ConnectedAccount).where(ConnectedAccount.end_user_id == end_user.id))
            ).scalars()
        )
        for row in rows:
            await self._revoke_quietly(row)
            await self._db.delete(row)
        # Deleted here rather than left to ON DELETE CASCADE: the OSS edition
        # runs on SQLite, where foreign keys are off unless a connection turns
        # them on, and "forget this user" must not depend on that.
        await self._db.execute(
            delete(ConnectedAccountOAuthState).where(ConnectedAccountOAuthState.end_user_id == end_user.id)
        )
        await self._db.delete(end_user)
        await self._db.commit()
        return len(rows)

    # -- internals ---------------------------------------------------------------

    async def _live_token(self, row: ConnectedAccount) -> AccessToken:
        expiring = row.expires_at is not None and row.expires_at <= datetime.now(UTC) + REFRESH_LEEWAY
        if expiring and row.encrypted_refresh_token:
            try:
                tokens = await self._client(row.provider, end_user_id=row.end_user_id or uuid.uuid4()).refresh_token(
                    self._decrypt_row(row, row.encrypted_refresh_token)
                )
            except OAuthError as error:
                # A refresh the provider refuses is the other way a credential
                # dies (next to a consumer's 401): mark it so the application
                # can prompt a reconnect instead of retrying into the same
                # failure on every request.
                row.status = "needs_reauth"
                row.invalid_reason = "the provider refused to refresh the credential"
                row.invalid_at = datetime.now(UTC)
                await self._db.commit()
                raise ConnectedAccountExchangeError(row.provider, "token refresh") from error
            _apply_tokens(row, tokens, keep_refresh=True)
            await self._db.commit()
            await self._db.refresh(row)
        extra: dict[str, str] = (
            json.loads(self._decrypt_row(row, row.encrypted_extra_tokens)) if row.encrypted_extra_tokens else {}
        )
        return AccessToken(
            connection_id=row.id,
            provider=row.provider,
            key=row.connection_key,
            token=self._decrypt_row(row, row.encrypted_access_token),
            token_type=row.token_type,
            expires_at=row.expires_at,
            scopes=list(row.scopes or []),
            extra=extra,
            extra_scopes={name: list(scopes) for name, scopes in (row.extra_scopes or {}).items()},
            account_identifier=row.account_identifier,
            account_label=row.account_label,
            account_metadata=dict(row.account_metadata or {}),
        )

    def _decrypt_row(self, row: ConnectedAccount, ciphertext: str) -> str:
        """Decrypt one of ``row``'s secrets, through its own key when it has one.

        A row written before the envelope (``encrypted_data_key`` NULL) is read
        with the deployment key directly, so no migration of ciphertext is
        needed: those rows are re-enveloped the next time they are written.
        """
        if row.encrypted_data_key is None:
            return decrypt_secret(ciphertext)
        return decrypt_under(ciphertext, decrypt_secret(row.encrypted_data_key))

    async def _row(self, workspace_id: uuid.UUID, user: str, account_id: uuid.UUID) -> ConnectedAccount:
        """One connection this user may act on: their own, or the workspace's shared one.

        A user reaching a shared connection by id is deliberate — they resolve
        it for tokens, so they can also see it, relabel it and disconnect it.
        The workspace is the boundary that matters; within it the application
        decides who may do what.
        """
        own = (
            select(ConnectedAccount)
            .join(EndUser, EndUser.id == ConnectedAccount.end_user_id)
            .where(
                ConnectedAccount.id == account_id,
                EndUser.workspace_id == workspace_id,
                EndUser.external_id == user,
            )
        )
        row = (await self._db.execute(own)).scalar_one_or_none()
        if row is None:
            row = (
                await self._db.execute(
                    select(ConnectedAccount).where(
                        ConnectedAccount.id == account_id, ConnectedAccount.workspace_id == workspace_id
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            raise ConnectedAccountNotFoundError(account_id)
        return row

    async def _identity(
        self, client: OAuthClient, provider: str, tokens: TokenSet
    ) -> tuple[str | None, str | None, dict[str, Any]]:
        """Who the account is, from the provider's own identity endpoint. Best effort."""
        try:
            profile = await client.fetch_identity(tokens)
        except Exception as error:  # noqa: BLE001 - identity is a nicety; the grant is what matters
            logger.info("identity fetch at %s skipped: %s", provider, type(error).__name__)
            return None, None, {}
        tenancy = profile.tenancies[0] if profile.tenancies else None
        tenancy_id = getattr(tenancy, "id", None) if tenancy is not None else None
        tenancy_name = getattr(tenancy, "name", None) if tenancy is not None else None
        identifier = profile.email or profile.username or profile.subject or tenancy_id
        label = profile.name or profile.email or profile.username or tenancy_name
        if tenancy_name and label and tenancy_name != label:
            label = f"{label} ({tenancy_name})"
        metadata: dict[str, Any] = {
            key: value
            for key, value in {
                "subject": profile.subject,
                "email": profile.email,
                "name": profile.name,
                "username": profile.username,
                "tenancy_id": tenancy_id,
                "tenancy_name": tenancy_name,
            }.items()
            if value
        }
        return (str(identifier) if identifier else None), (str(label) if label else None), metadata

    async def _upsert(
        self,
        end_user: EndUser,
        provider: str,
        tokens: TokenSet,
        identifier: str | None,
        account_label: str | None,
        metadata: dict[str, Any],
        *,
        key: str = "",
        shared: bool = False,
    ) -> ConnectedAccount:
        """Store the grant, updating the row for this owner, provider, key and account.

        Reconnecting the same account in the same partition upgrades one row,
        which is what makes a scope upgrade land where the application resolves
        it. A shared grant is keyed by the workspace instead of the person, and
        remembers who consented.
        """
        owner = (
            {"workspace_id": end_user.workspace_id, "connected_by_end_user_id": end_user.id}
            if shared
            else {"end_user_id": end_user.id, "connected_by_end_user_id": end_user.id}
        )
        row: ConnectedAccount | None = None
        if identifier is not None:
            owner_match = (
                ConnectedAccount.workspace_id == end_user.workspace_id
                if shared
                else ConnectedAccount.end_user_id == end_user.id
            )
            row = (
                await self._db.execute(
                    select(ConnectedAccount).where(
                        owner_match,
                        ConnectedAccount.provider == provider,
                        ConnectedAccount.connection_key == key,
                        ConnectedAccount.account_identifier == identifier,
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            row = ConnectedAccount(
                provider=provider,
                connection_key=key,
                account_identifier=identifier,
                encrypted_access_token="",
                **owner,
            )
            self._db.add(row)
        else:
            row.connected_by_end_user_id = end_user.id
        row.account_label = account_label
        row.account_metadata = metadata or None
        # A reconnect is how a dead credential comes back to life.
        row.status = "active"
        row.invalid_reason = None
        row.invalid_at = None
        try:
            _apply_tokens(row, tokens, keep_refresh=False)
        except SecretBoxUnavailableError as error:
            raise SecretBoxUnavailableTenancyError() from error
        await self._db.commit()
        await self._db.refresh(row)
        return row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _identity_handler(provider: str, built: ProviderConfig) -> Any:
    module = {
        "atlassian": apron_atlassian,
        "github": apron_github,
        "google": apron_google,
        "hubspot": apron_hubspot,
        "linear": apron_linear,
        "microsoft": apron_microsoft,
        "notion": apron_notion,
        "salesforce": apron_salesforce,
        "slack": apron_slack,
        "typeform": apron_typeform,
    }[provider]
    return module.maybe_identity_handler(built)


def _secret_box_ready() -> bool:
    try:
        encrypt_secret("probe")
    except SecretBoxUnavailableError:
        return False
    return True


def _looks_like_missing_state(error: OAuthError) -> bool:
    text = str(error).lower()
    return "state" in text and ("not found" in text or "invalid" in text or "expired" in text or "unknown" in text)


def _expires_at(tokens: TokenSet) -> datetime | None:
    if tokens.expires_at is not None:
        return datetime.fromtimestamp(tokens.expires_at, tz=UTC)
    if tokens.expires_in is not None:
        return datetime.fromtimestamp(time.time() + tokens.expires_in, tz=UTC)
    return None


def _extra_tokens(tokens: TokenSet) -> dict[str, str]:
    """Secondary credentials some providers issue next to the primary one.

    Slack's ``oauth.v2.access`` answers with the bot token as ``access_token``
    and the user's own under ``authed_user``; both are kept so a consumer can
    act as either.
    """
    extra: dict[str, str] = {}
    authed_user = tokens.metadata.get("authed_user")
    if isinstance(authed_user, dict) and isinstance(authed_user.get("access_token"), str):
        extra["user"] = authed_user["access_token"]
    return extra


def _apply_tokens(row: ConnectedAccount, tokens: TokenSet, *, keep_refresh: bool) -> None:
    """Write a token set onto a row, encrypted under the row's own key.

    Every write mints a fresh data key, so a row that predates the envelope is
    enveloped the next time it is refreshed, and re-encrypting a row does not
    reuse a key with a new plaintext.
    """
    data_key = generate_data_key()
    keep_extra = row.encrypted_extra_tokens if keep_refresh else None
    keep_refresh_token = row.encrypted_refresh_token if keep_refresh else None
    if keep_extra is not None or keep_refresh_token is not None:
        # Re-encrypt what we are keeping under the new key, so one row never
        # holds ciphertext from two envelopes.
        if keep_refresh_token is not None:
            keep_refresh_token = encrypt_under(_decrypt_row_value(row, keep_refresh_token), data_key)
        if keep_extra is not None:
            keep_extra = encrypt_under(_decrypt_row_value(row, keep_extra), data_key)
    row.encrypted_data_key = encrypt_secret(data_key)
    row.encrypted_access_token = encrypt_under(tokens.access_token, data_key)
    if tokens.refresh_token:
        row.encrypted_refresh_token = encrypt_under(tokens.refresh_token, data_key)
    else:
        row.encrypted_refresh_token = keep_refresh_token
    extra = _extra_tokens(tokens)
    if extra:
        row.encrypted_extra_tokens = encrypt_under(json.dumps(extra), data_key)
        row.extra_scopes = _extra_scopes(tokens) or None
    else:
        row.encrypted_extra_tokens = keep_extra
        if keep_extra is None:
            row.extra_scopes = None
    row.token_type = tokens.token_type or "Bearer"
    row.expires_at = _expires_at(tokens)
    if tokens.scope:
        row.scopes = _split_scopes(tokens.scope)


def _decrypt_row_value(row: ConnectedAccount, ciphertext: str) -> str:
    """A row's secret under whichever envelope wrote it (see ``_decrypt_row``)."""
    if row.encrypted_data_key is None:
        return decrypt_secret(ciphertext)
    return decrypt_under(ciphertext, decrypt_secret(row.encrypted_data_key))


def _split_scopes(raw: str) -> list[str]:
    return raw.replace(",", " ").split()


def _extra_scopes(tokens: TokenSet) -> dict[str, list[str]]:
    """Which scopes each secondary token holds.

    Slack grants its user token a scope set of its own, next to the bot's; a
    consumer that has to choose which token may post needs them apart.
    """
    authed_user = tokens.metadata.get("authed_user")
    if isinstance(authed_user, dict) and isinstance(authed_user.get("scope"), str):
        return {"user": _split_scopes(authed_user["scope"])}
    return {}


def _public(row: ConnectedAccount, user: str | None, *, connected_by: str | None = None) -> ConnectedAccountPublic:
    return ConnectedAccountPublic(
        id=row.id,
        user=user,
        owner="workspace" if row.workspace_id is not None else "user",
        connected_by=connected_by,
        provider=row.provider,
        key=row.connection_key,
        account_identifier=row.account_identifier,
        account_label=row.account_label,
        label=row.label,
        status="needs_reauth" if row.status == "needs_reauth" else "active",
        invalid_reason=row.invalid_reason,
        scopes=list(row.scopes or []),
        extra_scopes={name: list(scopes) for name, scopes in (row.extra_scopes or {}).items()},
        account_metadata=dict(row.account_metadata or {}),
        expires_at=row.expires_at,
        has_refresh_token=row.encrypted_refresh_token is not None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def provider_label(provider: str) -> str:
    return _PROVIDER_LABELS.get(provider, provider)
