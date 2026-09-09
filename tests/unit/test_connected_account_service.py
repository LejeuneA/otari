"""Connections: config, the database state store, and the service, against an
in-memory database with apron-auth's client replaced by a scripted stand-in."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import pytest
from apron_auth.errors import OAuthError
from apron_auth.models import IdentityProfile, OAuthPendingState, TenancyContext, TokenSet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import gateway.models  # noqa: F401  (registers every table on the shared metadata)
from gateway.core.config import GatewayConfig
from gateway.models.entities import ConnectedAccount, ConnectedAccountOAuthState, EndUser
from gateway.models.tenancy import Organization, Workspace
from gateway.services.secret_box import (
    SecretDecryptionError,
    decrypt_secret,
    decrypt_under,
    encrypt_secret,
    generate_secret_key,
)
from gateway.services.tenancy import connected_account_service as svc
from gateway.services.tenancy.connected_account_service import (
    ConnectedAccountService,
    ConnectedAccountUpdate,
    DatabaseStateStore,
    ImportRequest,
    provider_config,
    redirect_uri,
    validate_return_url,
    with_query,
)
from gateway.services.tenancy.errors import (
    ConnectedAccountBindingError,
    ConnectedAccountExchangeError,
    ConnectedAccountFlowNotFoundError,
    ConnectedAccountNotFoundError,
    ConnectedAccountReturnUrlError,
    ConnectedAccountStateInvalidError,
    ConnectedAppNotConfiguredError,
    SecretBoxUnavailableTenancyError,
)

T = TypeVar("T")
ALICE, BOB = "alice@acme.test", "bob@acme.test"


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OTARI_SECRET_KEY", generate_secret_key())
    yield


def configured(**apps: Any) -> GatewayConfig:
    entries = apps or {
        "slack": {"client_id": "slack-id", "client_secret": "slack-secret", "user_scopes": ["channels:read"]},
        "github": {"client_id": "gh-id", "client_secret": "gh-secret", "scopes": ["repo"]},
    }
    return GatewayConfig(public_base_url="https://otari.example.com", connected_apps=entries)


def run(scenario: Callable[[AsyncSession], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                return await scenario(session)
        finally:
            await engine.dispose()

    return asyncio.run(main())


async def make_workspace(session: AsyncSession) -> uuid.UUID:
    slug = uuid.uuid4().hex[:8]
    organization = Organization(name=f"Acme {slug}", slug=f"acme-{slug}")
    session.add(organization)
    await session.commit()
    await session.refresh(organization)
    workspace = Workspace(name=f"ws-{slug}", organization_id=organization.id)
    session.add(workspace)
    await session.commit()
    await session.refresh(workspace)
    return workspace.id


class FakeOAuthClient:
    """Scripted stand-in for apron-auth's client: same surface, no network."""

    def __init__(self, store: DatabaseStateStore, *, tokens: TokenSet, identity: IdentityProfile | None) -> None:
        self.store = store
        self.tokens = tokens
        self.identity = identity
        self.revoked: list[str] = []
        self.refreshed: list[str] = []

    async def get_authorization_url(
        self, redirect_uri: str | None = None, metadata: dict[str, Any] | None = None
    ) -> tuple[str, OAuthPendingState]:
        pending = OAuthPendingState(
            state=uuid.uuid4().hex,
            redirect_uri=redirect_uri or "https://otari.example.com/cb",
            code_verifier="verifier-123",
            created_at=time.time(),
            metadata=metadata or {},
        )
        await self.store.save(pending)
        return f"https://provider.test/authorize?state={pending.state}", pending

    async def exchange_code(self, code: str, state: str | None = None, **_: Any) -> TokenSet:
        assert state is not None
        pending = await self.store.consume(state)
        if pending is None:
            msg = "State not found or expired"
            raise OAuthError(msg)
        if code == "bad-code":
            msg = "invalid_grant"
            raise OAuthError(msg)
        assert pending.code_verifier == "verifier-123"
        return self.tokens

    async def fetch_identity(self, tokens: TokenSet) -> IdentityProfile:
        if self.identity is None:
            msg = "no identity"
            raise OAuthError(msg)
        return self.identity

    refuse_refresh = False

    async def refresh_token(self, refresh_token: str) -> TokenSet:
        self.refreshed.append(refresh_token)
        if self.refuse_refresh:
            msg = "invalid_grant: token revoked"
            raise OAuthError(msg)
        return TokenSet(access_token="refreshed-access", refresh_token=None, expires_in=3600, scope=self.tokens.scope)

    async def revoke_token(self, token: str) -> bool:
        self.revoked.append(token)
        return True


class Service(ConnectedAccountService):
    """The real service with the apron client swapped for the fake."""

    def __init__(
        self, db: AsyncSession, config: GatewayConfig, *, tokens: TokenSet, identity: IdentityProfile | None
    ) -> None:
        super().__init__(db, config)
        self.fake: FakeOAuthClient | None = None
        self.asked_scopes: list[str] | None = None
        self._fake_tokens, self._fake_identity = tokens, identity

    def _client(
        self,
        provider: str,
        *,
        end_user_id: uuid.UUID,
        scopes: list[str] | None = None,
        return_url: str | None = None,
        store: DatabaseStateStore | None = None,
    ) -> Any:
        provider_config(self._config, provider, scopes)  # still refuses an unconfigured app
        # The store the service built carries the flow's intent (its id, the
        # partition, whether it is shared, the account it is bound to), so the
        # fake must use it rather than making its own.
        self.asked_scopes = scopes
        self.fake = FakeOAuthClient(
            store or DatabaseStateStore(self._db, end_user_id=end_user_id, provider=provider, return_url=return_url),
            tokens=self._fake_tokens,
            identity=self._fake_identity,
        )
        return self.fake


SLACK_TOKENS = TokenSet(
    access_token="xoxb-bot",
    refresh_token="refresh-1",
    expires_in=3600,
    scope="chat:write,channels:read",
    metadata={"authed_user": {"id": "U1", "access_token": "xoxp-user"}},
)
SLACK_IDENTITY = IdentityProfile(
    provider="slack", subject="U1", name="Alice", tenancies=(TenancyContext(id="T001", name="Acme Corp"),)
)


async def _authorize_and_complete(
    service: Service, workspace_id: uuid.UUID, user: str, provider: str, **kw: Any
) -> Any:
    started = await service.authorize(workspace_id, user, provider, **kw)
    state = started.authorization_url.rsplit("state=", 1)[1]
    account, _return = await service.complete(state=state, code="good-code")
    return account


# -- config and helpers ----------------------------------------------------------


def test_config_validates_entries() -> None:
    with pytest.raises(ValueError, match="not a supported app"):
        GatewayConfig(connected_apps={"myspace": {"client_id": "a", "client_secret": "b"}})
    with pytest.raises(ValueError, match="client_secret is required"):
        GatewayConfig(connected_apps={"slack": {"client_id": "a"}})
    with pytest.raises(ValueError, match="user_scopes is only meaningful for slack"):
        GatewayConfig(connected_apps={"github": {"client_id": "a", "client_secret": "b", "user_scopes": []}})
    assert configured().connected_app_providers == ("github", "slack")


def test_an_app_without_public_base_url_is_not_on_offer() -> None:
    config = GatewayConfig(connected_apps={"github": {"client_id": "a", "client_secret": "b"}})
    assert config.connected_app_providers == ()
    with pytest.raises(ConnectedAppNotConfiguredError):
        provider_config(config, "github")


def test_provider_config_uses_presets_and_derived_redirect() -> None:
    config = configured()
    built = provider_config(config, "github")
    assert built.client_id == "gh-id"
    assert "repo" in built.scopes and "read:user" in built.scopes  # preset base scopes are kept
    assert built.redirect_uri == "https://otari.example.com/connected-accounts/github/callback"
    assert redirect_uri(config, "slack").endswith("/connected-accounts/slack/callback")
    assert provider_config(config, "slack").authorize_url.startswith("https://slack.com/oauth")


def test_return_url_rules() -> None:
    validate_return_url("https://myapp.example.com/settings?tab=apps")
    validate_return_url("http://localhost:3000/settings")
    for bad in ("http://myapp.example.com/x", "https://myapp.example.com/x#frag", "javascript:alert(1)", "/relative"):
        with pytest.raises(ConnectedAccountReturnUrlError):
            validate_return_url(bad)
    assert (
        with_query("https://a.test/p", connection="ok", provider="slack")
        == "https://a.test/p?connection=ok&provider=slack"
    )
    assert with_query("https://a.test/p?x=1", connection="ok") == "https://a.test/p?x=1&connection=ok"
    assert with_query("https://a.test/#/connections", connection="ok") == "https://a.test/#/connections?connection=ok"


# -- the flow ------------------------------------------------------------------


def test_connect_flow_creates_end_user_and_stores_encrypted_tokens() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        started = await service.authorize(workspace, ALICE, "slack", return_url="https://myapp.test/settings")
        assert started.authorization_url.startswith("https://provider.test/authorize")
        assert started.expires_at > datetime.now(UTC) + timedelta(minutes=9)
        end_user = (await session.execute(select(EndUser))).scalar_one()
        assert (end_user.workspace_id, end_user.external_id) == (workspace, ALICE)
        pending = (await session.execute(select(ConnectedAccountOAuthState))).scalar_one()
        assert pending.end_user_id == end_user.id and pending.return_url == "https://myapp.test/settings"
        assert pending.encrypted_code_verifier not in (None, "verifier-123")

        state = started.authorization_url.rsplit("state=", 1)[1]
        account, return_to = await service.complete(state=state, code="good-code")
        assert return_to == "https://myapp.test/settings"
        assert (account.user, account.provider, account.account_identifier) == (ALICE, "slack", "U1")
        assert account.account_label == "Alice (Acme Corp)"
        assert account.scopes == ["chat:write", "channels:read"] and account.has_refresh_token

        row = (await session.execute(select(ConnectedAccount))).scalar_one()
        assert "xoxb-bot" not in (row.encrypted_access_token + (row.encrypted_extra_tokens or ""))
        token = await service.access_token(workspace, ALICE, account.id)
        assert token.token == "xoxb-bot" and token.extra == {"user": "xoxp-user"}
        apps = await service.list_apps(workspace, ALICE)
        assert {app.provider: app.connected_accounts for app in apps} == {"github": 0, "slack": 1}

        with pytest.raises(ConnectedAccountStateInvalidError):  # single use
            await service.complete(state=state, code="good-code")

    run(scenario)


def test_default_return_url_when_none_given() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        started = await service.authorize(workspace, ALICE, "slack")
        state = started.authorization_url.rsplit("state=", 1)[1]
        _, return_to = await service.complete(state=state, code="c")
        assert return_to == "https://otari.example.com/#/connections"
        assert await service.return_url_for_state("unknown") == "https://otari.example.com/#/connections"

    run(scenario)


def test_workspaces_isolate_users_with_the_same_name() -> None:
    async def scenario(session: AsyncSession) -> None:
        first, second = await make_workspace(session), await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        account = await _authorize_and_complete(service, first, ALICE, "slack")
        assert (await service.list_accounts(second, ALICE)).count == 0
        with pytest.raises(ConnectedAccountNotFoundError):
            await service.get_account(second, ALICE, account.id)
        with pytest.raises(ConnectedAccountNotFoundError):
            await service.get_account(first, BOB, account.id)
        assert await service.access_token_for_provider(second, ALICE, "slack") is None
        assert (await service.list_accounts(first, ALICE, provider="slack")).count == 1

    run(scenario)


def test_reconnecting_the_same_account_updates_the_row() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        first = await _authorize_and_complete(service, workspace, ALICE, "slack")
        service._fake_tokens = TokenSet(access_token="xoxb-new", scope="chat:write")
        second = await _authorize_and_complete(service, workspace, ALICE, "slack")
        assert first.id == second.id
        assert (await service.list_accounts(workspace, ALICE)).count == 1
        assert (await service.access_token(workspace, ALICE, first.id)).token == "xoxb-new"

    run(scenario)


def test_expired_state_is_refused() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        started = await service.authorize(workspace, ALICE, "slack")
        row = (await session.execute(select(ConnectedAccountOAuthState))).scalar_one()
        row.created_at = datetime.now(UTC) - timedelta(minutes=11)
        await session.commit()
        with pytest.raises(ConnectedAccountStateInvalidError):
            await service.complete(state=started.authorization_url.rsplit("state=", 1)[1], code="c")

    run(scenario)


def test_provider_refusal_is_a_502_and_missing_identity_is_tolerated() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=TokenSet(access_token="gho_x", scope="repo"), identity=None)
        started = await service.authorize(workspace, ALICE, "github")
        with pytest.raises(ConnectedAccountExchangeError):
            await service.complete(state=started.authorization_url.rsplit("state=", 1)[1], code="bad-code")
        account = await _authorize_and_complete(service, workspace, ALICE, "github")
        assert account.account_identifier is None and account.account_label is None
        assert (await service.access_token(workspace, ALICE, account.id)).token == "gho_x"

    run(scenario)


def test_access_token_refreshes_when_about_to_expire() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        account = await _authorize_and_complete(service, workspace, ALICE, "slack")
        row = (await session.execute(select(ConnectedAccount))).scalar_one()
        row.expires_at = datetime.now(UTC) + timedelta(seconds=10)
        await session.commit()
        token = await service.access_token_for_provider(workspace, ALICE, "slack")
        assert token is not None and token.token == "refreshed-access"
        assert service.fake is not None and service.fake.refreshed == ["refresh-1"]
        assert (await service.get_account(workspace, ALICE, account.id)).has_refresh_token is True

    run(scenario)


def test_update_label_and_disconnect_revokes() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        account = await _authorize_and_complete(service, workspace, ALICE, "slack")
        updated = await service.update(workspace, ALICE, account.id, ConnectedAccountUpdate(label="Work Slack"))
        assert updated.label == "Work Slack"
        await service.disconnect(workspace, ALICE, account.id)
        assert service.fake is not None and service.fake.revoked == ["xoxb-bot"]
        assert (await service.list_accounts(workspace, ALICE)).count == 0

    run(scenario)


def test_unconfigured_app_bad_return_url_and_missing_secret_key_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        with pytest.raises(ConnectedAppNotConfiguredError):
            await service.authorize(workspace, ALICE, "notion")
        with pytest.raises(ConnectedAccountReturnUrlError):
            await service.authorize(workspace, ALICE, "slack", return_url="http://evil.test/")
        assert (await session.execute(select(EndUser))).scalar_one_or_none() is None  # nothing written on refusal
        monkeypatch.delenv("OTARI_SECRET_KEY")
        with pytest.raises(SecretBoxUnavailableTenancyError):
            await service.authorize(workspace, ALICE, "slack")

    run(scenario)


def test_token_helpers() -> None:
    assert svc._extra_tokens(SLACK_TOKENS) == {"user": "xoxp-user"}
    assert svc._extra_tokens(TokenSet(access_token="a")) == {}
    row = ConnectedAccount(end_user_id=uuid.uuid4(), provider="slack", encrypted_access_token="")
    svc._apply_tokens(row, TokenSet(access_token="a", scope="x y", expires_in=60), keep_refresh=False)
    assert row.scopes == ["x", "y"] and row.expires_at is not None and row.encrypted_refresh_token is None
    assert json.loads('{"user": "u"}') == {"user": "u"}


# -- one provider, several connections -----------------------------------------


def test_a_key_partitions_one_provider_into_separate_connections() -> None:
    """An application whose product shows Gmail and Drive apart connects Google twice."""

    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        config = configured(google={"client_id": "g-id", "client_secret": "g-secret"})
        service = Service(session, config, tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        mail = await _authorize_and_complete(service, workspace, ALICE, "google", key="mail")
        service._fake_tokens = TokenSet(access_token="drive-token", scope="drive.readonly")
        drive = await _authorize_and_complete(service, workspace, ALICE, "google", key="drive")

        assert mail.id != drive.id and (mail.key, drive.key) == ("mail", "drive")
        assert (await service.list_accounts(workspace, ALICE)).count == 2
        assert (await service.list_accounts(workspace, ALICE, key="drive")).count == 1

        # Each partition resolves its own credential, and disconnecting one
        # leaves the other alone: the two are separate as far as the
        # application is concerned, even though one provider issued both.
        mail_token = await service.access_token_for_provider(workspace, ALICE, "google", key="mail")
        drive_token = await service.access_token_for_provider(workspace, ALICE, "google", key="drive")
        assert mail_token is not None and mail_token.token == "xoxb-bot"
        assert drive_token is not None and drive_token.token == "drive-token"
        await service.disconnect(workspace, ALICE, drive.id)
        assert (await service.list_accounts(workspace, ALICE)).count == 1
        assert await service.access_token_for_provider(workspace, ALICE, "google", key="mail") is not None

    run(scenario)


def test_scope_mode_add_asks_for_the_union_with_what_is_already_granted() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        await _authorize_and_complete(service, workspace, ALICE, "slack", scopes=["chat:write", "channels:read"])

        # An upgrade that names only the new scope would drop the two the user
        # has already consented to at most providers, so "add" unions them.
        await service.authorize(workspace, ALICE, "slack", scopes=["files:read"], scope_mode="add")
        assert service.asked_scopes == ["chat:write", "channels:read", "files:read"]

        # "exact" stays literal, which is what a scope *reduction* needs.
        await service.authorize(workspace, ALICE, "slack", scopes=["files:read"], scope_mode="exact")
        assert service.asked_scopes == ["files:read"]

        # Nothing granted yet in this partition: nothing to union with.
        await service.authorize(workspace, ALICE, "slack", key="other", scopes=["files:read"], scope_mode="add")
        assert service.asked_scopes == ["files:read"]

    run(scenario)


def test_a_flow_bound_to_an_account_refuses_a_different_one() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        started = await service.authorize(workspace, ALICE, "slack", expected_account_identifier="U-OTHER")
        state = started.authorization_url.rsplit("state=", 1)[1]
        with pytest.raises(ConnectedAccountBindingError, match="U-OTHER"):
            await service.complete(state=state, code="good-code")
        # Nothing stored: an upgrade that landed on another account would leave
        # the credential the application resolves un-upgraded.
        assert (await service.list_accounts(workspace, ALICE)).count == 0
        flow = await service.flow(workspace, started.flow_id)
        assert (flow.status, flow.reason) == ("failed", "binding_mismatch")

        # The account it was started for goes through.
        ok = await _authorize_and_complete(service, workspace, ALICE, "slack", expected_account_identifier="U1")
        assert ok.account_identifier == "U1"

    run(scenario)


# -- shared connections ---------------------------------------------------------


def test_a_shared_connection_serves_every_user_and_the_owner_is_the_workspace() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        shared = await _authorize_and_complete(service, workspace, ALICE, "slack", shared=True)
        assert (shared.owner, shared.user, shared.connected_by) == ("workspace", None, ALICE)

        # Bob never connected anything, and resolves it anyway.
        token = await service.access_token_for_provider(workspace, BOB, "slack")
        assert token is not None and token.token == "xoxb-bot"
        listed = await service.list_accounts(workspace, BOB)
        assert listed.count == 1 and listed.data[0].owner == "workspace"
        assert listed.data[0].connected_by == ALICE
        assert (await service.list_accounts(workspace, BOB, include_shared=False)).count == 0
        assert {app.provider: app.connected_accounts for app in await service.list_apps(workspace, BOB)}["slack"] == 1

        # Another workspace sees none of it.
        other = await make_workspace(session)
        assert await service.access_token_for_provider(other, BOB, "slack") is None

    run(scenario)


def test_a_users_own_connection_wins_over_the_shared_one() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        await _authorize_and_complete(service, workspace, ALICE, "slack", shared=True)
        service._fake_tokens = TokenSet(access_token="bobs-own", scope="chat:write")
        service._fake_identity = IdentityProfile(provider="slack", subject="U2", name="Bob")
        own = await _authorize_and_complete(service, workspace, BOB, "slack")

        assert own.owner == "user"
        token = await service.access_token_for_provider(workspace, BOB, "slack")
        assert token is not None and token.token == "bobs-own"
        # Alice, who has no personal Slack, still gets the shared one.
        alice_token = await service.access_token_for_provider(workspace, ALICE, "slack")
        assert alice_token is not None and alice_token.token == "xoxb-bot"

    run(scenario)


# -- flows ----------------------------------------------------------------------


def test_a_flow_reports_pending_then_connected_and_expires_on_time() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        started = await service.authorize(workspace, ALICE, "slack", key="mail")
        pending = await service.flow(workspace, started.flow_id)
        assert (pending.status, pending.connection_id, pending.key, pending.user) == ("pending", None, "mail", ALICE)

        state = started.authorization_url.rsplit("state=", 1)[1]
        account, _ = await service.complete(state=state, code="good-code")
        done = await service.flow(workspace, started.flow_id)
        assert (done.status, done.connection_id) == ("connected", account.id)

        # A flow nobody finished reads as expired once its link has died,
        # without anything having to sweep it.
        stale = await service.authorize(workspace, ALICE, "slack")
        row = (
            await session.execute(
                select(ConnectedAccountOAuthState).where(ConnectedAccountOAuthState.flow_id == stale.flow_id)
            )
        ).scalar_one()
        row.created_at = datetime.now(UTC) - timedelta(minutes=11)
        await session.commit()
        assert (await service.flow(workspace, stale.flow_id)).status == "expired"

        # Another workspace cannot ask about this one's flows.
        with pytest.raises(ConnectedAccountFlowNotFoundError):
            await service.flow(await make_workspace(session), started.flow_id)

    run(scenario)


def test_a_denied_consent_is_recorded_on_the_flow() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        started = await service.authorize(workspace, ALICE, "slack", return_url="https://myapp.test/back")
        state = started.authorization_url.rsplit("state=", 1)[1]
        assert await service.return_url_for_state(state, failure="access_denied") == "https://myapp.test/back"
        flow = await service.flow(workspace, started.flow_id)
        assert (flow.status, flow.reason) == ("failed", "access_denied")

    run(scenario)


# -- dead credentials -----------------------------------------------------------


def test_a_rejected_credential_recovers_through_one_forced_refresh() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        account = await _authorize_and_complete(service, workspace, ALICE, "slack")
        # The expiry says the token is fine; the provider disagrees.
        reported = await service.report_rejected(workspace, ALICE, account.id, reason="slack: token_revoked")
        assert reported.status == "active"
        assert service.fake is not None and service.fake.refreshed == ["refresh-1"]
        assert (await service.access_token(workspace, ALICE, account.id)).token == "refreshed-access"

    run(scenario)


def test_a_credential_that_cannot_be_refreshed_is_marked_for_reconnection() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        account = await _authorize_and_complete(service, workspace, ALICE, "slack")
        assert service.fake is not None
        FakeOAuthClient.refuse_refresh = True
        try:
            reported = await service.report_rejected(workspace, ALICE, account.id, reason="slack: token_revoked")
            assert reported.status == "needs_reauth" and reported.invalid_reason == "slack: token_revoked"
            # The row stays, so the application can see it and prompt, and a
            # listing carries the reason rather than a hole where a
            # connection used to be.
            listed = await service.list_accounts(workspace, ALICE)
            assert listed.count == 1 and listed.data[0].status == "needs_reauth"

            # A refusal on the ordinary refresh path marks it the same way.
            row = (await session.execute(select(ConnectedAccount))).scalar_one()
            row.status, row.expires_at = "active", datetime.now(UTC) + timedelta(seconds=10)
            await session.commit()
            with pytest.raises(ConnectedAccountExchangeError):
                await service.access_token(workspace, ALICE, account.id)
            assert (await service.get_account(workspace, ALICE, account.id)).status == "needs_reauth"
        finally:
            FakeOAuthClient.refuse_refresh = False

        # Reconnecting brings it back to life.
        again = await _authorize_and_complete(service, workspace, ALICE, "slack")
        assert again.id == account.id and again.status == "active" and again.invalid_reason is None

    run(scenario)


def test_report_rejected_without_a_refresh_token_marks_it_directly() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=TokenSet(access_token="gho_x", scope="repo"), identity=None)
        account = await _authorize_and_complete(service, workspace, ALICE, "github")
        reported = await service.report_rejected(workspace, ALICE, account.id, reason=None)
        assert reported.status == "needs_reauth"
        assert reported.invalid_reason == "the provider rejected the credential"
        assert service.fake is not None and service.fake.refreshed == []

    run(scenario)


# -- deletion -------------------------------------------------------------------


def test_forgetting_a_user_revokes_and_deletes_everything_of_theirs() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        await _authorize_and_complete(service, workspace, ALICE, "slack")
        service._fake_tokens = TokenSet(access_token="gho_x", scope="repo")
        service._fake_identity = IdentityProfile(provider="github", subject="gh-1", username="alice")
        await _authorize_and_complete(service, workspace, ALICE, "github")
        shared = await _authorize_and_complete(service, workspace, BOB, "slack", shared=True)

        removed = await service.forget_user(workspace, ALICE)
        assert removed == 2
        assert service.fake is not None and "gho_x" in service.fake.revoked
        assert (await session.execute(select(EndUser).where(EndUser.external_id == ALICE))).scalar_one_or_none() is None
        # Her pending flows went too — explicitly, not by trusting a cascade
        # SQLite would not run. The workspace's shared connection stayed: it
        # is not hers to delete, and Bob's flow row belongs to him.
        left = (await session.execute(select(ConnectedAccountOAuthState))).scalars().all()
        assert [row.provider for row in left] == ["slack"]
        assert (await service.list_accounts(workspace, BOB)).data[0].id == shared.id
        assert await service.forget_user(workspace, "never-seen") == 0

    run(scenario)


# -- encryption at rest ---------------------------------------------------------


def test_tokens_are_encrypted_under_the_rows_own_key() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        account = await _authorize_and_complete(service, workspace, ALICE, "slack")
        row = (await session.execute(select(ConnectedAccount))).scalar_one()

        # The row carries its own wrapped key, and its ciphertext cannot be
        # read with the deployment key: that key opens the envelope, not the
        # credential. One disclosed row key is worth one account.
        assert row.encrypted_data_key is not None
        with pytest.raises(SecretDecryptionError):
            decrypt_secret(row.encrypted_access_token)
        data_key = decrypt_secret(row.encrypted_data_key)
        assert decrypt_under(row.encrypted_access_token, data_key) == "xoxb-bot"

        # Two accounts never share an envelope, and a refresh re-keys the row.
        service._fake_identity = IdentityProfile(provider="slack", subject="U2", name="Bob")
        other = await _authorize_and_complete(service, workspace, BOB, "slack")
        rows = {r.id: r for r in (await session.execute(select(ConnectedAccount))).scalars()}
        assert rows[account.id].encrypted_data_key != rows[other.id].encrypted_data_key
        before = rows[account.id].encrypted_data_key
        row = rows[account.id]
        row.expires_at = datetime.now(UTC) + timedelta(seconds=10)
        await session.commit()
        refreshed = await service.access_token(workspace, ALICE, account.id)
        assert refreshed.token == "refreshed-access"
        await session.refresh(row)
        assert row.encrypted_data_key != before
        # The kept refresh token was re-encrypted under the new key, not left
        # behind under the old one.
        assert (await service.access_token(workspace, ALICE, account.id)).token == "refreshed-access"

    run(scenario)


def test_a_row_written_before_the_envelope_is_still_readable() -> None:
    """Rows encrypted with the deployment key directly keep working, and re-envelope on write."""

    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        account = await _authorize_and_complete(service, workspace, ALICE, "slack")
        row = (await session.execute(select(ConnectedAccount))).scalar_one()
        row.encrypted_data_key = None
        row.encrypted_access_token = encrypt_secret("legacy-token")
        row.encrypted_refresh_token = encrypt_secret("legacy-refresh")
        row.encrypted_extra_tokens = encrypt_secret(json.dumps({"user": "legacy-user"}))
        row.expires_at = None
        await session.commit()

        token = await service.access_token(workspace, ALICE, account.id)
        assert token.token == "legacy-token" and token.extra == {"user": "legacy-user"}

        row.expires_at = datetime.now(UTC) + timedelta(seconds=10)
        await session.commit()
        assert (await service.access_token(workspace, ALICE, account.id)).token == "refreshed-access"
        await session.refresh(row)
        assert row.encrypted_data_key is not None  # re-enveloped on the way through

    run(scenario)


# -- what a consumer needs to act ----------------------------------------------


def test_a_token_carries_the_account_and_both_slack_scope_sets() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        tokens = TokenSet(
            access_token="xoxb-bot",
            refresh_token="refresh-1",
            expires_in=3600,
            scope="chat:write,channels:read",
            metadata={"authed_user": {"id": "U1", "access_token": "xoxp-user", "scope": "search:read,files:write"}},
        )
        service = Service(session, configured(), tokens=tokens, identity=SLACK_IDENTITY)
        account = await _authorize_and_complete(service, workspace, ALICE, "slack", key="chat")
        token = await service.access_token(workspace, ALICE, account.id)

        # Everything a Slack consumer needs in one answer: which token is
        # which, what each may do, and the workspace the pair belongs to.
        assert (token.connection_id, token.key, token.provider) == (account.id, "chat", "slack")
        assert token.token == "xoxb-bot" and token.extra["user"] == "xoxp-user"
        assert token.scopes == ["chat:write", "channels:read"]
        assert token.extra_scopes == {"user": ["search:read", "files:write"]}
        assert token.account_identifier == "U1"
        assert token.account_metadata["tenancy_id"] == "T001"
        assert token.account_metadata["tenancy_name"] == "Acme Corp"
        assert account.extra_scopes == {"user": ["search:read", "files:write"]}
        assert account.account_metadata["tenancy_name"] == "Acme Corp"

    run(scenario)


# -- importing a grant obtained elsewhere ---------------------------------------


def test_an_imported_grant_behaves_like_a_connected_one() -> None:
    """What a migration off an application's own OAuth needs: hand over, then use normally."""

    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        imported = await service.import_grant(
            workspace,
            "slack",
            ImportRequest(
                user=ALICE,
                key="chat",
                access_token="xoxb-legacy",
                refresh_token="refresh-legacy",
                extra_tokens={"user": "xoxp-legacy"},
                extra_scopes={"user": ["search:read"]},
                scopes=["chat:write", "channels:read"],
                account_identifier="T001",
                account_label="Acme Corp",
                account_metadata={"tenancy_id": "T001", "tenancy_name": "Acme Corp"},
                label="Work Slack",
                expires_at=datetime.now(UTC) + timedelta(days=30),
            ),
        )
        assert (imported.provider, imported.key, imported.account_identifier) == ("slack", "chat", "T001")
        assert imported.label == "Work Slack" and imported.status == "active"

        # An ordinary connection from here on: resolvable, encrypted under its
        # own key, and carrying both Slack tokens with their scopes apart.
        token = await service.access_token_for_provider(workspace, ALICE, "slack", key="chat")
        assert token is not None
        assert token.token == "xoxb-legacy" and token.extra == {"user": "xoxp-legacy"}
        assert token.extra_scopes == {"user": ["search:read"]}
        assert token.account_metadata["tenancy_name"] == "Acme Corp"
        row = (await session.execute(select(ConnectedAccount))).scalar_one()
        assert row.encrypted_data_key is not None
        with pytest.raises(SecretDecryptionError):
            decrypt_secret(row.encrypted_access_token)

        # Running the backfill twice updates the same row rather than adding one.
        again = await service.import_grant(
            workspace,
            "slack",
            ImportRequest(user=ALICE, key="chat", access_token="xoxb-second", account_identifier="T001"),
        )
        assert again.id == imported.id
        assert (await service.list_accounts(workspace, ALICE)).count == 1
        assert (await service.access_token(workspace, ALICE, imported.id)).token == "xoxb-second"

    run(scenario)


def test_an_imported_grant_can_be_shared_and_refuses_an_unconfigured_app() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        shared = await service.import_grant(
            workspace,
            "slack",
            ImportRequest(user=ALICE, shared=True, access_token="xoxb-team", account_identifier="T001"),
        )
        assert (shared.owner, shared.connected_by) == ("workspace", ALICE)
        assert (await service.access_token_for_provider(workspace, BOB, "slack")) is not None

        # Nothing may be imported for an app this deployment cannot maintain:
        # a token nobody can refresh or revoke is a trap, not a migration.
        with pytest.raises(ConnectedAppNotConfiguredError):
            await service.import_grant(workspace, "notion", ImportRequest(user=ALICE, access_token="secret"))

    run(scenario)


def test_an_imported_grant_without_a_refresh_token_still_reports_when_it_dies() -> None:
    async def scenario(session: AsyncSession) -> None:
        workspace = await make_workspace(session)
        service = Service(session, configured(), tokens=SLACK_TOKENS, identity=SLACK_IDENTITY)
        imported = await service.import_grant(
            workspace,
            "slack",
            ImportRequest(user=ALICE, access_token="xoxb-norefresh", account_identifier="T001", expires_at=None),
        )
        assert imported.has_refresh_token is False and imported.expires_at is None
        reported = await service.report_rejected(workspace, ALICE, imported.id, reason="slack: token_revoked")
        assert reported.status == "needs_reauth"

    run(scenario)
