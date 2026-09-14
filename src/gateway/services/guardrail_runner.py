"""Run a guardrail inside this process, rather than against the sidecar.

`services/guardrails.py` sends a profile to an operator-run container over
``POST /validate``. That container holds the guardrails, built from its own YAML
at boot, which is why a profile there is a name this repository cannot describe
and why adding one means editing a file and restarting a container.

This module is the other half of that story (otari#1108): given the
``any_guardrail`` class to build and the arguments to build it with, it
constructs the guardrail here and calls it here. Nothing routes to it yet. The
store that supplies those arguments is otari#1111, and the request path chooses
between this and the HTTP call in otari#1113.

Three things shape the code:

* ``validate`` is a plain synchronous ``def`` upstream, and so is ``create``,
  which for a model-backed guardrail imports torch and loads weights. Both are
  offloaded to a worker thread. Doing either on the event loop would freeze every
  concurrent request in the process for as long as it took.
* A thread cannot be cancelled. When the deadline passes, the request is answered
  as unavailable and the thread runs to completion regardless. A build is
  therefore *shielded*, so the model it loaded is kept rather than discarded: the
  request that paid for a cold start fails, and the next one is served from the
  cache instead of starting the same load again.
* Every failure becomes :class:`GuardrailsNotReachableError`, so the fail-open and
  fail-closed handling in ``run_input_guardrails`` governs an in-process guardrail
  exactly as it governs a remote one, and the caller is told only the profile name.

The offload uses the process-wide default executor, shared with file extraction
and OCR (``services/file_extractors.py``). ``web_search_backend.py`` took a pool
of its own rather than pay that; this does not, because a guardrail check blocks a
caller who is waiting while an upload can queue. An ungated call still waiting
when its deadline passes is discarded rather than run, and a gated one is capped
at one outstanding call per guardrail, so the worst case is the pool's workers
all busy at once rather than a growing backlog.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from any_guardrail import AnyGuardrail, EvaluateArgumentError, Guardrail
from any_guardrail.base import GuardrailName
from any_guardrail.registry import GUARDRAIL_METADATA
from any_guardrail.types import BackendType

from gateway.log_config import logger
from gateway.models.guardrails import GuardrailConfig
from gateway.services.guardrail_catalog import _backend_availability
from gateway.services.guardrails import (
    _DEFAULT_TIMEOUT_S,
    GuardrailResult,
    GuardrailsNotReachableError,
    _unevaluated_detail,
)

_CacheKey = tuple[str, str]

# Sentinel, because a guardrail reporting no verdict at all and one reporting an
# explicit `None` are different failures: the first is malformed, the second is a
# legitimate inconclusive result that must not block.
_NO_VERDICT = object()


@dataclass(frozen=True)
class GuardrailDefinition:
    """A guardrail this gateway builds and runs itself.

    Deliberately not fields on :class:`GuardrailConfig`. That model is the request
    body, so anything on it is something a caller can send, and ``create_kwargs``
    is where a vendor API key and endpoint live: a caller who could set it could
    point a check at a server of their own and have this gateway post the prompt
    there. ``ResolvedOrganizationGuardrail`` keeps a credential beside a config for
    the same reason.

    The mappings are excluded from the generated hash because a ``dict`` cannot be
    hashed, and ``frozen=True`` would otherwise build a ``__hash__`` that raises
    the first time an instance reached a set. Equality stays by value.
    """

    guardrail_name: str
    create_kwargs: Mapping[str, Any] = field(default_factory=dict, hash=False)
    validate_kwargs: Mapping[str, Any] = field(default_factory=dict, hash=False)


@dataclass
class _Entry:
    """One built guardrail, and the gate that decides whether calls may overlap.

    ``gate`` is ``None`` when calls may run concurrently. It is held for as long
    as the worker thread runs rather than for as long as a caller waits, so see
    :meth:`GuardrailRunner._evaluate` rather than wrapping it in ``async with``.
    """

    guardrail: Guardrail
    gate: asyncio.Lock | None


def _cache_key(definition: GuardrailDefinition) -> _CacheKey:
    """The guardrail class plus a digest of what it was constructed with.

    A digest rather than the values, because ``create_kwargs`` holds secrets and a
    cache key ends up in memory dumps and, by accident, in logs. Keys are sorted so
    one configuration written in two field orders does not build twice. A value
    JSON cannot encode raises here, which the caller turns into an unavailable
    verdict: a live SDK session is not something a stable key can be made from.
    """
    payload = json.dumps(dict(definition.create_kwargs), sort_keys=True, separators=(",", ":"))
    return definition.guardrail_name, hashlib.sha256(payload.encode()).hexdigest()


def _gate_for(name: GuardrailName) -> asyncio.Lock | None:
    """Whether two requests may be inside this guardrail at the same time.

    A hosted-API guardrail is an HTTP call and is free to overlap, which is what
    ``None`` says. A local model is one object shared by every request that reaches
    it, and a transformers pipeline is not documented as thread safe, so those are
    serialized per instance. The gate is per entry, so a slow local model does not
    queue an unrelated guardrail.
    """
    metadata = GUARDRAIL_METADATA.get(name)
    if metadata is not None and metadata.backend is BackendType.HOSTED_API:
        return None
    return asyncio.Lock()


def _release(gate: asyncio.Lock, task: asyncio.Task[object]) -> None:
    """Free a gated guardrail once the thread inside it has actually finished."""
    gate.release()
    if not task.cancelled():
        # Read it, so a failure nobody is left to await is not reported as an
        # exception that was never retrieved.
        task.exception()


async def _build(name: GuardrailName, definition: GuardrailDefinition) -> _Entry:
    """Construct the guardrail, and pair it with the gate that guards that object.

    The two are made together so they cannot come apart. Every caller awaiting one
    build gets this one entry, so a guardrail that may not be called twice at once
    has exactly one lock no matter how many requests were waiting for it or whether
    the cache ended up keeping it.

    Only the construction goes to a thread: it imports the guardrail's module and,
    for a local model, loads weights.
    """
    guardrail = await asyncio.to_thread(AnyGuardrail.create, name, **definition.create_kwargs)
    return _Entry(guardrail=guardrail, gate=_gate_for(name))


def _verdict(output: object, cfg: GuardrailConfig) -> GuardrailResult:
    """Map an ``any_guardrail`` output onto the result the request path reads.

    Typed as ``object`` rather than ``GuardrailOutput`` because the shape checks
    are real: this is a third-party return value under a ``>=0.7.7,<0.8.0`` floor,
    and the same checks the HTTP path makes on a response body apply to it.
    ``categories``, ``spans`` and ``usage`` are dropped; ``GuardrailResult`` has no
    home for them.
    """
    if isinstance(output, list):
        if not output:
            raise GuardrailsNotReachableError(
                f"guardrail profile {cfg.profile!r} returned an empty result list",
                public_detail=_unevaluated_detail(cfg.profile),
            )
        output = output[0]

    valid = getattr(output, "valid", _NO_VERDICT)
    if valid is _NO_VERDICT:
        raise GuardrailsNotReachableError(
            f"guardrail profile {cfg.profile!r} returned no verdict",
            public_detail=_unevaluated_detail(cfg.profile),
        )
    if valid is not None and not isinstance(valid, bool):
        raise GuardrailsNotReachableError(
            f"guardrail profile {cfg.profile!r} returned a non-boolean verdict",
            public_detail=_unevaluated_detail(cfg.profile),
        )

    return GuardrailResult(
        profile=cfg.profile,
        mode=cfg.mode,
        valid=valid,
        explanation=getattr(output, "explanation", None),
        score=getattr(output, "score", None),
    )


class GuardrailRunner:
    """Builds guardrails from ``any_guardrail`` and runs them in this process.

    One instance serves the process and holds every guardrail it has built. It is
    not itself a guardrail: a request carrying two profiles calls :meth:`run` twice,
    and the two built objects sit side by side in the one cache.

    Create it from inside a running event loop, not at import time. It holds
    ``asyncio`` locks and tasks, and those bind to the loop that first uses them, so
    an instance built at import would break under a second loop.
    """

    def __init__(self, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s
        # No capacity limit, because the deployment already is one: a key reaches
        # `_built` only after some profile claimed it, and `_forget` drops it as
        # soon as none does, so the cache holds at most one entry per profile the
        # operator has defined. What that does not bound is size, since each entry
        # may be a loaded model, and nothing here releases one that has gone quiet.
        # Capping resident models is otari#1119.
        self._built: dict[_CacheKey, _Entry] = {}
        self._inflight: dict[_CacheKey, asyncio.Task[_Entry]] = {}
        self._keys: dict[str, _CacheKey] = {}
        self._lock = asyncio.Lock()

    async def run(
        self, *, definition: GuardrailDefinition, cfg: GuardrailConfig, input_text: str
    ) -> GuardrailResult:
        """Check ``input_text`` against one guardrail, building it if needed.

        The deadline covers the build and the check together, so a caller waits no
        longer than they would on the HTTP path. A cold start therefore tends to
        exhaust it: that request is answered as unavailable while the build carries
        on, and the request after it is served from the cache.

        Every failure raises :class:`GuardrailsNotReachableError`. Its message is
        for the log and names the profile, the guardrail and the failure's type;
        ``public_detail`` names the profile and nothing else.
        """
        try:
            name = GuardrailName(definition.guardrail_name)
        except ValueError as exc:
            raise GuardrailsNotReachableError(
                f"guardrail profile {cfg.profile!r} names an unknown guardrail "
                f"{definition.guardrail_name!r}",
                public_detail=_unevaluated_detail(cfg.profile),
            ) from exc

        try:
            return await asyncio.wait_for(self._check(name, definition, cfg, input_text), self._timeout_s)
        except GuardrailsNotReachableError:
            raise
        except TimeoutError as exc:
            raise GuardrailsNotReachableError(
                f"guardrail profile {cfg.profile!r} ({name.value}) did not finish within {self._timeout_s}s",
                public_detail=_unevaluated_detail(cfg.profile),
            ) from exc
        except ImportError as exc:
            raise self._missing_packages(name, cfg, exc) from exc
        except EvaluateArgumentError as exc:
            # The one third-party message carried whole. Upstream builds it from
            # argument *names* and never their values, and it is the only text that
            # tells an operator which field they left out.
            raise GuardrailsNotReachableError(
                f"guardrail profile {cfg.profile!r} ({name.value}) was called wrongly: {exc}",
                public_detail=_unevaluated_detail(cfg.profile),
            ) from exc
        except Exception as exc:
            # Broad on purpose, against the usual rule: a guardrail's own
            # dependencies raise whatever they like, and none of it may reach the
            # caller as a 500. The type is named and the text is not, because a
            # vendor SDK echoes the arguments it was handed, and those hold the key.
            raise GuardrailsNotReachableError(
                f"guardrail profile {cfg.profile!r} ({name.value}) failed in-process: {type(exc).__name__}",
                public_detail=_unevaluated_detail(cfg.profile),
            ) from exc

    def evict(self, profile_name: str) -> None:
        """Forget what was built for ``profile_name``.

        Called by whatever writes a guardrail's definition, so an edited profile
        does not keep answering from the instance built out of its old arguments.
        Synchronous and unlocked: every mutation here happens on the event loop
        thread. Evicting mid-build drops the in-flight result too, though a caller
        already waiting on it is still served.
        """
        key = self._keys.pop(profile_name, None)
        if key is not None:
            self._forget(key)

    async def _check(
        self, name: GuardrailName, definition: GuardrailDefinition, cfg: GuardrailConfig, input_text: str
    ) -> GuardrailResult:
        entry = await self._entry(name, definition, cfg.profile)
        # The caller's arguments win, matching the sidecar's documented contract.
        # A mandated profile's entry already carries the operator's, because
        # `_overlay_mandate` replaced the caller's before the request got here.
        kwargs = {**definition.validate_kwargs, **cfg.validate_kwargs}
        return _verdict(await self._evaluate(entry, name, input_text, kwargs), cfg)

    async def _evaluate(
        self, entry: _Entry, name: GuardrailName, input_text: str, kwargs: dict[str, Any]
    ) -> object:
        """Call the guardrail on a worker thread, gated if it may not overlap.

        The gate is released by the thread finishing, not by this coroutine
        returning. Those are not the same moment: when the deadline passes, this
        coroutine is cancelled while the thread runs on, because a thread cannot be
        stopped. Releasing on the way out would let the next request start a second
        call through the same model object, which is the overlap the gate exists to
        prevent. So the lock is taken by hand and handed to the task's completion,
        and the abandoned caller still returns at its deadline rather than waiting
        for a check whose answer nobody wants.
        """
        call = functools.partial(AnyGuardrail.evaluate, name, entry.guardrail, input_text, **kwargs)
        gate = entry.gate
        if gate is None:
            return await asyncio.to_thread(call)

        await gate.acquire()
        try:
            task = asyncio.ensure_future(asyncio.to_thread(call))
        except BaseException:
            gate.release()
            raise
        task.add_done_callback(functools.partial(_release, gate))
        # Shielded so this caller's deadline ends its own wait and not the call,
        # which would otherwise be cancelled and release the gate early.
        return await asyncio.shield(task)

    async def _entry(self, name: GuardrailName, definition: GuardrailDefinition, profile: str) -> _Entry:
        """The built guardrail for ``definition``, building it at most once."""
        key = _cache_key(definition)
        async with self._lock:
            self._claim(profile, key)
            # Read the cache *after* taking the lock. A finished build lands via a
            # callback that runs between coroutine steps, so it can arrive while
            # this coroutine is parked here, and a check made before the lock would
            # start a second build for a key that is already built.
            entry = self._built.get(key)
            if entry is not None:
                return entry
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.ensure_future(_build(name, definition))
                self._inflight[key] = task
                task.add_done_callback(functools.partial(self._store, key))

        # Shielded, so this caller's deadline ends its own wait and not the build.
        # The task's own result is returned rather than a fresh look in `_built`,
        # because one build makes one entry and therefore one gate. Waiters that
        # arrive together must share it even when the entry was evicted mid-build
        # and never cached, or two of them would call one model object at once.
        return await asyncio.shield(task)

    def _store(self, key: _CacheKey, task: asyncio.Task[_Entry]) -> None:
        """Move a finished build into the cache, unless nothing wants it any more.

        This is what fills the cache, rather than the coroutine that awaited the
        build, because when the deadline passed there may be no such coroutine left.
        """
        if self._inflight.get(key) is not task:
            return  # evicted, or superseded: this result is not ours to keep
        del self._inflight[key]
        if task.cancelled():
            return
        if task.exception() is not None:
            return  # retrieved, so it is never reported as never retrieved
        self._built[key] = task.result()

    def _claim(self, profile: str, key: _CacheKey) -> None:
        """Point ``profile`` at ``key``, releasing whatever it pointed at before.

        A profile should only ever re-key through a store write, which evicts. This
        closes the gap when one does not, for the price of a dict lookup.
        """
        previous = self._keys.get(profile)
        self._keys[profile] = key
        if previous is not None and previous != key:
            self._forget(previous)

    def _forget(self, key: _CacheKey) -> None:
        """Drop ``key`` unless some other profile still resolves to it."""
        if key in self._keys.values():
            return
        self._built.pop(key, None)
        self._inflight.pop(key, None)

    def _missing_packages(
        self, name: GuardrailName, cfg: GuardrailConfig, exc: ImportError
    ) -> GuardrailsNotReachableError:
        """Turn an import failure into an error naming Otari's extra, not the vendor's.

        Upstream re-raises a gated import as ``raise ImportError(msg) from e`` and
        treats a chained cause as its missing-extra signal, so an uncaused one is a
        real bug rather than an uninstalled package and is not reported as one.
        Neither message is quoted: upstream's names the vendor extra and the module.
        """
        if exc.__cause__ is None:
            return GuardrailsNotReachableError(
                f"guardrail profile {cfg.profile!r} ({name.value}) could not be imported",
                public_detail=_unevaluated_detail(cfg.profile),
            )
        _, extra = _backend_availability(name)
        remedy = f"install the {extra!r} extra" if extra else "no backend information for this guardrail"
        logger.warning("Guardrail %r cannot run here: %s", name.value, remedy)
        return GuardrailsNotReachableError(
            f"guardrail profile {cfg.profile!r} ({name.value}) is missing its packages: {remedy}",
            public_detail=_unevaluated_detail(cfg.profile),
        )


# The one runner the process uses, and the one a store write must reach to evict
# a profile it changed. Created on first use rather than at import, because the
# class holds `asyncio` locks and tasks that bind to the loop that first touches
# them: an instance built at import would outlive a lifespan restart and fail
# from inside asyncio under the next loop. The same shape, and the same reason,
# as the pooled client in `services/search_backend.py`.
_runner: GuardrailRunner | None = None


def get_guardrail_runner() -> GuardrailRunner:
    """The process-wide runner, built on the first call from a running loop."""
    global _runner  # noqa: PLW0603

    if _runner is None:
        _runner = GuardrailRunner()
    return _runner


def reset_guardrail_runner() -> None:
    """Drop the runner and everything it has built (shutdown, tests).

    Whatever models it holds become unreachable and are collected; nothing is
    unloaded explicitly, because upstream offers no way to. A no-op when nothing
    ever built one, which is every hybrid deployment until otari#1113.
    """
    global _runner  # noqa: PLW0603

    _runner = None
