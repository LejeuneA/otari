"""The traffic seam: what a plugin sees of an inference request, and how it is asked.

A plugin registers a *traffic observer* through ``PluginContext``. The gateway
builds one provider-neutral :class:`Conversation` per inference request (chat,
messages, or responses shape), asks each observer about it before dispatch, asks
again about every tool call the model produces (buffered per call while a
stream flows), and writes whatever the observers annotated onto the usage row.

Every observer call is fenced: an exception is logged with the plugin's name
and skipped, a call that outlives ``plugins.observer_timeout_ms`` is cancelled
and skipped, and nothing at all runs when no plugin registered an observer. A
plugin can therefore never fail or slow a request beyond the budget.

Decisions (a system text to inject, a tool call to deny) are recorded in this
phase and not applied; the ``annotations`` are what reach the usage row.
"""

import asyncio
import hashlib
import inspect
import json
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from gateway.log_config import logger

Api = Literal["chat", "messages", "responses"]


@dataclass(frozen=True)
class Caller:
    """Who sent the request, as the gateway resolved it."""

    api_key_id: str | None
    user_id: str | None
    workspace_id: str | None
    organization_id: str | None


@dataclass(frozen=True)
class ToolCall:
    """A tool the model asked the client to run."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """What the client sent back for one tool call."""

    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Turn:
    """One assistant message and the tool results the client returned for it."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True)
class Conversation:
    """The request's conversation, the same shape whichever API carried it."""

    api: Api
    model: str
    system: str
    turns: tuple[Turn, ...]
    session_key: str
    latest_user_text: str = ""

    @property
    def last_turn(self) -> Turn | None:
        return self.turns[-1] if self.turns else None


@dataclass(frozen=True)
class RequestEvent:
    caller: Caller
    conversation: Conversation


@dataclass
class RequestDecision:
    """What an observer says about a request. Only ``annotations`` are applied today."""

    inject_system: str | None = None
    annotations: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallEvent:
    caller: Caller
    conversation: Conversation
    tool_call: ToolCall


@dataclass
class ToolCallDecision:
    """What an observer says about a tool call. ``deny`` is recorded, not applied, today."""

    deny: str | None = None
    annotations: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class TrafficObserver(Protocol):
    """What a plugin registers. Both methods are optional; either may be sync or async."""

    def on_request(self, event: RequestEvent) -> Any: ...

    def on_tool_call(self, event: ToolCallEvent) -> Any: ...


DEFAULT_OBSERVER_TIMEOUT_MS = 250


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class TrafficObservers:
    """The observers this process loaded, called with the fences the seam promises."""

    def __init__(self, observers: Iterable[tuple[str, Any]], timeout_ms: int = DEFAULT_OBSERVER_TIMEOUT_MS) -> None:
        self._observers = [(name, observer) for name, observer in observers]
        self._timeout = timeout_ms / 1000

    def __bool__(self) -> bool:
        return bool(self._observers)

    def __len__(self) -> int:
        return len(self._observers)

    async def _ask(self, method_name: str, event: Any) -> list[tuple[str, Any]]:
        """Call ``method_name`` on every observer that has it; return (plugin, decision) pairs."""
        answers: list[tuple[str, Any]] = []
        for name, observer in self._observers:
            method = getattr(observer, method_name, None)
            if method is None:
                continue
            started = time.perf_counter()
            try:
                outcome = method(event)
                if inspect.isawaitable(outcome):
                    outcome = await asyncio.wait_for(outcome, timeout=self._timeout)
            except TimeoutError:
                logger.warning(
                    "Plugin %s: %s took longer than %.0f ms and was skipped", name, method_name, self._timeout * 1000
                )
                continue
            except Exception:  # noqa: BLE001 one plugin's failure must not touch the request
                logger.exception("Plugin %s: %s raised and was skipped", name, method_name)
                continue
            elapsed = time.perf_counter() - started
            if elapsed > self._timeout:
                # A synchronous observer cannot be cut off; say so rather than hide it.
                logger.warning("Plugin %s: %s took %.0f ms synchronously", name, method_name, elapsed * 1000)
            if outcome is not None:
                answers.append((name, outcome))
        return answers

    async def on_request(self, event: RequestEvent) -> list[tuple[str, RequestDecision]]:
        return [(name, d) for name, d in await self._ask("on_request", event) if isinstance(d, RequestDecision)]

    async def on_tool_call(self, event: ToolCallEvent) -> list[tuple[str, ToolCallDecision]]:
        return [(name, d) for name, d in await self._ask("on_tool_call", event) if isinstance(d, ToolCallDecision)]


class TrafficHooks:
    """One request's observers, conversation, caller, and what they annotated so far.

    Built by ``prepare_gateway_tools`` when at least one observer is loaded, kept
    on the request context, and read at settlement for the usage row.
    """

    def __init__(self, observers: TrafficObservers, caller: Caller, conversation: Conversation) -> None:
        self.observers = observers
        self.caller = caller
        self.conversation = conversation
        self.annotations: dict[str, dict[str, Any]] = {}
        self.decisions: list[tuple[str, RequestDecision | ToolCallDecision]] = []

    def _merge(self, name: str, annotations: dict[str, Any]) -> None:
        if not annotations:
            return
        current = self.annotations.setdefault(name, {})
        for key, value in annotations.items():
            if isinstance(value, list) and isinstance(current.get(key), list):
                current[key] = [*current[key], *value]
            else:
                current[key] = value

    async def request(self) -> None:
        for name, decision in await self.observers.on_request(RequestEvent(self.caller, self.conversation)):
            self.decisions.append((name, decision))
            self._merge(name, decision.annotations)

    async def tool_call(self, tool_call: ToolCall) -> None:
        event = ToolCallEvent(self.caller, self.conversation, tool_call)
        for name, decision in await self.observers.on_tool_call(event):
            self.decisions.append((name, decision))
            self._merge(name, decision.annotations)
            if decision.deny:
                # Recorded so an operator can see what enforcement would have done.
                self._merge(name, {"would_deny": [{"tool_call_id": tool_call.id, "message": decision.deny}]})

    async def result(self, api: Api, result: Any) -> None:
        """Ask about every tool call in a non-streaming result."""
        for tool_call in tool_calls_from_result(api, result):
            await self.tool_call(tool_call)

    def observe_stream(self, api: Api, stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
        """Pass a provider stream through, asking about each tool call as it completes."""
        assembler = assembler_for(api)

        async def _observed() -> AsyncIterator[Any]:
            async for chunk in stream:
                for tool_call in assembler.feed(chunk):
                    await self.tool_call(tool_call)
                yield chunk
            for tool_call in assembler.finish():
                await self.tool_call(tool_call)

        return _observed()

    def annotations_or_none(self) -> dict[str, dict[str, Any]] | None:
        return self.annotations or None


# ---------------------------------------------------------------------------
# Session identity
# ---------------------------------------------------------------------------


def session_key(*candidates: str | None, system: str = "", first_user_text: str = "") -> str:
    """The first explicit identifier a request carries, else a digest of how it began.

    A digest of the system prompt and first user turn is stable for one agent
    session, since neither changes as the conversation grows, and different for
    two sessions that started differently. It is best effort: two sessions that
    began identically collide.
    """
    for candidate in candidates:
        if candidate:
            return str(candidate)[:200]
    digest = hashlib.sha256(f"{system}\x1f{first_user_text}".encode()).hexdigest()[:16]
    return f"anon-{digest}"


# ---------------------------------------------------------------------------
# Normalizing a request into a Conversation
# ---------------------------------------------------------------------------


def _text_of(content: Any) -> str:
    """Flatten a content value (string, list of blocks, or nested) into text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("text", "input_text", "output_text", "content", "output"):
            if key in content:
                return _text_of(content[key])
        return ""
    if isinstance(content, list):
        return "\n".join(part for part in (_text_of(item) for item in content) if part)
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    return str(content)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"_raw": raw}
        return parsed if isinstance(parsed, dict) else {"_value": parsed}
    return {}


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


class _TurnBuilder:
    def __init__(self) -> None:
        self.turns: list[Turn] = []
        self._open: dict[str, Any] | None = None

    def assistant(self, text: str, tool_calls: list[ToolCall]) -> None:
        self._close()
        self._open = {"text": text, "tool_calls": tool_calls, "results": []}

    def tool_call(self, tool_call: ToolCall) -> None:
        if self._open is None:
            self._open = {"text": "", "tool_calls": [], "results": []}
        self._open["tool_calls"].append(tool_call)

    def result(self, result: ToolResult) -> None:
        if self._open is None:
            self._open = {"text": "", "tool_calls": [], "results": []}
        self._open["results"].append(result)

    def _close(self) -> None:
        if self._open is not None:
            self.turns.append(
                Turn(
                    text=self._open["text"],
                    tool_calls=tuple(self._open["tool_calls"]),
                    results=tuple(self._open["results"]),
                )
            )
            self._open = None

    def build(self) -> tuple[Turn, ...]:
        self._close()
        return tuple(self.turns)


def conversation_from_chat(model: str, messages: list[Any], *, session: str) -> Conversation:
    """OpenAI chat shape: ``system``/``user``/``assistant``/``tool`` roles."""
    system_parts: list[str] = []
    builder = _TurnBuilder()
    first_user = ""
    latest_user = ""
    for message in messages or []:
        role = _get(message, "role")
        content = _get(message, "content")
        if role in ("system", "developer"):
            system_parts.append(_text_of(content))
        elif role == "user":
            text = _text_of(content)
            first_user = first_user or text
            latest_user = text or latest_user
        elif role == "assistant":
            calls: list[ToolCall] = []
            for raw in _get(message, "tool_calls") or []:
                function = _get(raw, "function") or {}
                calls.append(
                    ToolCall(
                        id=str(_get(raw, "id") or ""),
                        name=str(_get(function, "name") or ""),
                        arguments=_parse_arguments(_get(function, "arguments")),
                    )
                )
            builder.assistant(_text_of(content), calls)
        elif role == "tool":
            builder.result(ToolResult(call_id=str(_get(message, "tool_call_id") or ""), content=_text_of(content)))
    system = "\n".join(part for part in system_parts if part)
    return Conversation(
        api="chat",
        model=model,
        system=system,
        turns=builder.build(),
        session_key=session_key(session, system=system, first_user_text=first_user),
        latest_user_text=latest_user,
    )


def conversation_from_messages(model: str, system: Any, messages: list[Any], *, session: str) -> Conversation:
    """Anthropic messages shape: ``tool_use`` and ``tool_result`` content blocks."""
    system_text = _text_of(system)
    builder = _TurnBuilder()
    first_user = ""
    latest_user = ""
    for message in messages or []:
        role = _get(message, "role")
        content = _get(message, "content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": _text_of(content)}]
        if role == "assistant":
            texts: list[str] = []
            calls: list[ToolCall] = []
            for block in blocks:
                kind = _get(block, "type")
                if kind == "tool_use":
                    calls.append(
                        ToolCall(
                            id=str(_get(block, "id") or ""),
                            name=str(_get(block, "name") or ""),
                            arguments=_parse_arguments(_get(block, "input")),
                        )
                    )
                elif kind == "text":
                    texts.append(_text_of(_get(block, "text")))
            builder.assistant("\n".join(t for t in texts if t), calls)
        elif role == "user":
            texts = []
            for block in blocks:
                if _get(block, "type") == "tool_result":
                    builder.result(
                        ToolResult(
                            call_id=str(_get(block, "tool_use_id") or ""),
                            content=_text_of(_get(block, "content")),
                            is_error=bool(_get(block, "is_error", False)),
                        )
                    )
                else:
                    texts.append(_text_of(block))
            text = "\n".join(t for t in texts if t)
            if text:
                first_user = first_user or text
                latest_user = text
    return Conversation(
        api="messages",
        model=model,
        system=system_text,
        turns=builder.build(),
        session_key=session_key(session, system=system_text, first_user_text=first_user),
        latest_user_text=latest_user,
    )


def conversation_from_responses(model: str, instructions: Any, input_data: Any, *, session: str) -> Conversation:
    """OpenAI responses shape: message items plus ``function_call`` / ``function_call_output`` items."""
    system_text = _text_of(instructions)
    builder = _TurnBuilder()
    first_user = ""
    latest_user = ""
    items = input_data if isinstance(input_data, list) else [{"type": "message", "role": "user", "content": input_data}]
    for item in items:
        kind = _get(item, "type") or "message"
        if kind == "message":
            role = _get(item, "role")
            text = _text_of(_get(item, "content"))
            if role == "assistant":
                builder.assistant(text, [])
            elif role in ("system", "developer"):
                system_text = "\n".join(part for part in (system_text, text) if part)
            else:
                first_user = first_user or text
                latest_user = text or latest_user
        elif kind == "function_call":
            builder.tool_call(
                ToolCall(
                    id=str(_get(item, "call_id") or _get(item, "id") or ""),
                    name=str(_get(item, "name") or ""),
                    arguments=_parse_arguments(_get(item, "arguments")),
                )
            )
        elif kind == "function_call_output":
            builder.result(ToolResult(call_id=str(_get(item, "call_id") or ""), content=_text_of(_get(item, "output"))))
    return Conversation(
        api="responses",
        model=model,
        system=system_text,
        turns=builder.build(),
        session_key=session_key(session, system=system_text, first_user_text=first_user),
        latest_user_text=latest_user,
    )


# ---------------------------------------------------------------------------
# Tool calls out of a result, complete or streamed
# ---------------------------------------------------------------------------


def tool_calls_from_result(api: Api, result: Any) -> list[ToolCall]:
    """Every tool call a non-streaming result carries."""
    calls: list[ToolCall] = []
    if api == "chat":
        for choice in _get(result, "choices") or []:
            message = _get(choice, "message")
            for raw in _get(message, "tool_calls") or []:
                function = _get(raw, "function") or {}
                calls.append(
                    ToolCall(
                        id=str(_get(raw, "id") or ""),
                        name=str(_get(function, "name") or ""),
                        arguments=_parse_arguments(_get(function, "arguments")),
                    )
                )
    elif api == "messages":
        calls.extend(
            ToolCall(
                id=str(_get(block, "id") or ""),
                name=str(_get(block, "name") or ""),
                arguments=_parse_arguments(_get(block, "input")),
            )
            for block in _get(result, "content") or []
            if _get(block, "type") == "tool_use"
        )
    elif api == "responses":
        calls.extend(
            ToolCall(
                id=str(_get(item, "call_id") or _get(item, "id") or ""),
                name=str(_get(item, "name") or ""),
                arguments=_parse_arguments(_get(item, "arguments")),
            )
            for item in _get(result, "output") or []
            if _get(item, "type") == "function_call"
        )
    return calls


class ToolCallAssembler(Protocol):
    """Rebuilds tool calls from a stream's fragments; ``feed`` returns the ones that just completed."""

    def feed(self, chunk: Any) -> list[ToolCall]: ...

    def finish(self) -> list[ToolCall]: ...


class _Partial:
    __slots__ = ("arguments", "id", "name")

    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments: list[str] = []

    def build(self) -> ToolCall:
        return ToolCall(id=self.id, name=self.name, arguments=_parse_arguments("".join(self.arguments)))


class ChatToolCallAssembler:
    """``delta.tool_calls[i]`` fragments, keyed by index, flushed on ``finish_reason``."""

    def __init__(self) -> None:
        self._partials: dict[int, _Partial] = {}

    def feed(self, chunk: Any) -> list[ToolCall]:
        completed: list[ToolCall] = []
        for choice in _get(chunk, "choices") or []:
            delta = _get(choice, "delta")
            for raw in _get(delta, "tool_calls") or []:
                index = int(_get(raw, "index") or 0)
                partial = self._partials.setdefault(index, _Partial())
                if _get(raw, "id"):
                    partial.id = str(_get(raw, "id"))
                function = _get(raw, "function")
                if function is not None:
                    if _get(function, "name"):
                        partial.name = str(_get(function, "name"))
                    if _get(function, "arguments"):
                        partial.arguments.append(str(_get(function, "arguments")))
            if _get(choice, "finish_reason"):
                completed.extend(self.finish())
        return completed

    def finish(self) -> list[ToolCall]:
        done = [self._partials[index].build() for index in sorted(self._partials)]
        self._partials.clear()
        return done


class MessagesToolCallAssembler:
    """``content_block_start`` (tool_use) through ``content_block_stop``, keyed by block index."""

    def __init__(self) -> None:
        self._partials: dict[int, _Partial] = {}

    def feed(self, chunk: Any) -> list[ToolCall]:
        kind = _get(chunk, "type")
        index = int(_get(chunk, "index") or 0)
        if kind == "content_block_start":
            block = _get(chunk, "content_block")
            if _get(block, "type") == "tool_use":
                partial = _Partial()
                partial.id = str(_get(block, "id") or "")
                partial.name = str(_get(block, "name") or "")
                initial = _get(block, "input")
                if isinstance(initial, dict) and initial:
                    partial.arguments.append(json.dumps(initial))
                self._partials[index] = partial
        elif kind == "content_block_delta" and index in self._partials:
            delta = _get(chunk, "delta")
            if _get(delta, "type") == "input_json_delta":
                self._partials[index].arguments.append(str(_get(delta, "partial_json") or ""))
        elif kind == "content_block_stop" and index in self._partials:
            return [self._partials.pop(index).build()]
        return []

    def finish(self) -> list[ToolCall]:
        done = [self._partials[index].build() for index in sorted(self._partials)]
        self._partials.clear()
        return done


class ResponsesToolCallAssembler:
    """``response.output_item.done`` items of type ``function_call``, which arrive whole."""

    def feed(self, chunk: Any) -> list[ToolCall]:
        if _get(chunk, "type") != "response.output_item.done":
            return []
        item = _get(chunk, "item")
        if _get(item, "type") != "function_call":
            return []
        return [
            ToolCall(
                id=str(_get(item, "call_id") or _get(item, "id") or ""),
                name=str(_get(item, "name") or ""),
                arguments=_parse_arguments(_get(item, "arguments")),
            )
        ]

    def finish(self) -> list[ToolCall]:
        return []


_ASSEMBLERS: dict[str, Callable[[], ToolCallAssembler]] = {
    "chat": ChatToolCallAssembler,
    "messages": MessagesToolCallAssembler,
    "responses": ResponsesToolCallAssembler,
}


def assembler_for(api: Api) -> ToolCallAssembler:
    return _ASSEMBLERS[api]()
