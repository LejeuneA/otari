"""The traffic seam: what a plugin sees of an inference request, and how it is asked.

A plugin registers a *traffic observer* through ``PluginContext``. The gateway
builds one provider-neutral :class:`Conversation` per inference request (chat,
messages, or responses shape), asks each observer about it before dispatch, asks
again about every tool call the model produces (buffered per call while a
stream flows), and writes whatever the observers annotated onto the usage row.

Every observer call is fenced: an exception is logged with the plugin's name
and skipped, a call that outlives ``plugins.observer_timeout_ms`` is abandoned
and skipped (an async one is cancelled; a sync one runs in a worker thread, so
the request moves on while it finishes), annotations are capped per plugin,
and nothing at all runs when no plugin registered an observer.

Decisions (a system text to inject, a tool call to deny) are recorded in this
phase and not applied; the ``annotations`` are what reach the usage row.
"""

import asyncio
import hashlib
import inspect
import json
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
class ToolSpec:
    """A tool the request declared the model may call."""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Turn:
    """One assistant message and the tool results the client returned for it.

    ``user_text`` is what the user said before this assistant message, so an
    observer can tell an instruction from a tool result without re-reading the
    wire shape.
    """

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    results: tuple[ToolResult, ...] = ()
    user_text: str = ""


@dataclass(frozen=True)
class Conversation:
    """The request's conversation, the same shape whichever API carried it."""

    api: Api
    model: str
    system: str
    turns: tuple[Turn, ...]
    session_key: str
    latest_user_text: str = ""
    tools: tuple[ToolSpec, ...] = ()

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
# Per plugin, per request: the serialized size its annotations may reach on the row.
MAX_ANNOTATION_BYTES = 16 * 1024
# Keys the gateway writes into a plugin's annotations itself; a plugin cannot set them.
RESERVED_ANNOTATION_KEYS = frozenset({"would_deny"})


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
            try:
                outcome = await asyncio.wait_for(self._call(method, event), timeout=self._timeout)
            except TimeoutError:
                logger.warning(
                    "Plugin %s: %s took longer than %.0f ms and was skipped", name, method_name, self._timeout * 1000
                )
                continue
            except Exception:  # noqa: BLE001 one plugin's failure must not touch the request
                logger.exception("Plugin %s: %s raised and was skipped", name, method_name)
                continue
            if outcome is not None:
                answers.append((name, outcome))
        return answers

    @staticmethod
    async def _call(method: Callable[[Any], Any], event: Any) -> Any:
        """Run one observer method so that the deadline applies whichever way it was written.

        A coroutine function is awaited on the loop. A plain function is run in
        a worker thread: the loop cannot interrupt it, but it can stop waiting,
        so a slow sync observer delays the request by the budget and no more.
        """
        if inspect.iscoroutinefunction(method):
            return await method(event)
        outcome = await asyncio.to_thread(method, event)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return outcome

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

    def _merge(self, name: str, annotations: dict[str, Any], *, gateway: bool = False) -> None:
        if not annotations:
            return
        # The row's column is JSON, and the writer drops the whole batch it is in
        # when one value cannot be serialized; refuse here, per plugin, instead.
        if not isinstance(annotations, dict):
            logger.warning("Plugin %s: annotations must be a dict, got %s; skipped", name, type(annotations).__name__)
            return
        if not gateway and (reserved := RESERVED_ANNOTATION_KEYS.intersection(annotations)):
            logger.warning("Plugin %s: annotation keys %s are the gateway's; dropped", name, sorted(reserved))
            annotations = {key: value for key, value in annotations.items() if key not in reserved}
        try:
            # allow_nan=False: NaN and Infinity pass json.dumps by default and are
            # refused by PostgreSQL's json parser, which would drop the row.
            json.dumps(annotations, allow_nan=False)
        except (TypeError, ValueError) as exc:
            logger.warning("Plugin %s: annotations are not JSON-serializable and were skipped: %s", name, exc)
            return
        current = self.annotations.get(name, {})
        merged = dict(current)
        for key, value in annotations.items():
            if isinstance(value, list) and isinstance(current.get(key), list):
                merged[key] = [*current[key], *value]
            else:
                merged[key] = value
        # The column is read back with every usage row, so one plugin's verbosity
        # must not swell the listing; what was recorded so far is kept.
        size = len(json.dumps(merged, separators=(",", ":")).encode())
        if size > MAX_ANNOTATION_BYTES:
            logger.warning(
                "Plugin %s: annotations would reach %d bytes, over the %d byte cap; this batch was skipped",
                name,
                size,
                MAX_ANNOTATION_BYTES,
            )
            return
        self.annotations[name] = merged

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
                self._merge(
                    name, {"would_deny": [{"tool_call_id": tool_call.id, "message": decision.deny}]}, gateway=True
                )

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


def session_key(*candidates: str | None, system: str = "", first_user_text: str = "", scope: str | None = None) -> str:
    """The first explicit identifier a request carries, else a digest of how it began.

    A digest of the system prompt and first user turn is stable for one agent
    session, since neither changes as the conversation grows, and different for
    two sessions that started differently. ``scope`` (the API key) goes into the
    digest so two callers whose sessions began identically, a shared agent
    template for instance, are not one session. It stays best effort: two
    sessions of one caller that began identically collide.
    """
    for candidate in candidates:
        if candidate:
            return str(candidate)[:200]
    digest = hashlib.sha256(f"{scope or ''}\x1f{system}\x1f{first_user_text}".encode()).hexdigest()[:16]
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


def tool_specs(tools: Iterable[Any] | None) -> tuple[ToolSpec, ...]:
    """The tools a request declared, whichever of the three shapes carried them.

    Chat wraps them as ``{"type": "function", "function": {...}}``; responses
    flattens the same fields onto the item; messages uses ``input_schema``.
    Anything without a name (a provider's built-in tool) is left out.
    """
    specs: list[ToolSpec] = []
    for tool in tools or ():
        function = _get(tool, "function")
        source = function if function is not None else tool
        name = _get(source, "name")
        if not name:
            continue
        parameters = _get(source, "parameters")
        if parameters is None:
            parameters = _get(source, "input_schema")
        specs.append(
            ToolSpec(
                name=str(name),
                description=str(_get(source, "description") or ""),
                parameters=parameters if isinstance(parameters, dict) else {},
            )
        )
    return tuple(specs)


class _TurnBuilder:
    def __init__(self) -> None:
        self.turns: list[Turn] = []
        self._open: dict[str, Any] | None = None
        self._pending_user: list[str] = []
        self.first_user_text = ""
        self.latest_user_text = ""

    def user(self, text: str) -> None:
        if not text:
            return
        self.first_user_text = self.first_user_text or text
        self.latest_user_text = text
        self._pending_user.append(text)

    def assistant(self, text: str, tool_calls: list[ToolCall]) -> None:
        self._close()
        self._open = self._turn(text, tool_calls)

    def tool_call(self, tool_call: ToolCall) -> None:
        if self._open is None:
            self._open = self._turn("", [])
        self._open["tool_calls"].append(tool_call)

    def result(self, result: ToolResult) -> None:
        if self._open is None:
            self._open = self._turn("", [])
        self._open["results"].append(result)

    def _turn(self, text: str, tool_calls: list[ToolCall]) -> dict[str, Any]:
        user_text = "\n".join(self._pending_user)
        self._pending_user = []
        return {"text": text, "tool_calls": tool_calls, "results": [], "user_text": user_text}

    def _close(self) -> None:
        if self._open is not None:
            self.turns.append(
                Turn(
                    text=self._open["text"],
                    tool_calls=tuple(self._open["tool_calls"]),
                    results=tuple(self._open["results"]),
                    user_text=self._open["user_text"],
                )
            )
            self._open = None

    def build(self) -> tuple[Turn, ...]:
        self._close()
        return tuple(self.turns)


def conversation_from_chat(
    model: str, messages: list[Any], *, session: str, scope: str | None = None, tools: Iterable[Any] | None = None
) -> Conversation:
    """OpenAI chat shape: ``system``/``user``/``assistant``/``tool`` roles."""
    system_parts: list[str] = []
    builder = _TurnBuilder()
    for message in messages or []:
        role = _get(message, "role")
        content = _get(message, "content")
        if role in ("system", "developer"):
            system_parts.append(_text_of(content))
        elif role == "user":
            builder.user(_text_of(content))
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
    return _conversation("chat", model, system, builder, session=session, scope=scope, tools=tools)


def conversation_from_messages(
    model: str,
    system: Any,
    messages: list[Any],
    *,
    session: str,
    scope: str | None = None,
    tools: Iterable[Any] | None = None,
) -> Conversation:
    """Anthropic messages shape: ``tool_use`` and ``tool_result`` content blocks."""
    system_text = _text_of(system)
    builder = _TurnBuilder()
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
            builder.user("\n".join(t for t in texts if t))
    return _conversation("messages", model, system_text, builder, session=session, scope=scope, tools=tools)


def conversation_from_responses(
    model: str,
    instructions: Any,
    input_data: Any,
    *,
    session: str,
    scope: str | None = None,
    tools: Iterable[Any] | None = None,
) -> Conversation:
    """OpenAI responses shape: message items plus ``function_call`` / ``function_call_output`` items."""
    system_text = _text_of(instructions)
    builder = _TurnBuilder()
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
                builder.user(text)
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
    return _conversation("responses", model, system_text, builder, session=session, scope=scope, tools=tools)


def _conversation(
    api: Api,
    model: str,
    system: str,
    builder: _TurnBuilder,
    *,
    session: str,
    scope: str | None,
    tools: Iterable[Any] | None,
) -> Conversation:
    return Conversation(
        api=api,
        model=model,
        system=system,
        turns=builder.build(),
        session_key=session_key(session, system=system, first_user_text=builder.first_user_text, scope=scope),
        latest_user_text=builder.latest_user_text,
        tools=tool_specs(tools),
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
