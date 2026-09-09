"""Endpoint tests for /v1/connections and the hosted OAuth callback."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from apron_auth.models import IdentityProfile, OAuthPendingState, TokenSet
from fastapi.testclient import TestClient

from gateway.api.deps import reset_config
from gateway.core.config import GatewayConfig
from gateway.core.database import reset_db
from gateway.main import create_app
from gateway.services.secret_box import generate_secret_key
from gateway.services.tenancy import connected_account_service as svc

AUTH = {"Authorization": "Bearer sk-test-master"}
USER = "alice@acme.test"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OTARI_SECRET_KEY", generate_secret_key())
    yield
    reset_config()
    reset_db()


class _FakeClient:
    def __init__(self, config: Any, state_store: Any = None, **_: Any) -> None:
        self.store = state_store

    async def get_authorization_url(
        self, redirect_uri: str | None = None, metadata: dict[str, Any] | None = None
    ) -> tuple[str, OAuthPendingState]:
        pending = OAuthPendingState(
            state=uuid.uuid4().hex,
            redirect_uri="https://otari.example.com/cb",
            code_verifier="v",
            created_at=time.time(),
            metadata=metadata or {},
        )
        await self.store.save(pending)
        return f"https://github.com/login/oauth/authorize?state={pending.state}", pending

    async def exchange_code(self, code: str, state: str | None = None, **_: Any) -> TokenSet:
        assert state and await self.store.consume(state) is not None
        return TokenSet(access_token="gho_secret", scope="repo read:user")

    async def fetch_identity(self, tokens: TokenSet) -> IdentityProfile:
        return IdentityProfile(provider="github", username="octocat", name="Octo Cat")

    async def revoke_token(self, token: str) -> bool:
        return True


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> TestClient:
    monkeypatch.setattr(svc, "OAuthClient", _FakeClient)
    config = GatewayConfig(
        database_url=f"sqlite:///{tmp_path / 'connections.db'}",
        master_key="sk-test-master",
        public_base_url="https://otari.example.com",
        connected_apps={"github": {"client_id": "id", "client_secret": "secret", "scopes": ["repo"]}},
        **overrides,
    )
    return TestClient(create_app(config))


def test_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/v1/connections", params={"user": USER}).status_code == 401
        assert client.get("/v1/connections/apps").status_code == 401


def test_full_flow_over_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        apps = client.get("/v1/connections/apps", headers=AUTH, params={"user": USER}).json()
        assert [app["provider"] for app in apps] == ["github"]
        assert apps[0]["connected_accounts"] == 0 and "repo" in apps[0]["scopes"]

        started = client.post(
            "/v1/connections/github/authorize",
            headers=AUTH,
            json={"user": USER, "return_url": "https://myapp.test/settings?tab=apps"},
        )
        assert started.status_code == 200, started.text
        url = started.json()["authorization_url"]
        assert url.startswith("https://github.com/login/oauth/authorize")
        state = parse_qs(urlsplit(url).query)["state"][0]

        # The provider sends the user's browser back; no credential on that request.
        landed = client.get(f"/connected-accounts/github/callback?code=abc&state={state}", follow_redirects=False)
        assert landed.status_code == 302
        location = urlsplit(landed.headers["location"])
        query = parse_qs(location.query)
        assert (location.scheme, location.netloc, location.path) == ("https", "myapp.test", "/settings")
        assert query["tab"] == ["apps"] and query["connection"] == ["ok"] and query["provider"] == ["github"]
        connection_id = query["connection_id"][0]

        listed = client.get("/v1/connections", headers=AUTH, params={"user": USER}).json()
        assert listed["count"] == 1 and listed["data"][0]["account_identifier"] == "octocat"
        assert "gho_secret" not in client.get("/v1/connections", headers=AUTH, params={"user": USER}).text

        token = client.get("/v1/connections/github/token", headers=AUTH, params={"user": USER}).json()
        assert token["token"] == "gho_secret" and token["provider"] == "github"
        assert client.get("/v1/connections/github/token", headers=AUTH, params={"user": "nobody"}).status_code == 404

        patched = client.patch(
            f"/v1/connections/{connection_id}", headers=AUTH, params={"user": USER}, json={"label": "Work"}
        )
        assert patched.json()["label"] == "Work"
        assert client.get(f"/v1/connections/{connection_id}", headers=AUTH, params={"user": "bob"}).status_code == 404
        assert client.delete(f"/v1/connections/{connection_id}", headers=AUTH, params={"user": USER}).status_code == 204
        assert client.get(f"/v1/connections/{connection_id}", headers=AUTH, params={"user": USER}).status_code == 404

        # A reused state lands on the return page with an error rather than a raw 400.
        reused = client.get(f"/connected-accounts/github/callback?code=abc&state={state}", follow_redirects=False)
        assert parse_qs(urlsplit(reused.headers["location"]).query)["connection"] == ["error"]
        assert parse_qs(urlsplit(reused.headers["location"]).query)["reason"] == ["stateinvalid"]


def test_provider_error_and_bad_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        started = client.post("/v1/connections/github/authorize", headers=AUTH, json={"user": USER}).json()
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
        denied = client.get(
            f"/connected-accounts/github/callback?error=access_denied&state={state}", follow_redirects=False
        )
        assert denied.status_code == 302
        location = urlsplit(denied.headers["location"])
        # No return_url was given, so the browser lands on this deployment's own hash-routed page,
        # with the outcome inside the fragment where a hash router reads it.
        assert (location.netloc, location.path) == ("otari.example.com", "/")
        route, _, fragment_query = location.fragment.partition("?")
        assert route == "/connections"
        query = parse_qs(fragment_query)
        assert query["connection"] == ["error"] and query["reason"] == ["access_denied"]

        assert client.post("/v1/connections/slack/authorize", headers=AUTH, json={"user": USER}).status_code == 400
        assert client.post("/v1/connections/myspace/authorize", headers=AUTH, json={"user": USER}).status_code == 422
        bad_return = client.post(
            "/v1/connections/github/authorize", headers=AUTH, json={"user": USER, "return_url": "http://evil.test/"}
        )
        assert bad_return.status_code == 400
        assert client.post("/v1/connections/github/authorize", headers=AUTH, json={}).status_code == 422


def test_flow_polling_over_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An application that opened a popup asks how the flow ended, not for the state."""
    with _client(tmp_path, monkeypatch) as client:
        started = client.post(
            "/v1/connections/github/authorize", headers=AUTH, json={"user": USER, "key": "issues"}
        ).json()
        flow_id = started["flow_id"]
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]

        pending = client.get(f"/v1/connections/flows/{flow_id}", headers=AUTH).json()
        assert (pending["status"], pending["key"], pending["user"]) == ("pending", "issues", USER)
        assert pending["connection_id"] is None

        client.get(f"/connected-accounts/github/callback?code=abc&state={state}", follow_redirects=False)
        done = client.get(f"/v1/connections/flows/{flow_id}", headers=AUTH).json()
        assert done["status"] == "connected" and done["connection_id"] is not None

        # The flow id is not a credential: it says how things went and nothing more.
        assert "token" not in done and state not in client.get(
            f"/v1/connections/flows/{flow_id}", headers=AUTH
        ).text
        assert client.get(f"/v1/connections/flows/{uuid.uuid4()}", headers=AUTH).status_code == 404
        assert client.get(f"/v1/connections/flows/{flow_id}").status_code == 401


def test_a_named_connection_answers_only_for_its_own_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        started = client.post("/v1/connections/github/authorize", headers=AUTH, json={"user": USER}).json()
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
        landed = client.get(f"/connected-accounts/github/callback?code=abc&state={state}", follow_redirects=False)
        connection_id = parse_qs(urlsplit(landed.headers["location"]).fragment.partition("?")[2])["connection_id"][0]

        named = client.get(
            "/v1/connections/github/token", headers=AUTH, params={"user": USER, "connection_id": connection_id}
        )
        assert named.status_code == 200 and named.json()["connection_id"] == connection_id
        # Another app's endpoint must not serve it, even with the right id.
        assert (
            client.get(
                "/v1/connections/slack/token", headers=AUTH, params={"user": USER, "connection_id": connection_id}
            ).status_code
            == 404
        )
        # Someone else's user cannot name it either.
        assert (
            client.get(
                "/v1/connections/github/token", headers=AUTH, params={"user": "bob", "connection_id": connection_id}
            ).status_code
            == 404
        )


def test_reporting_a_rejected_credential_over_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        started = client.post("/v1/connections/github/authorize", headers=AUTH, json={"user": USER}).json()
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
        landed = client.get(f"/connected-accounts/github/callback?code=abc&state={state}", follow_redirects=False)
        connection_id = parse_qs(urlsplit(landed.headers["location"]).fragment.partition("?")[2])["connection_id"][0]

        # The fake issues no refresh token, so there is nothing to recover with.
        reported = client.post(
            f"/v1/connections/{connection_id}/rejected",
            headers=AUTH,
            params={"user": USER},
            json={"reason": "github: bad_credentials"},
        )
        assert reported.status_code == 200, reported.text
        assert reported.json()["status"] == "needs_reauth"
        assert reported.json()["invalid_reason"] == "github: bad_credentials"

        listed = client.get("/v1/connections", headers=AUTH, params={"user": USER}).json()
        assert listed["data"][0]["status"] == "needs_reauth"
        assert (
            client.post(f"/v1/connections/{uuid.uuid4()}/rejected", headers=AUTH, params={"user": USER}).status_code
            == 404
        )


def test_forgetting_a_user_over_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        started = client.post("/v1/connections/github/authorize", headers=AUTH, json={"user": USER}).json()
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
        client.get(f"/connected-accounts/github/callback?code=abc&state={state}", follow_redirects=False)
        assert client.get("/v1/connections", headers=AUTH, params={"user": USER}).json()["count"] == 1

        assert client.delete("/v1/connections/user", headers=AUTH, params={"user": USER}).status_code == 204
        assert client.get("/v1/connections", headers=AUTH, params={"user": USER}).json()["count"] == 0
        # Idempotent: a user with nothing left, or one never seen, is not an error.
        assert client.delete("/v1/connections/user", headers=AUTH, params={"user": USER}).status_code == 204
        assert client.delete("/v1/connections/user", headers=AUTH, params={"user": "never-seen"}).status_code == 204
        assert client.delete("/v1/connections/user", params={"user": USER}).status_code == 401


def test_shared_connections_over_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        started = client.post(
            "/v1/connections/github/authorize", headers=AUTH, json={"user": USER, "shared": True}
        ).json()
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
        client.get(f"/connected-accounts/github/callback?code=abc&state={state}", follow_redirects=False)

        # A user who connected nothing resolves the workspace's credential.
        listed = client.get("/v1/connections", headers=AUTH, params={"user": "bob"}).json()
        assert listed["count"] == 1
        assert (listed["data"][0]["owner"], listed["data"][0]["user"]) == ("workspace", None)
        assert listed["data"][0]["connected_by"] == USER
        assert client.get("/v1/connections/github/token", headers=AUTH, params={"user": "bob"}).status_code == 200
        excluded = client.get(
            "/v1/connections", headers=AUTH, params={"user": "bob", "include_shared": False}
        ).json()
        assert excluded["count"] == 0


def test_import_takes_the_master_key_not_an_application_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An application that could import grants could plant one for a user who never consented."""
    with _client(tmp_path, monkeypatch) as client:
        created = client.post("/v1/keys", headers=AUTH, json={"name": "app"})
        assert created.status_code in (200, 201), created.text
        api_key = created.json().get("key") or created.json().get("api_key")
        body = {"user": USER, "access_token": "gho_imported", "account_identifier": "octocat"}

        refused = client.post(
            "/v1/connections/github/import", headers={"Authorization": f"Bearer {api_key}"}, json=body
        )
        assert refused.status_code in (401, 403), refused.text
        assert client.post("/v1/connections/github/import", json=body).status_code in (401, 403)

        imported = client.post("/v1/connections/github/import", headers=AUTH, json=body)
        assert imported.status_code == 200, imported.text
        assert imported.json()["account_identifier"] == "octocat"

        # From here it is an ordinary connection for the application's own key.
        listed = client.get("/v1/connections", headers={"Authorization": f"Bearer {api_key}"}, params={"user": USER})
        assert listed.json()["count"] == 1
        token = client.get(
            "/v1/connections/github/token", headers={"Authorization": f"Bearer {api_key}"}, params={"user": USER}
        )
        assert token.json()["token"] == "gho_imported"
