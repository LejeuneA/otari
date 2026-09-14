"""The plugin seam beyond routes: manifest declarations, lifecycle, events, backends, settings, enforcement."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

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
    Function,
)

from gateway.core.config import GatewayConfig
from gateway.models.guardrails import GuardrailConfig
from gateway.models.plugins import PLUGIN_API_VERSION, PluginManifestError, PluginsConfig, parse_manifest
from gateway.plugins import load_plugins
from gateway.plugins.events import EventBus, current_bus, emit
from gateway.plugins.guardrails import GuardrailOutcome
from gateway.plugins.registry import PluginError, build_plugin_config
from gateway.plugins.traffic import (
    Caller,
    ChatStreamGate,
    Conversation,
    MessagesStreamGate,
    RequestDecision,
    RequestEvent,
    ResponseDecision,
    ResponseEvent,
    ResponsesStreamGate,
    ToolCall,
    ToolCallDecision,
    ToolCallEvent,
    TrafficHooks,
    TrafficObservers,
    deny_in_result,
    inject_system_text,
    response_text,
)
from gateway.services.guardrails import GuardrailsNotReachableError, run_input_guardrails
from gateway.services.plugin_settings_service import PluginSettingsError, effective_values, validate_plugin_settings
from gateway.services.routing.backends import get_router_backend, known_backends, set_plugin_router_backends

# --- manifest ------------------------------------------------------------------


def _manifest(extra: str = "") -> str:
    return f"""
[plugin]
name = "probe"
version = "1.0.0"
package = "probe_plugin"
{extra}
"""


def test_manifest_defaults_cover_api_version_modes_and_pages() -> None:
    manifest = parse_manifest(_manifest())

    assert manifest.plugin_api == 1
    assert manifest.modes == ["standalone", "hosted", "hybrid"]
    assert manifest.pages == []
    assert manifest.settings == {}


def test_manifest_pages_carry_placement_and_the_short_form_normalizes() -> None:
    manifest = parse_manifest(
        _manifest(
            """
[[plugin.pages]]
id = "runs"
label = "Runs"
section = "build"
parent = "tools"
icon = "shield"
order = 5
audience = "member"
entry = "#/runs"

[[plugin.pages]]
id = "gates"
label = "Gates"
section = "none"
"""
        )
    )
    assert [page.id for page in manifest.pages] == ["runs", "gates"]
    runs = manifest.pages[0]
    assert (runs.section, runs.parent, runs.icon, runs.order, runs.audience, runs.entry) == (
        "build",
        "tools",
        "shield",
        5,
        "member",
        "#/runs",
    )

    short = parse_manifest(_manifest('[plugin.ui]\nlabel = "Probe"'))
    assert [(page.id, page.label, page.section) for page in short.pages] == [("index", "Probe", "extend")]


@pytest.mark.parametrize(
    "extra",
    [
        '[plugin.ui]\nlabel = "A"\n[[plugin.pages]]\nid = "a"\nlabel = "A"',
        '[[plugin.pages]]\nid = "a"\nlabel = "A"\n[[plugin.pages]]\nid = "a"\nlabel = "B"',
        '[[plugin.pages]]\nid = "a"\nlabel = "A"\nentry = "/abs"',
        '[[plugin.pages]]\nid = "a"\nlabel = "A"\nsection = "sidebar"',
        "modes = []",
        "plugin_api = 0",
        '[plugin.settings.Bad-Key]\ntype = "int"',
        '[plugin.settings.timeout]\ntype = "int"\ndefault = "ten"',
        'contributes = ["telepathy"]',
    ],
)
def test_manifest_refuses_bad_declarations(extra: str) -> None:
    with pytest.raises(PluginManifestError):
        parse_manifest(_manifest(extra))


def test_manifest_settings_are_typed_and_default() -> None:
    manifest = parse_manifest(
        _manifest(
            """
config_keys = ["traffic"]
[plugin.settings.timeout]
type = "int"
default = 30
description = "Seconds."
[plugin.settings.token]
type = "str"
secret = true
editable = false
"""
        )
    )
    assert manifest.settings["timeout"].default == 30
    assert manifest.settings["token"].secret is True
    assert manifest.all_config_keys == ["timeout", "token", "traffic"]
    assert manifest.setting_defaults == {"timeout": 30}


def test_plugin_config_layers_defaults_under_the_block_and_checks_types() -> None:
    manifest = parse_manifest(_manifest('[plugin.settings.timeout]\ntype = "int"\ndefault = 30'))

    assert build_plugin_config(manifest, {}) == {"timeout": 30}
    assert build_plugin_config(manifest, {"timeout": 5}) == {"timeout": 5}
    with pytest.raises(PluginError, match="timeout"):
        build_plugin_config(manifest, {"timeout": "soon"})
    with pytest.raises(PluginError):
        build_plugin_config(manifest, {"timeout": True})


# --- loading ---------------------------------------------------------------------


def _install(directory: Path, manifest: str, package: str, *, package_name: str = "probe_plugin") -> None:
    package_dir = directory / "probe" / package_name
    package_dir.mkdir(parents=True)
    (package_dir / "otari-plugin.toml").write_text(manifest)
    (package_dir / "__init__.py").write_text(package)
    sys.modules.pop(package_name, None)


def _config(directory: Path, **plugins: Any) -> GatewayConfig:
    return GatewayConfig(
        database_url="sqlite:///./x.db",
        master_key="k",
        plugins=PluginsConfig(directory=str(directory), **plugins),
    )


FULL_PACKAGE = """
from gateway.plugins.api import GuardrailOutcome, PluginContext, RoutingDecision


class Guard:
    async def check(self, text, *, direction, kwargs):
        return GuardrailOutcome(valid="secret" not in text, explanation="found a secret")


class Router:
    async def rank(self, ctx):
        return RoutingDecision.decline("plugin declines")


class Tool:
    openai_tools = [{"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}}]

    def owns_tool(self, name):
        return name == "echo"

    async def call_tool(self, name, arguments):
        return "echoed"

    def purpose_hints(self):
        return [("echo", "Echoes.")]


STATE = {"started": 0, "stopped": 0, "events": [], "settings": []}


def register(ctx: PluginContext) -> None:
    ctx.add_guardrail("secrets", Guard())
    ctx.add_router_backend("decline", Router())
    ctx.add_tool("echo", Tool)
    ctx.on_startup(lambda: STATE.__setitem__("started", STATE["started"] + 1))

    async def stop():
        STATE["stopped"] += 1

    ctx.on_shutdown(stop)
    ctx.add_health_check(lambda: True)
    ctx.subscribe("usage.logged", lambda event: STATE["events"].append(event.name))
    ctx.on_settings_change(lambda config: STATE["settings"].append(dict(config)))
"""

FULL_MANIFEST = """
[plugin]
name = "probe"
version = "1.0.0"
package = "probe_plugin"
contributes = ["guardrails", "routing", "tools", "lifecycle", "events"]
modes = ["standalone"]
[plugin.settings.timeout]
type = "int"
default = 30
"""


@pytest.mark.asyncio
async def test_a_plugin_registers_every_new_kind_of_contribution(tmp_path: Path) -> None:
    _install(tmp_path, FULL_MANIFEST, FULL_PACKAGE)
    registry = load_plugins(_config(tmp_path, probe={"timeout": 5}))
    plugin = registry.get("probe")
    assert plugin is not None and plugin.status == "loaded", plugin.error

    assert list(registry.guardrail_backends()) == ["probe:secrets"]
    assert list(registry.tool_backends()) == ["probe:echo"]
    assert list(registry.router_backends()) == ["probe:decline"]
    assert "probe:decline" in known_backends()
    assert get_router_backend(_config(tmp_path), "probe:decline") is plugin.router_backends["decline"]
    assert plugin.config == {"timeout": 5}
    assert registry.events.handlers_for("usage.logged")

    await registry.startup()
    report, critical = await registry.check_health()
    assert report == {"probe": "ok"} and critical is False
    await registry.apply_settings("probe", {"timeout": 9})
    await registry.shutdown()

    state = sys.modules["probe_plugin"].STATE
    assert state["started"] == 1 and state["stopped"] == 1
    assert state["settings"] == [{"timeout": 9}]
    set_plugin_router_backends({})


def test_an_undeclared_backend_is_refused_at_load(tmp_path: Path) -> None:
    manifest = FULL_MANIFEST.replace(
        'contributes = ["guardrails", "routing", "tools", "lifecycle", "events"]', 'contributes = ["guardrails"]'
    )
    _install(tmp_path, manifest, FULL_PACKAGE)

    plugin = load_plugins(_config(tmp_path)).get("probe")

    assert plugin is not None and plugin.status == "failed"
    assert "without declaring" in (plugin.error or "")
    assert "routing" in (plugin.error or "") and "events" in (plugin.error or "")
    assert plugin.guardrails == {} and plugin.tools == {}


def test_a_plugin_that_wants_a_newer_plugin_api_is_refused(tmp_path: Path) -> None:
    _install(
        tmp_path,
        _manifest(f"plugin_api = {PLUGIN_API_VERSION + 1}"),
        "def register(ctx):\n    raise AssertionError('never')\n",
    )

    plugin = load_plugins(_config(tmp_path)).get("probe")

    assert plugin is not None and plugin.status == "failed"
    assert f"needs plugin API {PLUGIN_API_VERSION + 1}" in (plugin.error or "")


def test_a_plugin_is_left_disabled_outside_its_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path, _manifest('modes = ["hybrid"]'), "def register(ctx):\n    raise AssertionError('never')\n")

    plugin = load_plugins(_config(tmp_path)).get("probe")

    assert plugin is not None and plugin.status == "disabled"
    assert "not for standalone mode" in (plugin.error or "")


def test_a_wrong_setting_type_fails_the_plugin(tmp_path: Path) -> None:
    _install(tmp_path, _manifest('[plugin.settings.timeout]\ntype = "int"'), "def register(ctx):\n    pass\n")

    plugin = load_plugins(_config(tmp_path, probe={"timeout": "soon"})).get("probe")

    assert plugin is not None and plugin.status == "failed"
    assert "timeout" in (plugin.error or "")


def test_a_failing_startup_hook_marks_the_plugin_failed(tmp_path: Path) -> None:
    package = """
def register(ctx):
    def boom():
        raise RuntimeError("no database")
    ctx.on_startup(boom)
    ctx.add_health_check(lambda: True)
"""
    _install(tmp_path, _manifest('contributes = ["lifecycle"]'), package)
    registry = load_plugins(_config(tmp_path))

    asyncio.run(registry.startup())

    plugin = registry.get("probe")
    assert plugin is not None and plugin.status == "failed"
    assert "startup hook" in (plugin.error or "")
    assert asyncio.run(registry.check_health()) == ({}, False)


@pytest.mark.asyncio
async def test_a_critical_health_check_that_fails_is_reported(tmp_path: Path) -> None:
    package = """
def register(ctx):
    async def down():
        raise ConnectionError("queue unreachable")
    ctx.add_health_check(down, critical=True)
    ctx.add_health_check(lambda: True)
"""
    _install(tmp_path, _manifest('contributes = ["lifecycle"]'), package)
    registry = load_plugins(_config(tmp_path))

    report, critical = await registry.check_health()

    assert critical is True
    assert report["probe"].startswith("failing: ConnectionError: queue unreachable")


# --- events ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_bus_runs_handlers_off_the_emitter_and_fences_them() -> None:
    seen: list[str] = []

    async def slow(event: Any) -> None:
        await asyncio.sleep(1)

    def broken(event: Any) -> None:
        raise RuntimeError("boom")

    bus = EventBus(
        [
            ("a", "usage.logged", lambda e: seen.append(f"a:{e.payload['cost']}")),
            ("b", "*", lambda e: seen.append("b")),
            ("c", "usage.logged", slow),
            ("d", "usage.logged", broken),
        ],
        timeout_ms=20,
    )
    token = current_bus.set(bus)
    try:
        assert emit("usage.logged", cost=1.5) == 4
        assert emit("key.created") == 1
        assert seen == []  # nothing ran on the emitter's stack
        await bus.drain(timeout=1)
    finally:
        current_bus.reset(token)
    assert sorted(seen) == ["a:1.5", "b", "b"]


def test_emit_without_a_bound_bus_is_a_no_op() -> None:
    assert emit("usage.logged") == 0


# --- guardrail backends ------------------------------------------------------------


class _Guard:
    def __init__(self, valid: bool | None, *, raise_error: bool = False) -> None:
        self.valid = valid
        self.raise_error = raise_error
        self.seen: list[tuple[str, str, dict[str, Any]]] = []

    async def check(self, text: str, *, direction: str, kwargs: dict[str, Any]) -> GuardrailOutcome:
        self.seen.append((text, direction, kwargs))
        if self.raise_error:
            raise RuntimeError("model not loaded")
        return GuardrailOutcome(valid=self.valid, explanation="because", score=0.9)


@pytest.mark.asyncio
async def test_a_plugin_profile_is_checked_in_process_and_its_url_ignored() -> None:
    guard = _Guard(False)
    verdict = await run_input_guardrails(
        [GuardrailConfig(profile="pii:scan", mode="block", url="http://10.0.0.1/never", validate_kwargs={"k": 1})],
        "hello",
        default_url=None,
        local={"pii:scan": guard},
    )
    assert verdict.blocked is True
    assert guard.seen == [("hello", "input", {"k": 1})]
    assert verdict.results[0].explanation == "because"


@pytest.mark.asyncio
async def test_a_failing_plugin_backend_follows_the_mode_contract() -> None:
    broken = _Guard(None, raise_error=True)
    with pytest.raises(GuardrailsNotReachableError) as info:
        await run_input_guardrails(
            [GuardrailConfig(profile="p:x", mode="block")], "t", default_url=None, local={"p:x": broken}
        )
    assert "model not loaded" not in info.value.public_detail

    verdict = await run_input_guardrails(
        [GuardrailConfig(profile="p:x", mode="monitor")], "t", default_url=None, local={"p:x": broken}
    )
    assert verdict.blocked is False and verdict.results[0].valid is None


# --- settings ------------------------------------------------------------------------


def test_settings_writes_are_validated_against_the_manifest() -> None:
    manifest = parse_manifest(
        _manifest(
            '[plugin.settings.timeout]\ntype = "int"\ndefault = 30\n'
            '[plugin.settings.token]\ntype = "str"\nsecret = true\neditable = false'
        )
    )
    assert validate_plugin_settings(manifest, {"timeout": 5}) == {"timeout": 5}
    assert validate_plugin_settings(manifest, {"timeout": None}) == {"timeout": None}
    for bad in ({"nope": 1}, {"timeout": "x"}, {"token": "abc"}):
        with pytest.raises(PluginSettingsError):
            validate_plugin_settings(manifest, bad)
    assert effective_values({"timeout": 5, "token": "abc"}, manifest) == {"timeout": 5, "token": "********"}
    assert effective_values({"timeout": 5}, manifest) == {"timeout": 5, "token": None}


# --- traffic: decisions apply ---------------------------------------------------------

CALLER = Caller(api_key_id="k", user_id="u", workspace_id="w", organization_id="o")


def _conversation() -> Conversation:
    return Conversation(api="chat", model="m", system="", turns=(), session_key="s")


class _Enforcer:
    def on_request(self, event: RequestEvent) -> RequestDecision:
        return RequestDecision(
            inject_system="Never force-push.", block="blocked" if "bad" in event.conversation.latest_user_text else None
        )

    def on_tool_call(self, event: ToolCallEvent) -> ToolCallDecision:
        return ToolCallDecision(deny="no force-push" if "force" in str(event.tool_call.arguments) else None)

    def on_response(self, event: ResponseEvent) -> ResponseDecision:
        return ResponseDecision(
            block="leaked" if "secret" in event.text else None, annotations={"len": len(event.text)}
        )


def _hooks(text: str = "") -> TrafficHooks:
    conversation = Conversation(api="chat", model="m", system="", turns=(), session_key="s", latest_user_text=text)
    return TrafficHooks(TrafficObservers([("gates", _Enforcer())]), CALLER, conversation)


@pytest.mark.asyncio
async def test_request_decisions_are_collected() -> None:
    hooks = _hooks("bad idea")
    await hooks.request()
    assert hooks.blocked == ("gates", "blocked")
    assert hooks.injected_system == "Never force-push."
    assert hooks.annotations["gates"] == {"injected_system": True, "blocked": "blocked"}


def test_inject_system_text_per_api() -> None:
    assert inject_system_text("chat", {"messages": [{"role": "user", "content": "hi"}]}, "X")["messages"][0] == {
        "role": "system",
        "content": "X",
    }
    assert inject_system_text("messages", {"system": "S"}, "X")["system"] == "X\n\nS"
    assert inject_system_text("messages", {"system": [{"type": "text", "text": "S"}]}, "X")["system"][0]["text"] == "X"
    assert inject_system_text("messages", {}, "X")["system"] == "X"
    assert inject_system_text("responses", {"instructions": "I"}, "X")["instructions"] == "X\n\nI"
    assert inject_system_text("responses", {}, "") == {}


def _chat_result(*commands: str) -> ChatCompletion:
    return ChatCompletion(
        id="c",
        object="chat.completion",
        created=0,
        model="m",
        choices=[
            Choice(
                index=0,
                finish_reason="tool_calls",
                message=ChatCompletionMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ChatCompletionMessageFunctionToolCall(
                            id=f"call_{i}",
                            type="function",
                            function=Function(name="Bash", arguments=f'{{"command": "{c}"}}'),
                        )
                        for i, c in enumerate(commands)
                    ],
                ),
            )
        ],
    )


@pytest.mark.asyncio
async def test_a_denied_call_is_replaced_in_a_chat_result_and_the_answer_is_judged() -> None:
    hooks = _hooks()
    result, block = await hooks.result("chat", _chat_result("git push --force", "ls"))

    message = result.choices[0].message
    assert [call.id for call in message.tool_calls] == ["call_1"]
    assert "no force-push" in message.content
    assert result.choices[0].finish_reason == "tool_calls"
    assert block is None
    assert hooks.annotations["gates"]["denied"][0]["tool_call_id"] == "call_0"
    assert hooks.annotations["gates"]["len"] > 0

    result, _ = await hooks.result("chat", _chat_result("git push --force origin main"))
    assert result.choices[0].message.tool_calls is None
    assert result.choices[0].finish_reason == "stop"


@pytest.mark.asyncio
async def test_a_blocked_response_is_reported_for_a_non_streamed_answer() -> None:
    hooks = _hooks()
    result = ChatCompletion(
        id="c",
        object="chat.completion",
        created=0,
        model="m",
        choices=[
            Choice(
                index=0,
                finish_reason="stop",
                message=ChatCompletionMessage(role="assistant", content="the secret is 42"),
            )
        ],
    )
    _, block = await hooks.result("chat", result)
    assert block == "leaked"
    assert hooks.annotations["gates"]["blocked_response"] == "leaked"


def test_deny_in_result_for_messages_and_responses_shapes() -> None:
    messages = {
        "content": [
            {"type": "text", "text": "Running"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git push --force"}},
        ],
        "stop_reason": "tool_use",
    }
    out = deny_in_result("messages", messages, {"t1": "no force-push"})
    assert out["content"][1].type == "text" and "no force-push" in out["content"][1].text
    assert out["stop_reason"] == "end_turn"
    assert response_text("messages", {"content": [{"type": "text", "text": "Running"}]}) == "Running"

    responses = {"output": [{"type": "function_call", "call_id": "f1", "name": "shell", "arguments": "{}"}]}
    out = deny_in_result("responses", responses, {"f1": "nope"})
    assert out["output"][0].type == "message"
    assert "nope" in out["output"][0].content[0].text


async def _judge(call: ToolCall) -> str | None:
    return "no force-push" if "force" in str(call.arguments) else None


def _chunk(delta: ChoiceDelta, finish: Any = None) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="x",
        object="chat.completion.chunk",
        created=0,
        model="m",
        choices=[ChunkChoice(index=0, delta=delta, finish_reason=finish)],
    )


@pytest.mark.asyncio
async def test_the_chat_stream_gate_holds_a_call_and_replaces_a_denied_one() -> None:
    gate = ChatStreamGate()
    text = _chunk(ChoiceDelta(role="assistant", content="Running "))
    assert await gate.feed(text, _judge) == [text]
    start = _chunk(
        ChoiceDelta(
            tool_calls=[
                ChoiceDeltaToolCall(index=0, id="c1", function=ChoiceDeltaToolCallFunction(name="Bash", arguments=""))
            ]
        )
    )
    assert await gate.feed(start, _judge) == []
    middle = _chunk(
        ChoiceDelta(
            tool_calls=[
                ChoiceDeltaToolCall(
                    index=0, function=ChoiceDeltaToolCallFunction(arguments='{"command": "git push --force"}')
                )
            ]
        )
    )
    assert await gate.feed(middle, _judge) == []
    finish = _chunk(ChoiceDelta(), "tool_calls")

    out = await gate.feed(finish, _judge)

    assert all(not (choice.delta.tool_calls) for chunk in out for choice in chunk.choices)
    contents = [choice.delta.content for chunk in out for choice in chunk.choices if choice.delta.content]
    assert contents and "no force-push" in contents[0]
    assert out[-1].choices[0].finish_reason == "stop"
    assert gate.take_survivors() == []
    assert gate.text() == "Running "


@pytest.mark.asyncio
async def test_the_chat_stream_gate_lets_an_allowed_call_through_whole() -> None:
    gate = ChatStreamGate()
    start = _chunk(
        ChoiceDelta(
            tool_calls=[
                ChoiceDeltaToolCall(
                    index=0, id="c1", function=ChoiceDeltaToolCallFunction(name="Bash", arguments='{"command": "ls"}')
                )
            ]
        )
    )
    finish = _chunk(ChoiceDelta(), "tool_calls")
    assert await gate.feed(start, _judge) == []
    assert await gate.feed(finish, _judge) == [start, finish]
    assert [call.id for call in gate.take_survivors()] == ["c1"]


@pytest.mark.asyncio
async def test_the_messages_stream_gate_replaces_a_denied_block() -> None:
    gate = MessagesStreamGate()
    events = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"command": "git push --force"}'},
        },
    ]
    for event in events:
        assert await gate.feed(event, _judge) == []
    out = await gate.feed({"type": "content_block_stop", "index": 0}, _judge)
    assert [e.type for e in out] == ["content_block_start", "content_block_delta", "content_block_stop"]
    assert "no force-push" in out[1].delta.text
    stop = {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}
    assert (await gate.feed(stop, _judge))[0]["delta"]["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_the_responses_stream_gate_replaces_a_denied_item() -> None:
    gate = ResponsesStreamGate()
    added = {
        "type": "response.output_item.added",
        "output_index": 1,
        "sequence_number": 3,
        "item": {"type": "function_call", "call_id": "f1"},
    }
    assert await gate.feed(added, _judge) == []
    done = {
        "type": "response.output_item.done",
        "output_index": 1,
        "sequence_number": 5,
        "item": {
            "type": "function_call",
            "call_id": "f1",
            "name": "shell",
            "arguments": '{"command": "git push --force"}',
        },
    }
    out = await gate.feed(done, _judge)
    assert [e.type for e in out] == [
        "response.output_item.added",
        "response.output_text.delta",
        "response.output_item.done",
    ]
    assert "no force-push" in out[1].delta
    completed = {
        "type": "response.completed",
        "response": {"output": [{"type": "function_call", "call_id": "f1", "name": "shell", "arguments": "{}"}]},
    }
    assert (await gate.feed(completed, _judge))[0]["response"]["output"][0].type == "message"


@pytest.mark.asyncio
async def test_the_chat_stream_gate_renumbers_the_survivors_of_a_partial_denial() -> None:
    gate = ChatStreamGate()
    denied_call = ChoiceDeltaToolCall(
        index=0, id="c0", function=ChoiceDeltaToolCallFunction(name="Bash", arguments='{"command": "git push --force"}')
    )
    allowed_call = ChoiceDeltaToolCall(
        index=1, id="c1", function=ChoiceDeltaToolCallFunction(name="Bash", arguments='{"command": "ls"}')
    )
    assert await gate.feed(_chunk(ChoiceDelta(tool_calls=[denied_call])), _judge) == []
    assert await gate.feed(_chunk(ChoiceDelta(tool_calls=[allowed_call])), _judge) == []

    out = await gate.feed(_chunk(ChoiceDelta(), "tool_calls"), _judge)

    fragments = [raw for chunk in out for choice in chunk.choices for raw in (choice.delta.tool_calls or [])]
    # The survivor moves to index 0: a client accumulating by index cannot take a gap.
    assert [(raw.index, raw.id) for raw in fragments] == [(0, "c1")]
    assert out[-1].choices[0].finish_reason == "tool_calls"
    assert [call.id for call in gate.take_survivors()] == ["c1"]


@pytest.mark.asyncio
async def test_a_startup_failure_withdraws_the_plugin_from_events_and_routing(tmp_path: Path) -> None:
    package = """
from gateway.plugins.api import RoutingDecision


class Router:
    async def rank(self, ctx):
        return RoutingDecision.decline("never")


def register(ctx):
    ctx.add_router_backend("decline", Router())
    ctx.subscribe("usage.logged", lambda event: None)

    def boom():
        raise RuntimeError("no database")

    ctx.on_startup(boom)
"""
    _install(tmp_path, _manifest('contributes = ["lifecycle", "events", "routing"]'), package)
    registry = load_plugins(_config(tmp_path))
    assert registry.events.handlers_for("usage.logged")
    assert "probe:decline" in known_backends()

    await registry.startup()

    assert registry.get("probe") is not None and registry.get("probe").status == "failed"  # type: ignore[union-attr]
    assert registry.events.handlers_for("usage.logged") == []
    assert "probe:decline" not in known_backends()


@pytest.mark.asyncio
async def test_health_checks_are_bounded_and_the_report_is_held(tmp_path: Path) -> None:
    package = """
import asyncio

CALLS = {"n": 0}


def register(ctx):
    async def slow():
        CALLS["n"] += 1
        await asyncio.sleep(1)
        return True

    ctx.add_health_check(slow)
"""
    _install(tmp_path, _manifest('contributes = ["lifecycle"]'), package)
    config = _config(tmp_path, health_timeout_ms=20)
    registry = load_plugins(config)

    report, critical = await registry.check_health()
    again, _ = await registry.check_health()

    assert report["probe"].startswith("failing: TimeoutError") and critical is False
    assert again == report
    assert sys.modules["probe_plugin"].CALLS["n"] == 1


@pytest.mark.asyncio
async def test_secret_settings_are_encrypted_at_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway.services.plugin_settings_service import ENCRYPTED_KEY, load_plugin_settings, save_plugin_settings
    from gateway.services.secret_box import generate_secret_key

    monkeypatch.setenv("OTARI_SECRET_KEY", generate_secret_key())
    manifest = parse_manifest(
        _manifest('[plugin.settings.token]\ntype = "str"\nsecret = true\n[plugin.settings.timeout]\ntype = "int"')
    )
    rows: dict[str, Any] = {}

    class _Session:
        async def execute(self, statement: Any) -> Any:
            class _Result:
                def scalars(self_inner) -> list[Any]:
                    return list(rows.values())

            return _Result()

        async def get(self, model: Any, key: str) -> Any:
            return rows.get(key)

        def add(self, row: Any) -> None:
            rows[row.key] = row

        async def commit(self) -> None:
            pass

    await save_plugin_settings(_Session(), "probe", {"token": "hunter2", "timeout": 3}, manifest)  # type: ignore[arg-type]

    stored = {key.removeprefix("plugin:probe:"): json.loads(row.value) for key, row in rows.items()}
    assert stored["timeout"] == 3
    assert ENCRYPTED_KEY in stored["token"] and "hunter2" not in json.dumps(stored["token"])
    assert await load_plugin_settings(_Session(), "probe", manifest) == {"token": "hunter2", "timeout": 3}  # type: ignore[arg-type]
