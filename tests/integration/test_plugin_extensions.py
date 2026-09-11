"""The plugin seam on a running app: settings, pages, health, enforcement, guardrails, tools, events."""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from any_llm.types.completion import (
    ChatCompletion,
    ChatCompletionMessage,
    ChatCompletionMessageFunctionToolCall,
    Choice,
    CompletionUsage,
    Function,
)
from fastapi.testclient import TestClient

from gateway.core.config import API_KEY_HEADER, API_ROOT, GatewayConfig
from gateway.models.plugins import PluginsConfig

from .conftest import build_test_client

HEADERS = {API_KEY_HEADER: "Bearer test-master-key"}

MANIFEST = """
[plugin]
name = "ext"
version = "0.1.0"
package = "ext_plugin"
contributes = ["traffic", "guardrails", "tools", "events", "lifecycle", "ui"]
[plugin.settings.banned]
type = "str"
default = "force"
[plugin.settings.token]
type = "str"
secret = true
[[plugin.pages]]
id = "runs"
label = "Runs"
section = "build"
parent = "tools"
audience = "member"
[[plugin.pages]]
id = "admin"
label = "Admin"
"""

PACKAGE = """
from gateway.plugins.api import (
    GuardrailOutcome, PluginContext, RequestDecision, ResponseDecision, ToolCallDecision,
)

STATE = {"config": None, "events": [], "started": False}


class Enforcer:
    def on_request(self, event):
        text = event.conversation.latest_user_text
        return RequestDecision(
            inject_system="Plugin says hello.",
            block="refused by plugin" if "forbidden" in text else None,
            annotations={"seen": True},
        )

    def on_tool_call(self, event):
        banned = STATE["config"]["banned"]
        return ToolCallDecision(deny=f"no {banned}" if banned in str(event.tool_call.arguments) else None)

    def on_response(self, event):
        return ResponseDecision(block="answer withheld" if "secret" in event.text else None)


class Guard:
    async def check(self, text, *, direction, kwargs):
        return GuardrailOutcome(valid="pii" not in text, explanation="pii found")


class Echo:
    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
            },
        }
    ]

    def owns_tool(self, name):
        return name == "echo"

    async def call_tool(self, name, arguments):
        return "echo:" + str(arguments.get("text"))

    def purpose_hints(self):
        return [("echo", "Echoes text.")]


def register(ctx: PluginContext) -> None:
    STATE["config"] = ctx.config
    ctx.add_traffic_observer(Enforcer())
    ctx.add_guardrail("pii", Guard())
    ctx.add_tool("echo", Echo)
    ctx.subscribe("usage.logged", lambda event: STATE["events"].append(event.payload))
    ctx.subscribe("plugin.settings_changed", lambda event: STATE["events"].append(event.payload))
    ctx.on_startup(lambda: STATE.__setitem__("started", True))
    ctx.add_health_check(lambda: STATE["started"])
"""


@pytest.fixture
def plugins_dir(tmp_path: Path) -> Generator[Path]:
    directory = tmp_path / "otari-plugins"
    package_dir = directory / "ext" / "ext_plugin"
    package_dir.mkdir(parents=True)
    (package_dir / "otari-plugin.toml").write_text(MANIFEST)
    (package_dir / "__init__.py").write_text(PACKAGE)
    for page in ("static",):
        (package_dir / page).mkdir()
        (package_dir / page / "index.html").write_text("<h1>ext</h1>")
    sys.modules.pop("ext_plugin", None)
    yield directory
    sys.modules.pop("ext_plugin", None)


@pytest.fixture
def ext_client(postgres_url: str, plugins_dir: Path) -> Generator[TestClient]:
    config = GatewayConfig(
        database_url=postgres_url,
        master_key="test-master-key",
        auto_migrate=False,
        require_pricing=False,
        model_discovery=False,
        providers={"anthropic": {"api_key": "sk-ant"}},
        plugins=PluginsConfig(directory=str(plugins_dir)),
    )
    yield from build_test_client(config)


def _state() -> dict[str, Any]:
    state: dict[str, Any] = sys.modules["ext_plugin"].STATE
    return state


def _create_user(client: TestClient) -> None:
    response = client.post(f"{API_ROOT}/users", json={"user_id": "test-user", "alias": "Test User"}, headers=HEADERS)
    assert response.status_code == 200


def _completion(content: str | None = None, *tool_calls: tuple[str, str]) -> ChatCompletion:
    return ChatCompletion(
        id="chatcmpl-1",
        object="chat.completion",
        created=0,
        model="claude-opus-4",
        choices=[
            Choice(
                index=0,
                finish_reason="tool_calls" if tool_calls else "stop",
                message=ChatCompletionMessage(
                    role="assistant",
                    content=content,
                    tool_calls=[
                        ChatCompletionMessageFunctionToolCall(
                            id=f"call_{i}", type="function", function=Function(name=name, arguments=arguments)
                        )
                        for i, (name, arguments) in enumerate(tool_calls)
                    ]
                    or None,
                ),
            )
        ],
        usage=CompletionUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _chat(client: TestClient, text: str, **extra: Any) -> Any:
    return client.post(
        f"{API_ROOT}/chat/completions",
        json={
            "model": "anthropic:claude-opus-4",
            "messages": [{"role": "user", "content": text}],
            "user": "test-user",
            **extra,
        },
        headers=HEADERS,
    )


def test_the_listing_describes_the_new_contributions_and_pages(ext_client: TestClient) -> None:
    response = ext_client.get(f"{API_ROOT}/plugins", headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plugin_api"] == 1
    plugin = body["plugins"][0]
    assert plugin["status"] == "loaded", plugin["error"]
    assert plugin["guardrails"] == ["ext:pii"]
    assert plugin["tools"] == ["ext:echo"]
    assert plugin["events"] == ["plugin.settings_changed", "usage.logged"]
    assert plugin["traffic"] is True
    assert [field["key"] for field in plugin["settings"]] == ["banned", "token"]
    runs, admin = plugin["pages"]
    assert (runs["path"], runs["url"], runs["section"], runs["parent"], runs["audience"]) == (
        "/plugins/ext",
        "/plugins/ext/ui/",
        "build",
        "tools",
        "member",
    )
    assert (admin["path"], admin["url"], admin["section"]) == ("/plugins/ext/admin", "/plugins/ext/ui/admin/", "extend")
    assert ext_client.get("/plugins/ext/ui/admin/").status_code == 200
    assert ext_client.get("/plugins/ext/ui/").text == "<h1>ext</h1>"


def test_the_pages_route_lists_pages_for_the_rail(ext_client: TestClient) -> None:
    response = ext_client.get(f"{API_ROOT}/plugins/pages", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert [page["id"] for page in response.json()["pages"]] == ["runs", "admin"]


def test_health_reports_the_plugin_after_its_startup_hook_ran(ext_client: TestClient) -> None:
    assert _state()["started"] is True
    response = ext_client.get(f"{API_ROOT}/health")
    assert response.status_code == 200
    assert response.json()["plugins"] == {"ext": "ok"}
    ready = ext_client.get(f"{API_ROOT}/health/readiness")
    assert ready.status_code == 200 and ready.json()["plugins"] == {"ext": "ok"}


def test_settings_are_read_changed_and_applied_live(ext_client: TestClient) -> None:
    response = ext_client.get(f"{API_ROOT}/plugins/ext/settings", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["values"] == {"banned": "force", "token": None}

    response = ext_client.put(
        f"{API_ROOT}/plugins/ext/settings", json={"values": {"banned": "rm -rf", "token": "abc"}}, headers=HEADERS
    )
    assert response.status_code == 200, response.text
    assert response.json()["values"] == {"banned": "rm -rf", "token": "********"}
    assert _state()["config"]["banned"] == "rm -rf"
    assert {"plugin": "ext", "keys": ["banned", "token"]} in _state()["events"]

    response = ext_client.put(f"{API_ROOT}/plugins/ext/settings", json={"values": {"banned": None}}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["values"]["banned"] == "force"

    response = ext_client.put(f"{API_ROOT}/plugins/ext/settings", json={"values": {"banned": 3}}, headers=HEADERS)
    assert response.status_code == 422
    response = ext_client.put(f"{API_ROOT}/plugins/ext/settings", json={"values": {"nope": 3}}, headers=HEADERS)
    assert response.status_code == 422


def test_a_plugin_blocks_a_request_before_the_provider(ext_client: TestClient) -> None:
    _create_user(ext_client)
    calls: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> ChatCompletion:
        calls.append(kwargs)
        return _completion("fine")

    with patch("gateway.api.routes.chat.acompletion", new=fake_acompletion):
        response = _chat(ext_client, "do the forbidden thing")
    assert response.status_code == 403, response.text
    assert "refused by plugin" in response.text
    assert calls == []


def test_a_plugin_injects_system_text_and_denies_a_tool_call(ext_client: TestClient) -> None:
    _create_user(ext_client)
    calls: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> ChatCompletion:
        calls.append(kwargs)
        return _completion(None, ("Bash", '{"command": "git push --force"}'), ("Bash", '{"command": "ls"}'))

    with patch("gateway.api.routes.chat.acompletion", new=fake_acompletion):
        response = _chat(ext_client, "push it")
    assert response.status_code == 200, response.text
    assert calls[0]["messages"][0] == {"role": "system", "content": "Plugin says hello."}
    message = response.json()["choices"][0]["message"]
    assert [call["id"] for call in message["tool_calls"]] == ["call_1"]
    assert "no force" in message["content"]


def test_a_plugin_withholds_a_blocked_answer_after_paying_for_it(ext_client: TestClient) -> None:
    _create_user(ext_client)

    async def fake_acompletion(**kwargs: Any) -> ChatCompletion:
        return _completion("the secret is 42")

    with patch("gateway.api.routes.chat.acompletion", new=fake_acompletion):
        response = _chat(ext_client, "tell me")
    assert response.status_code == 403, response.text
    assert "answer withheld" in response.text
    usage = ext_client.get(f"{API_ROOT}/usage", headers=HEADERS).json()
    assert usage and usage[0]["plugin_annotations"]["ext"]["blocked_response"] == "answer withheld"


def test_a_plugin_guardrail_profile_blocks_like_a_service_one(ext_client: TestClient) -> None:
    _create_user(ext_client)

    async def fake_acompletion(**kwargs: Any) -> ChatCompletion:
        return _completion("fine")

    with patch("gateway.api.routes.chat.acompletion", new=fake_acompletion):
        blocked = _chat(ext_client, "here is pii", guardrails=[{"profile": "ext:pii", "mode": "block"}])
        allowed = _chat(ext_client, "clean", guardrails=[{"profile": "ext:pii", "mode": "block"}])
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["detail"]["guardrails"][0]["profile"] == "ext:pii"
    assert allowed.status_code == 200, allowed.text
    assert '"profile":"ext:pii"' in allowed.headers["x-otari-guardrails"]


def test_a_plugin_tool_runs_in_the_gateway_tool_loop(ext_client: TestClient) -> None:
    _create_user(ext_client)
    calls: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> ChatCompletion:
        calls.append(kwargs)
        if len(calls) == 1:
            return _completion(None, ("echo", '{"text": "hi"}'))
        return _completion("done")

    with (
        patch("gateway.api.routes.chat.acompletion", new=fake_acompletion),
        patch("gateway.services.mcp_loop.acompletion", new=fake_acompletion),
    ):
        response = _chat(ext_client, "echo hi", tools=[{"type": "plugin", "name": "ext:echo"}])
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "done"
    assert len(calls) == 2
    names = {tool.get("function", {}).get("name") or tool.get("name") for tool in calls[0]["tools"]}
    assert "echo" in names
    assert any(m.get("role") == "tool" and "echo:hi" in str(m.get("content")) for m in calls[1]["messages"])

    unknown = _chat(ext_client, "x", tools=[{"type": "plugin", "name": "ext:nope"}])
    assert unknown.status_code == 400


def test_usage_events_reach_a_subscribed_plugin(ext_client: TestClient) -> None:
    _create_user(ext_client)
    _state()["events"].clear()

    async def fake_acompletion(**kwargs: Any) -> ChatCompletion:
        return _completion("fine")

    with patch("gateway.api.routes.chat.acompletion", new=fake_acompletion):
        response = _chat(ext_client, "hello")
    assert response.status_code == 200
    # Handlers run as tasks on the app's loop; the next request gives them a turn.
    ext_client.get(f"{API_ROOT}/health")
    logged = [e for e in _state()["events"] if "model" in e]
    assert logged and logged[0]["model"] == "claude-opus-4"
    assert logged[0]["plugin_annotations"]["ext"]["seen"] is True
