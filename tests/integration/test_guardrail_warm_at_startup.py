"""Warming the defined guardrails at boot, instead of on the first request to use one.

Lazy building was never a preference. Until a guardrail could be written down,
this process could not name the profiles a deployment had, so the first request
to use one was the only thing that could ask for it to be built. The
``guardrails:`` block and the ``guardrail_credentials`` table changed that, and
these cover what startup does with them.

``AnyGuardrail`` is stubbed at the name the runner imported, so nothing here
builds a real guardrail or reaches a vendor.
"""

import time
from collections.abc import Callable, Generator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.core.config import API_ROOT, GatewayConfig
from gateway.services.guardrail_runner import reset_guardrail_runner
from gateway.services.secret_box import generate_secret_key

from .conftest import build_test_client


@pytest.fixture(autouse=True)
def _clean_runner() -> Iterator[None]:
    reset_guardrail_runner()
    yield
    reset_guardrail_runner()


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTARI_SECRET_KEY", generate_secret_key())


class _Guardrail:
    """Stand-in for a built guardrail."""


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every construction, by the ``any_guardrail`` class asked for."""
    names: list[str] = []

    def _create(name: Any, **_kwargs: Any) -> _Guardrail:
        names.append(str(getattr(name, "value", name)))
        return _Guardrail()

    class _Stub:
        create = staticmethod(_create)

        @staticmethod
        def evaluate(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("warming must not call a guardrail")

    monkeypatch.setattr("gateway.services.guardrail_runner.AnyGuardrail", _Stub)
    return names


def _config(postgres_url: str, **overrides: Any) -> GatewayConfig:
    return GatewayConfig(
        database_url=postgres_url,
        master_key="test-master-key",
        host="127.0.0.1",
        port=8000,
        auto_migrate=False,
        require_pricing=False,
        **overrides,
    )


def _client(config: GatewayConfig) -> Generator[TestClient]:
    yield from build_test_client(config)


def _settled(done: Callable[[], bool], *, seconds: float = 5.0) -> bool:
    """Wait for the warm task, which startup creates and deliberately does not await.

    The loop runs in the client's own thread, so sleeping here is what lets it
    make progress. Polled rather than slept once, so the test is not a guess about
    how long a stubbed constructor takes.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if done():
            return True
        time.sleep(0.02)
    return done()


def test_builds_a_config_file_guardrail_at_startup(postgres_url: str, built: list[str]) -> None:
    """The first request to name the profile no longer pays for its construction."""
    config = _config(
        postgres_url,
        guardrails={"from-file": {"guardrail_name": "lakera_guard", "create_kwargs": {"api_key": "k"}}},
    )

    for _client_ in _client(config):
        assert _settled(lambda: built == ["lakera_guard"]), built


def test_skips_a_disabled_guardrail(postgres_url: str, built: list[str]) -> None:
    """Off means off: warming it would hold a client for a profile nothing may use."""
    config = _config(
        postgres_url,
        guardrails={
            "on": {"guardrail_name": "lakera_guard", "create_kwargs": {"api_key": "k"}},
            "off": {"guardrail_name": "any_llm", "enabled": False},
        },
    )

    for _client_ in _client(config):
        assert _settled(lambda: built == ["lakera_guard"]), built


def test_one_definition_that_will_not_build_does_not_stop_the_gateway(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warming is a head start, never a gate: the lazy path still serves that profile.

    The first guardrail raises and the second is built anyway, so a failure is
    per profile rather than the end of the pass.
    """
    attempted: list[str] = []

    def _create(name: Any, **_kwargs: Any) -> Any:
        value = str(getattr(name, "value", name))
        attempted.append(value)
        if value == "lakera_guard":
            raise RuntimeError("vendor rejected the key")
        return _Guardrail()

    class _Stub:
        create = staticmethod(_create)

    monkeypatch.setattr("gateway.services.guardrail_runner.AnyGuardrail", _Stub)
    config = _config(
        postgres_url,
        guardrails={
            "broken": {"guardrail_name": "lakera_guard", "create_kwargs": {"api_key": "k"}},
            "fine": {"guardrail_name": "any_llm"},
        },
    )

    for client in _client(config):
        assert client.get(f"{API_ROOT}/health").status_code == 200
        assert _settled(lambda: sorted(attempted) == ["any_llm", "lakera_guard"]), attempted


def test_a_stored_guardrail_wins_over_the_config_entry_of_the_same_name(
    postgres_url: str, built: list[str], master_key_header: dict[str, str]
) -> None:
    """The store's precedence rule, applied to what gets built rather than what is listed."""
    config = _config(
        postgres_url,
        guardrails={"shared": {"guardrail_name": "lakera_guard", "create_kwargs": {"api_key": "k"}}},
    )

    for client in _client(config):
        assert client.post(
            "/api/v1/guardrail-credentials",
            json={"name": "shared", "guardrail_name": "any_llm", "create_kwargs": {}},
            headers=master_key_header,
        ).status_code == 201

    built.clear()
    for _client_ in _client(config):
        assert _settled(lambda: built == ["any_llm"]), built


def test_warming_builds_nothing_when_no_guardrail_is_defined(postgres_url: str, built: list[str]) -> None:
    """The ordinary deployment. Warming must cost it nothing at all."""
    for client in _client(_config(postgres_url)):
        assert client.get(f"{API_ROOT}/health").status_code == 200
        assert _settled(lambda: built != [], seconds=0.3) is False
