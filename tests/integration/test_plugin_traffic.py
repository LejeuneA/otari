"""The traffic seam, end to end on a running app.

A directory plugin registers a traffic observer. Real requests then go through
the chat route with the provider faked, and the usage rows the gateway writes
are read back through the usage API: the observer's annotations are on them,
for a non-streaming answer with a tool call and for a streamed one whose tool
call arrives in fragments.
"""

import sys
from collections.abc import AsyncIterator, Generator
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import pytest
from any_llm.types.completion import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessage,
    ChatCompletionMessageFunctionToolCall,
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
    ChunkChoice,
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
name = "watcher"
version = "0.1.0"
package = "watcher_plugin"
contributes = ["traffic"]
"""

PACKAGE = """
from gateway.plugins import PluginContext
from gateway.plugins.traffic import RequestDecision, ToolCallDecision


class Watcher:
    def on_request(self, event):
        turns = event.conversation.turns
        return RequestDecision(
            annotations={
                "api": event.conversation.api,
                "session": event.conversation.session_key,
                "prior_tool_calls": sum(len(turn.tool_calls) for turn in turns),
                "workspace": event.caller.workspace_id is not None,
            }
        )

    async def on_tool_call(self, event):
        command = event.tool_call.arguments.get("command", "")
        deny = "no force-push" if "--force" in command else None
        return ToolCallDecision(deny=deny, annotations={"tools": [event.tool_call.name]})


def register(ctx: PluginContext) -> None:
    ctx.add_traffic_observer(Watcher())
"""


@pytest.fixture
def plugins_dir(tmp_path: Path) -> Generator[Path]:
    directory = tmp_path / "otari-plugins"
    package_dir = directory / "watcher" / "watcher_plugin"
    package_dir.mkdir(parents=True)
    (package_dir / "otari-plugin.toml").write_text(MANIFEST)
    (package_dir / "__init__.py").write_text(PACKAGE)
    sys.modules.pop("watcher_plugin", None)
    yield directory
    sys.modules.pop("watcher_plugin", None)


@pytest.fixture
def watched_client(postgres_url: str, plugins_dir: Path) -> Generator[TestClient]:
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


def _create_user(client: TestClient) -> None:
    response = client.post(f"{API_ROOT}/users", json={"user_id": "test-user", "alias": "Test User"}, headers=HEADERS)
    assert response.status_code == 200


def _latest_usage(client: TestClient) -> dict[str, Any]:
    response = client.get(f"{API_ROOT}/usage", headers=HEADERS)
    assert response.status_code == 200
    rows = response.json()
    assert rows, "no usage row was written"
    row: dict[str, Any] = rows[0]
    return row


def _completion_with_tool_call(command: str) -> ChatCompletion:
    return ChatCompletion(
        id="chatcmpl-1",
        object="chat.completion",
        created=0,
        model="claude-opus-4",
        choices=[
            Choice(
                index=0,
                finish_reason="tool_calls",
                message=ChatCompletionMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ChatCompletionMessageFunctionToolCall(
                            id="call_1",
                            type="function",
                            function=Function(name="Bash", arguments=f'{{"command": "{command}"}}'),
                        )
                    ],
                ),
            )
        ],
        usage=CompletionUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


CONVERSATION = [
    {"role": "user", "content": "push the branch"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c0", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "git status"}'}}
        ],
    },
    {"role": "tool", "tool_call_id": "c0", "content": "clean"},
    {"role": "user", "content": "go ahead"},
]


@pytest.mark.asyncio
async def test_an_observer_annotates_the_usage_row_of_a_completion(watched_client: TestClient) -> None:
    _create_user(watched_client)

    async def fake_acompletion(**kwargs: Any) -> ChatCompletion:
        return _completion_with_tool_call("git push --force origin main")

    with patch("gateway.api.routes.chat.acompletion", new=fake_acompletion):
        response = watched_client.post(
            f"{API_ROOT}/chat/completions",
            json={
                "model": "anthropic:claude-opus-4",
                "messages": CONVERSATION,
                "user": "test-user",
                "session_label": "s-42",
            },
            headers=HEADERS,
        )

    assert response.status_code == 200, response.text
    # Phase 1 records, never alters: the tool call reaches the client untouched.
    assert response.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "Bash"
    row = _latest_usage(watched_client)
    assert row["plugin_annotations"] == {
        "watcher": {
            "api": "chat",
            "session": "s-42",
            "prior_tool_calls": 1,
            "workspace": True,
            "tools": ["Bash"],
            "would_deny": [{"tool_call_id": "call_1", "message": "no force-push"}],
        }
    }


@pytest.mark.asyncio
async def test_an_observer_sees_a_streamed_tool_call_once_it_is_whole(watched_client: TestClient) -> None:
    _create_user(watched_client)

    def chunk(
        delta: ChoiceDelta, finish: Literal["stop", "tool_calls"] | None = None, usage: CompletionUsage | None = None
    ) -> ChatCompletionChunk:
        return ChatCompletionChunk(
            id="chatcmpl-2",
            object="chat.completion.chunk",
            created=0,
            model="claude-opus-4",
            choices=[ChunkChoice(index=0, delta=delta, finish_reason=finish)],
            usage=usage,
        )

    async def chunk_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield chunk(ChoiceDelta(role="assistant", content="Running "))
        yield chunk(
            ChoiceDelta(
                tool_calls=[
                    ChoiceDeltaToolCall(
                        index=0, id="call_s", function=ChoiceDeltaToolCallFunction(name="Bash", arguments="")
                    )
                ]
            )
        )
        yield chunk(
            ChoiceDelta(
                tool_calls=[
                    ChoiceDeltaToolCall(index=0, function=ChoiceDeltaToolCallFunction(arguments='{"command": "pyt'))
                ]
            )
        )
        yield chunk(
            ChoiceDelta(
                tool_calls=[ChoiceDeltaToolCall(index=0, function=ChoiceDeltaToolCallFunction(arguments='est -q"}'))]
            )
        )
        yield chunk(
            ChoiceDelta(),
            finish="tool_calls",
            usage=CompletionUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def fake_acompletion(**kwargs: Any) -> AsyncIterator[ChatCompletionChunk]:
        return chunk_stream()

    with patch("gateway.api.routes.chat.acompletion", new=fake_acompletion):
        response = watched_client.post(
            f"{API_ROOT}/chat/completions",
            json={
                "model": "anthropic:claude-opus-4",
                "messages": [{"role": "user", "content": "test it"}],
                "user": "test-user",
                "stream": True,
            },
            headers=HEADERS,
        )

    assert response.status_code == 200, response.text
    assert "pyt" in response.text and "est -q" in response.text
    row = _latest_usage(watched_client)
    annotations = row["plugin_annotations"]["watcher"]
    assert annotations["tools"] == ["Bash"]
    assert annotations["prior_tool_calls"] == 0
    assert annotations["session"].startswith("anon-")
    assert "would_deny" not in annotations


@pytest.mark.asyncio
async def test_a_request_without_observers_writes_no_annotations(client: TestClient) -> None:
    _create_user(client)

    async def fake_acompletion(**kwargs: Any) -> ChatCompletion:
        return _completion_with_tool_call("ls")

    with patch("gateway.api.routes.chat.acompletion", new=fake_acompletion):
        response = client.post(
            f"{API_ROOT}/chat/completions",
            json={
                "model": "anthropic:claude-opus-4",
                "messages": [{"role": "user", "content": "hi"}],
                "user": "test-user",
            },
            headers=HEADERS,
        )

    assert response.status_code == 200, response.text
    assert _latest_usage(client)["plugin_annotations"] is None


@pytest.mark.asyncio
async def test_a_failed_request_keeps_its_request_annotations(watched_client: TestClient) -> None:
    _create_user(watched_client)

    async def failing_acompletion(**kwargs: Any) -> ChatCompletion:
        raise RuntimeError("upstream exploded")

    with patch("gateway.api.routes.chat.acompletion", new=failing_acompletion):
        response = watched_client.post(
            f"{API_ROOT}/chat/completions",
            json={
                "model": "anthropic:claude-opus-4",
                "messages": CONVERSATION,
                "user": "test-user",
                "session_label": "s-43",
            },
            headers=HEADERS,
        )

    assert response.status_code >= 500, response.text
    row = _latest_usage(watched_client)
    assert row["status"] == "error"
    assert row["plugin_annotations"] == {
        "watcher": {"api": "chat", "session": "s-43", "prior_tool_calls": 1, "workspace": True}
    }
