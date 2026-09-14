"""Gateway events a plugin can subscribe to.

A plugin registers a handler for an event name through ``PluginContext.subscribe``.
The gateway emits from a few fixed points (the names are the contract, listed
in :data:`EVENT_NAMES`), and every handler runs as its own task, fenced by a
timeout and a ``try``, off the path of whatever emitted it: an emit returns
as soon as the tasks are scheduled, so a slow notifier never delays a response.

Emitters find the bus through a context variable that the app binds per
request (``gateway.main``), so a service deep in the request path can emit
without holding the app. Outside a request, or with no plugin subscribed,
``emit`` is a no-op.
"""

import asyncio
import inspect
import time
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from gateway.log_config import logger

EVENT_NAMES: tuple[str, ...] = (
    "usage.logged",
    "budget.exceeded",
    "key.created",
    "key.deleted",
    "plugin.settings_changed",
)


@dataclass(frozen=True)
class Event:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)


Handler = Callable[[Event], Any]

DEFAULT_EVENT_TIMEOUT_MS = 5_000
# Handlers in flight at once, across every plugin. Past it an emit drops its
# event rather than queue without bound behind a handler that cannot keep up.
MAX_IN_FLIGHT = 1_000


class EventBus:
    """The handlers every loaded plugin subscribed, called with the fences the seam promises."""

    def __init__(
        self, subscriptions: Iterable[tuple[str, str, Handler]] = (), timeout_ms: int = DEFAULT_EVENT_TIMEOUT_MS
    ) -> None:
        self._handlers: dict[str, list[tuple[str, Handler]]] = {}
        for plugin, name, handler in subscriptions:
            self._handlers.setdefault(name, []).append((plugin, handler))
        self._timeout = timeout_ms / 1000
        self._tasks: set[asyncio.Task[None]] = set()
        self._dropped = 0
        self._last_drop_log = 0.0

    def __bool__(self) -> bool:
        return bool(self._handlers)

    def remove_plugin(self, plugin: str) -> None:
        """Drop every handler ``plugin`` subscribed, for a plugin withdrawn after load."""
        for name, handlers in list(self._handlers.items()):
            kept = [(owner, handler) for owner, handler in handlers if owner != plugin]
            if kept:
                self._handlers[name] = kept
            else:
                del self._handlers[name]

    def handlers_for(self, name: str) -> list[tuple[str, Handler]]:
        return [*self._handlers.get(name, []), *self._handlers.get("*", [])]

    async def _run(self, plugin: str, handler: Handler, event: Event) -> None:
        try:
            outcome = handler(event)
            if inspect.isawaitable(outcome):
                await asyncio.wait_for(outcome, timeout=self._timeout)
        except TimeoutError:
            logger.warning(
                "Plugin %s: handler for %s took longer than %.0f ms", plugin, event.name, self._timeout * 1000
            )
        except Exception:  # noqa: BLE001 one plugin's failure must not reach the emitter
            logger.exception("Plugin %s: handler for %s raised", plugin, event.name)

    def emit(self, name: str, **payload: Any) -> int:
        """Schedule every handler for ``name``; return how many were scheduled."""
        handlers = self.handlers_for(name)
        if not handlers:
            return 0
        if len(self._tasks) >= MAX_IN_FLIGHT:
            self._dropped += 1
            now = time.monotonic()
            if now - self._last_drop_log > 10:
                self._last_drop_log = now
                logger.warning(
                    "Plugin events: %d handlers in flight; %s dropped (%d dropped so far)",
                    len(self._tasks),
                    name,
                    self._dropped,
                )
            return 0
        event = Event(name=name, payload=payload)
        loop = asyncio.get_running_loop()
        for plugin, handler in handlers:
            task = loop.create_task(self._run(plugin, handler, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return len(handlers)

    async def drain(self, timeout: float | None = None) -> None:
        """Wait for scheduled handlers, for tests and for shutdown."""
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout if timeout is not None else self._timeout)


current_bus: ContextVar[EventBus | None] = ContextVar("otari_plugin_events", default=None)


def emit(name: str, **payload: Any) -> int:
    """Emit on the bus bound to the current context, or do nothing.

    Sync on purpose: the handlers run as tasks, so an emitter never awaits a
    plugin. Safe to call where no loop is running, where it does nothing.
    """
    bus = current_bus.get()
    if bus is None:
        return 0
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return 0
    return bus.emit(name, **payload)
