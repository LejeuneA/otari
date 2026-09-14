"""The traffic seam: conversation normalizing, tool-call assembly, and fenced dispatch.

Covers the mechanism without a running app. The end-to-end path (a plugin's
observer annotating the usage row of a real request) is in
``tests/integration/test_plugin_traffic.py``.
"""

import asyncio
import datetime
import math
import time
from typing import Any

import pytest

from gateway.plugins.traffic import (
    Caller,
    ChatToolCallAssembler,
    Conversation,
    MessagesToolCallAssembler,
    RequestDecision,
    RequestEvent,
    ResponsesToolCallAssembler,
    ToolCall,
    ToolCallDecision,
    ToolCallEvent,
    ToolSpec,
    TrafficHooks,
    TrafficObservers,
    conversation_from_chat,
    conversation_from_messages,
    conversation_from_responses,
    session_key,
    tool_calls_from_result,
)

CALLER = Caller(api_key_id="k", user_id="u", workspace_id="w", organization_id="o")


# --- normalizing -----------------------------------------------------------


def test_chat_conversation_pairs_tool_calls_with_tool_results() -> None:
    messages = [
        {"role": "system", "content": "Be careful."},
        {"role": "user", "content": "run the tests"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "pytest -q"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "2 passed"},
        {"role": "assistant", "content": "All green."},
        {"role": "user", "content": "thanks"},
    ]

    conversation = conversation_from_chat("gpt", messages, session="")

    assert conversation.api == "chat"
    assert conversation.system == "Be careful."
    assert conversation.latest_user_text == "thanks"
    assert len(conversation.turns) == 2
    first = conversation.turns[0]
    assert first.tool_calls == (ToolCall(id="c1", name="Bash", arguments={"command": "pytest -q"}),)
    assert first.results[0].call_id == "c1"
    assert first.results[0].content == "2 passed"
    assert conversation.turns[1].text == "All green."
    assert conversation.session_key.startswith("anon-")
    # The user's instruction is on the turn it prompted; a tool result is not user text.
    assert first.user_text == "run the tests"
    assert conversation.turns[1].user_text == ""


def test_messages_conversation_reads_tool_use_and_tool_result_blocks() -> None:
    messages = [
        {"role": "user", "content": "push it"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Pushing."},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git push --force"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [{"type": "text", "text": "denied"}],
                    "is_error": True,
                }
            ],
        },
    ]

    conversation = conversation_from_messages("claude", [{"type": "text", "text": "sys"}], messages, session="s-1")

    assert conversation.system == "sys"
    assert conversation.session_key == "s-1"
    (turn,) = conversation.turns
    assert turn.text == "Pushing."
    assert turn.tool_calls[0].arguments == {"command": "git push --force"}
    assert turn.results[0].is_error is True
    assert turn.results[0].content == "denied"


def test_responses_conversation_reads_function_call_items() -> None:
    input_data = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "list files"}]},
        {"type": "function_call", "call_id": "f1", "name": "shell", "arguments": '{"command": ["ls"]}'},
        {"type": "function_call_output", "call_id": "f1", "output": "a.py"},
    ]

    conversation = conversation_from_responses("gpt", "You are terse.", input_data, session="")

    assert conversation.system == "You are terse."
    (turn,) = conversation.turns
    assert turn.tool_calls[0] == ToolCall(id="f1", name="shell", arguments={"command": ["ls"]})
    assert turn.results[0].content == "a.py"


def test_responses_conversation_accepts_a_plain_string_input() -> None:
    conversation = conversation_from_responses("gpt", None, "hello", session="")

    assert conversation.turns == ()
    assert conversation.latest_user_text == "hello"


def test_declared_tools_are_normalized_from_each_request_shape() -> None:
    chat_tools = [{"type": "function", "function": {"name": "Bash", "description": "run", "parameters": {"a": 1}}}]
    messages_tools = [{"name": "Bash", "description": "run", "input_schema": {"a": 1}}]
    responses_tools = [{"type": "function", "name": "Bash", "description": "run", "parameters": {"a": 1}}]
    expected = (ToolSpec(name="Bash", description="run", parameters={"a": 1}),)

    assert conversation_from_chat("m", [], session="", tools=chat_tools).tools == expected
    assert conversation_from_messages("m", None, [], session="", tools=messages_tools).tools == expected
    assert conversation_from_responses("m", None, [], session="", tools=responses_tools).tools == expected
    # A provider's built-in tool has no name of its own and is left out.
    assert conversation_from_chat("m", [], session="", tools=[{"type": "web_search"}]).tools == ()


def test_session_key_prefers_an_explicit_identifier_and_is_stable_otherwise() -> None:
    assert session_key(None, "label-1", system="s", first_user_text="u") == "label-1"
    anonymous = session_key(None, None, system="s", first_user_text="u")
    assert anonymous == session_key(system="s", first_user_text="u")
    assert anonymous != session_key(system="s", first_user_text="other")
    # Two callers whose sessions began identically are two sessions.
    assert session_key(system="s", first_user_text="u", scope="key-a") != anonymous
    assert session_key(system="s", first_user_text="u", scope="key-a") != session_key(
        system="s", first_user_text="u", scope="key-b"
    )


def test_unparseable_arguments_are_kept_raw() -> None:
    messages = [{"role": "assistant", "tool_calls": [{"id": "c", "function": {"name": "f", "arguments": "{not json"}}]}]

    conversation = conversation_from_chat("m", messages, session="")

    assert conversation.turns[0].tool_calls[0].arguments == {"_raw": "{not json"}


# --- results and streams -----------------------------------------------------


def test_tool_calls_from_each_result_shape() -> None:
    chat = {"choices": [{"message": {"tool_calls": [{"id": "c1", "function": {"name": "Bash", "arguments": "{}"}}]}}]}
    messages = {
        "content": [
            {"type": "text", "text": "x"},
            {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"path": "a"}},
        ]
    }
    responses = {"output": [{"type": "function_call", "call_id": "f1", "name": "shell", "arguments": '{"a": 1}'}]}

    assert tool_calls_from_result("chat", chat) == [ToolCall("c1", "Bash", {})]
    assert tool_calls_from_result("messages", messages) == [ToolCall("t1", "Edit", {"path": "a"})]
    assert tool_calls_from_result("responses", responses) == [ToolCall("f1", "shell", {"a": 1})]


def test_chat_assembler_joins_argument_fragments_across_chunks() -> None:
    assembler = ChatToolCallAssembler()
    chunks = [
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "Bash", "arguments": ""}}]}}
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"command": '}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"ls"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]

    completed = [call for chunk in chunks for call in assembler.feed(chunk)]

    assert completed == [ToolCall("c1", "Bash", {"command": "ls"})]
    assert assembler.finish() == []


def test_messages_assembler_completes_a_tool_use_block_on_stop() -> None:
    assembler = MessagesToolCallAssembler()
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Running"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"command": "git '},
        },
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'status"}'}},
    ]
    assert [call for event in events for call in assembler.feed(event)] == []

    completed = assembler.feed({"type": "content_block_stop", "index": 1})

    assert completed == [ToolCall("t1", "Bash", {"command": "git status"})]


def test_responses_assembler_reads_done_function_call_items() -> None:
    assembler = ResponsesToolCallAssembler()

    assert assembler.feed({"type": "response.output_item.added", "item": {"type": "function_call"}}) == []
    assert assembler.feed(
        {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "f1", "name": "shell", "arguments": "{}"},
        }
    ) == [ToolCall("f1", "shell", {})]


# --- dispatch ----------------------------------------------------------------


def conversation() -> Conversation:
    return Conversation(api="chat", model="m", system="", turns=(), session_key="s")


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[RequestEvent] = []
        self.calls: list[ToolCallEvent] = []

    def on_request(self, event: RequestEvent) -> RequestDecision:
        self.requests.append(event)
        return RequestDecision(annotations={"seen": True, "fired": ["a"]})

    async def on_tool_call(self, event: ToolCallEvent) -> ToolCallDecision:
        self.calls.append(event)
        deny = "no force-push" if "force" in str(event.tool_call.arguments) else None
        return ToolCallDecision(deny=deny, annotations={"fired": [event.tool_call.name]})


@pytest.mark.asyncio
async def test_hooks_collect_annotations_per_plugin_and_record_would_deny() -> None:
    recorder = _Recorder()
    hooks = TrafficHooks(TrafficObservers([("gates", recorder)]), CALLER, conversation())

    await hooks.request()
    await hooks.tool_call(ToolCall("c1", "Bash", {"command": "git push --force"}))
    await hooks.tool_call(ToolCall("c2", "Edit", {}))

    assert len(recorder.requests) == 1
    assert [call.tool_call.id for call in recorder.calls] == ["c1", "c2"]
    assert hooks.annotations == {
        "gates": {
            "seen": True,
            "fired": ["a", "Bash", "Edit"],
            "would_deny": [{"tool_call_id": "c1", "message": "no force-push"}],
        }
    }


@pytest.mark.asyncio
async def test_a_raising_observer_is_skipped_and_the_others_answer() -> None:
    class Broken:
        def on_request(self, event: RequestEvent) -> RequestDecision:
            raise RuntimeError("boom")

    recorder = _Recorder()
    hooks = TrafficHooks(TrafficObservers([("broken", Broken()), ("gates", recorder)]), CALLER, conversation())

    await hooks.request()

    assert "broken" not in hooks.annotations
    assert hooks.annotations["gates"]["seen"] is True


@pytest.mark.asyncio
async def test_a_slow_async_observer_is_cut_off_at_the_budget() -> None:
    class Slow:
        async def on_request(self, event: RequestEvent) -> RequestDecision:
            await asyncio.sleep(1)
            return RequestDecision(annotations={"late": True})

    hooks = TrafficHooks(TrafficObservers([("slow", Slow())], timeout_ms=20), CALLER, conversation())

    await hooks.request()

    assert hooks.annotations == {}


@pytest.mark.asyncio
async def test_a_slow_sync_observer_is_abandoned_at_the_budget() -> None:
    import threading

    class Slow:
        def on_request(self, event: RequestEvent) -> RequestDecision:
            # Blocks its thread, never the loop: the request moves on at the budget.
            time.sleep(0.2)
            return RequestDecision(annotations={"late": True})

    class OnLoop:
        def on_request(self, event: RequestEvent) -> RequestDecision:
            return RequestDecision(annotations={"thread": threading.current_thread() is threading.main_thread()})

    observers = TrafficObservers([("slow", Slow()), ("worker", OnLoop())], timeout_ms=20)
    hooks = TrafficHooks(observers, CALLER, conversation())

    started = time.perf_counter()
    await hooks.request()

    assert time.perf_counter() - started < 0.15
    assert hooks.annotations == {"worker": {"thread": False}}


@pytest.mark.asyncio
async def test_annotations_are_capped_per_plugin_and_the_gateway_s_keys_are_reserved() -> None:
    class Verbose:
        def on_request(self, event: RequestEvent) -> RequestDecision:
            return RequestDecision(annotations={"small": "kept"})

        def on_tool_call(self, event: ToolCallEvent) -> ToolCallDecision:
            return ToolCallDecision(deny="no", annotations={"dump": "x" * (17 * 1024), "would_deny": "mine"})

    hooks = TrafficHooks(TrafficObservers([("v", Verbose())]), CALLER, conversation())

    await hooks.request()
    await hooks.tool_call(ToolCall("c1", "Bash", {}))

    # The oversized batch is dropped whole; what was recorded before it stays,
    # and the gateway's own would_deny entry is still written.
    assert hooks.annotations == {"v": {"small": "kept", "would_deny": [{"tool_call_id": "c1", "message": "no"}]}}


@pytest.mark.asyncio
async def test_annotations_that_cannot_reach_the_json_column_are_skipped() -> None:
    class WrongShape:
        def on_request(self, event: RequestEvent) -> RequestDecision:
            return RequestDecision(annotations=["not", "a", "dict"])  # type: ignore[arg-type]

    class WrongValue:
        def on_request(self, event: RequestEvent) -> RequestDecision:
            return RequestDecision(annotations={"seen_at": datetime.datetime.now(tz=datetime.UTC)})

    class NotJson:
        # json.dumps accepts NaN; PostgreSQL's json parser does not.
        def on_request(self, event: RequestEvent) -> RequestDecision:
            return RequestDecision(annotations={"score": math.nan})

    observers = TrafficObservers(
        [("shape", WrongShape()), ("value", WrongValue()), ("nan", NotJson()), ("gates", _Recorder())]
    )
    hooks = TrafficHooks(observers, CALLER, conversation())

    await hooks.request()

    assert set(hooks.annotations) == {"gates"}


@pytest.mark.asyncio
async def test_an_observer_with_only_one_method_is_asked_only_that() -> None:
    class RequestOnly:
        def on_request(self, event: RequestEvent) -> RequestDecision:
            return RequestDecision(annotations={"only": "request"})

    hooks = TrafficHooks(TrafficObservers([("r", RequestOnly())]), CALLER, conversation())

    await hooks.request()
    await hooks.tool_call(ToolCall("c", "Bash", {}))

    assert hooks.annotations == {"r": {"only": "request"}}


@pytest.mark.asyncio
async def test_observe_stream_passes_chunks_through_and_asks_per_completed_call() -> None:
    recorder = _Recorder()
    hooks = TrafficHooks(TrafficObservers([("gates", recorder)]), CALLER, conversation())

    async def source() -> Any:
        yield {"choices": [{"delta": {"content": "hi"}}]}
        yield {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "Bash", "arguments": "{}"}}]}}
            ]
        }
        yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}

    passed = [chunk async for chunk in hooks.observe_stream("chat", source())]

    assert len(passed) == 3
    assert [call.tool_call.id for call in recorder.calls] == ["c1"]


def test_observers_are_falsy_when_nothing_registered() -> None:
    assert not TrafficObservers([])
    assert TrafficObservers([("x", _Recorder())])
