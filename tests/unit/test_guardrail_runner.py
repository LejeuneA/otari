"""Unit tests for the in-process guardrail runner.

``any_guardrail``'s registry is real here, as it is in
``test_guardrail_catalog.py``: ``GuardrailName`` and the backend metadata are what
the runner reads to decide what it is running. What is stubbed is the pair of
calls that would reach a vendor or a model, ``AnyGuardrail.create`` and
``AnyGuardrail.evaluate``, swapped out at the name the runner imported so no test
constructs a guardrail and none loads a model backend.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from gateway.models.guardrails import GuardrailConfig
from gateway.services.guardrail_runner import (
    GuardrailDefinition,
    GuardrailRunner,
    get_guardrail_runner,
    reset_guardrail_runner,
)
from gateway.services.guardrails import GuardrailsNotReachableError

_HOSTED = "lakera_guard"  # BackendType.HOSTED_API: concurrent calls are fine
_LOCAL = "deepset"  # BackendType.LOCAL_ENCODER: calls are serialized per instance


class _Output:
    """Stand-in for ``GuardrailOutput``, which cannot hold ``valid=None``."""

    def __init__(self, valid: bool | None, explanation: str | None = None, score: float | None = None) -> None:
        self.valid = valid
        self.explanation = explanation
        self.score = score


class _Guardrail:
    """Stand-in for a built guardrail. The runner only ever hands it to ``evaluate``."""


def _definition(
    guardrail_name: str = _HOSTED,
    create_kwargs: dict[str, Any] | None = None,
    validate_kwargs: dict[str, Any] | None = None,
) -> GuardrailDefinition:
    return GuardrailDefinition(
        guardrail_name=guardrail_name,
        create_kwargs=create_kwargs if create_kwargs is not None else {"api_key": "sk-secret"},
        validate_kwargs=validate_kwargs or {},
    )


def _config(profile: str = "prompt-injection", **overrides: Any) -> GuardrailConfig:
    return GuardrailConfig(profile=profile, **overrides)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    create: Callable[..., Any] | None = None,
    evaluate: Callable[..., Any] | None = None,
) -> None:
    """Swap the two ``AnyGuardrail`` entry points the runner calls."""

    def _default_create(_name: Any, **_kwargs: Any) -> _Guardrail:
        return _Guardrail()

    def _default_evaluate(_name: Any, _guardrail: Any, _prompt: str, **_kwargs: Any) -> _Output:
        return _Output(True)

    chosen_create = create or _default_create
    chosen_evaluate = evaluate or _default_evaluate

    class _Stub:
        create = staticmethod(chosen_create)
        evaluate = staticmethod(chosen_evaluate)

    monkeypatch.setattr("gateway.services.guardrail_runner.AnyGuardrail", _Stub)


@pytest.mark.asyncio
async def test_a_passing_verdict_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, evaluate=lambda *_a, **_k: _Output(True, "clean", 0.01))
    result = await GuardrailRunner().run(definition=_definition(), cfg=_config(), input_text="hello")

    assert result.valid is True
    assert result.flagged is False
    assert result.profile == "prompt-injection"


@pytest.mark.asyncio
async def test_a_flagged_verdict_carries_its_explanation_and_score(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, evaluate=lambda *_a, **_k: _Output(False, "injection", 0.97))
    result = await GuardrailRunner().run(
        definition=_definition(), cfg=_config(mode="block"), input_text="ignore previous"
    )

    assert result.flagged is True
    assert result.mode == "block"
    assert result.explanation == "injection"
    assert result.score == 0.97


@pytest.mark.asyncio
async def test_an_inconclusive_verdict_does_not_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """``valid=None`` is Otari's tri-state: no verdict, and no block."""
    _install(monkeypatch, evaluate=lambda *_a, **_k: _Output(None))
    result = await GuardrailRunner().run(definition=_definition(), cfg=_config(mode="block"), input_text="hi")

    assert result.valid is None
    assert result.flagged is False


@pytest.mark.asyncio
async def test_a_verdict_without_a_valid_field_is_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, evaluate=lambda *_a, **_k: object())
    with pytest.raises(GuardrailsNotReachableError):
        await GuardrailRunner().run(definition=_definition(), cfg=_config(), input_text="hi")


@pytest.mark.asyncio
async def test_a_non_boolean_verdict_is_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, evaluate=lambda *_a, **_k: _Output("yes"))  # type: ignore[arg-type]
    with pytest.raises(GuardrailsNotReachableError):
        await GuardrailRunner().run(definition=_definition(), cfg=_config(), input_text="hi")


@pytest.mark.asyncio
async def test_a_batch_guardrails_list_output_is_unwrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """``openai_moderation`` answers a single input with a one-element list."""
    _install(monkeypatch, evaluate=lambda *_a, **_k: [_Output(False, "hate", 0.8)])
    result = await GuardrailRunner().run(definition=_definition(), cfg=_config(), input_text="hi")

    assert result.flagged is True
    assert result.explanation == "hate"


@pytest.mark.asyncio
async def test_an_empty_list_output_is_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, evaluate=lambda *_a, **_k: [])
    with pytest.raises(GuardrailsNotReachableError):
        await GuardrailRunner().run(definition=_definition(), cfg=_config(), input_text="hi")


@pytest.mark.asyncio
async def test_a_timeout_tells_the_caller_only_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, evaluate=lambda *_a, **_k: time.sleep(5))
    with pytest.raises(GuardrailsNotReachableError) as caught:
        await GuardrailRunner(timeout_s=0.05).run(
            definition=_definition(), cfg=_config(), input_text="hi"
        )

    assert caught.value.public_detail == "guardrail profile 'prompt-injection' could not be evaluated"
    assert "sk-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_a_missing_extra_names_otaris_extra_and_not_the_vendors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstream's text names ``any-guardrail[huggingface]``; ours must not."""

    def _create(*_a: Any, **_k: Any) -> Any:
        upstream = "Missing packages for HuggingFace provider. Try `pip install 'any-guardrail[huggingface]'`"
        raise ImportError(upstream) from ModuleNotFoundError("torch")

    _install(monkeypatch, create=_create)
    with pytest.raises(GuardrailsNotReachableError) as caught:
        await GuardrailRunner().run(definition=_definition(_LOCAL), cfg=_config(), input_text="hi")

    message = str(caught.value)
    assert "guardrails-local" in message
    assert "huggingface" not in message
    assert "torch" not in message


@pytest.mark.asyncio
async def test_an_uncaused_import_error_is_not_blamed_on_a_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstream's own rule: only a chained ImportError signals a missing extra."""

    def _create(*_a: Any, **_k: Any) -> Any:
        raise ImportError("Could not resolve guardrail class")

    _install(monkeypatch, create=_create)
    with pytest.raises(GuardrailsNotReachableError) as caught:
        await GuardrailRunner().run(definition=_definition(_LOCAL), cfg=_config(), input_text="hi")

    assert "guardrails-local" not in str(caught.value)


@pytest.mark.asyncio
async def test_an_unknown_guardrail_name_is_unevaluable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    with pytest.raises(GuardrailsNotReachableError) as caught:
        await GuardrailRunner().run(definition=_definition("no_such_guardrail"), cfg=_config(), input_text="hi")

    assert caught.value.public_detail == "guardrail profile 'prompt-injection' could not be evaluated"


@pytest.mark.asyncio
async def test_a_vendor_failure_leaks_neither_its_text_nor_the_create_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _evaluate(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("POST https://api.lakera.ai failed for key sk-secret")

    _install(monkeypatch, evaluate=_evaluate)
    with pytest.raises(GuardrailsNotReachableError) as caught:
        await GuardrailRunner().run(definition=_definition(), cfg=_config(), input_text="hi")

    message = str(caught.value)
    assert "sk-secret" not in message
    assert "api.lakera.ai" not in message
    assert "RuntimeError" in message


@pytest.mark.asyncio
async def test_a_missing_per_call_argument_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """``EvaluateArgumentError`` names argument names only, so it travels whole."""
    from any_guardrail import EvaluateArgumentError

    def _evaluate(*_a: Any, **_k: Any) -> Any:
        raise EvaluateArgumentError("any_llm.validate() requires ['policy']")

    _install(monkeypatch, evaluate=_evaluate)
    with pytest.raises(GuardrailsNotReachableError) as caught:
        await GuardrailRunner().run(definition=_definition("any_llm"), cfg=_config(), input_text="hi")

    assert "policy" in str(caught.value)


@pytest.mark.asyncio
async def test_the_caller_wins_a_validate_kwargs_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same rule as the sidecar. A mandated entry's kwargs already replaced the caller's."""
    seen: dict[str, Any] = {}

    def _evaluate(_name: Any, _guardrail: Any, _prompt: str, **kwargs: Any) -> _Output:
        seen.update(kwargs)
        return _Output(True)

    _install(monkeypatch, evaluate=_evaluate)
    await GuardrailRunner().run(
        definition=_definition(validate_kwargs={"threshold": 0.5, "stored_only": True}),
        cfg=_config(validate_kwargs={"threshold": 0.9}),
        input_text="hi",
    )

    assert seen == {"threshold": 0.9, "stored_only": True}


@pytest.mark.asyncio
async def test_one_profile_is_built_once(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = 0

    def _create(*_a: Any, **_k: Any) -> _Guardrail:
        nonlocal builds
        builds += 1
        return _Guardrail()

    _install(monkeypatch, create=_create)
    runner, definition = GuardrailRunner(), _definition()
    await runner.run(definition=definition, cfg=_config(), input_text="one")
    await runner.run(definition=definition, cfg=_config(), input_text="two")

    assert builds == 1


@pytest.mark.asyncio
async def test_two_field_orders_of_one_config_build_once(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = 0

    def _create(*_a: Any, **_k: Any) -> _Guardrail:
        nonlocal builds
        builds += 1
        return _Guardrail()

    _install(monkeypatch, create=_create)
    runner = GuardrailRunner()
    await runner.run(
        definition=_definition(create_kwargs={"api_key": "k", "endpoint": "e"}), cfg=_config(), input_text="one"
    )
    await runner.run(
        definition=_definition(create_kwargs={"endpoint": "e", "api_key": "k"}), cfg=_config(), input_text="two"
    )

    assert builds == 1


@pytest.mark.asyncio
async def test_evicting_a_profile_forces_the_next_run_to_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = 0

    def _create(*_a: Any, **_k: Any) -> _Guardrail:
        nonlocal builds
        builds += 1
        return _Guardrail()

    _install(monkeypatch, create=_create)
    runner, definition = GuardrailRunner(), _definition()
    await runner.run(definition=definition, cfg=_config(), input_text="one")
    runner.evict("prompt-injection")
    await runner.run(definition=definition, cfg=_config(), input_text="two")

    assert builds == 2


@pytest.mark.asyncio
async def test_a_profile_that_rekeys_without_an_evict_drops_its_old_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch)
    runner = GuardrailRunner()
    await runner.run(definition=_definition(create_kwargs={"api_key": "old"}), cfg=_config(), input_text="hi")
    await runner.run(definition=_definition(create_kwargs={"api_key": "new"}), cfg=_config(), input_text="hi")

    assert len(runner._built) == 1


@pytest.mark.asyncio
async def test_a_failed_build_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def _create(*_a: Any, **_k: Any) -> _Guardrail:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("vendor rejected the key")

    _install(monkeypatch, create=_create)
    runner, definition = GuardrailRunner(), _definition()
    for _ in range(2):
        with pytest.raises(GuardrailsNotReachableError):
            await runner.run(definition=definition, cfg=_config(), input_text="hi")

    assert attempts == 2
    assert len(runner._built) == 0


@pytest.mark.asyncio
async def test_a_build_that_outlives_its_timeout_still_lands_in_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of shielding: a slow model load is not repeated forever."""
    builds = 0

    def _create(*_a: Any, **_k: Any) -> _Guardrail:
        nonlocal builds
        builds += 1
        time.sleep(0.3)
        return _Guardrail()

    _install(monkeypatch, create=_create)
    runner, definition = GuardrailRunner(timeout_s=0.05), _definition()
    with pytest.raises(GuardrailsNotReachableError):
        await runner.run(definition=definition, cfg=_config(), input_text="hi")

    await asyncio.sleep(0.5)
    assert len(runner._built) == 1

    result = await runner.run(definition=definition, cfg=_config(), input_text="hi")
    assert result.valid is True
    assert builds == 1


@pytest.mark.asyncio
async def test_two_callers_arriving_together_share_one_build(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = 0

    def _create(*_a: Any, **_k: Any) -> _Guardrail:
        nonlocal builds
        builds += 1
        time.sleep(0.2)
        return _Guardrail()

    _install(monkeypatch, create=_create)
    runner, definition = GuardrailRunner(), _definition()
    first, second = await asyncio.gather(
        runner.run(definition=definition, cfg=_config(), input_text="one"),
        runner.run(definition=definition, cfg=_config(), input_text="two"),
    )

    assert first.valid is True
    assert second.valid is True
    assert builds == 1


@pytest.mark.asyncio
async def test_evicting_during_a_build_serves_the_waiter_and_keeps_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _create(*_a: Any, **_k: Any) -> _Guardrail:
        time.sleep(0.2)
        return _Guardrail()

    _install(monkeypatch, create=_create)
    runner, definition = GuardrailRunner(), _definition()
    pending = asyncio.ensure_future(runner.run(definition=definition, cfg=_config(), input_text="hi"))
    await asyncio.sleep(0.05)
    runner.evict("prompt-injection")

    assert (await pending).valid is True
    await asyncio.sleep(0.3)
    assert len(runner._built) == 0


@pytest.mark.asyncio
async def test_calls_into_one_local_model_do_not_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    peak = _install_overlap_probe(monkeypatch)
    runner, definition = GuardrailRunner(), _definition(_LOCAL, create_kwargs={})
    await asyncio.gather(*(runner.run(definition=definition, cfg=_config(), input_text="hi") for _ in range(4)))

    assert peak["value"] == 1


@pytest.mark.asyncio
async def test_calls_into_one_hosted_api_guardrail_do_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    peak = _install_overlap_probe(monkeypatch)
    runner, definition = GuardrailRunner(), _definition(_HOSTED)
    await asyncio.gather(*(runner.run(definition=definition, cfg=_config(), input_text="hi") for _ in range(4)))

    assert peak["value"] > 1


def _install_overlap_probe(monkeypatch: pytest.MonkeyPatch, seconds: float = 0.05) -> dict[str, int]:
    """Record how many ``evaluate`` calls were ever in flight at once."""
    state = {"value": 0, "active": 0}
    lock = threading.Lock()

    def _evaluate(*_a: Any, **_k: Any) -> _Output:
        with lock:
            state["active"] += 1
            state["value"] = max(state["value"], state["active"])
        time.sleep(seconds)
        with lock:
            state["active"] -= 1
        return _Output(True)

    _install(monkeypatch, evaluate=_evaluate)
    return state


@pytest.mark.asyncio
async def test_an_abandoned_local_check_keeps_its_gate_until_the_thread_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline ends the wait, not the thread. The next call must still queue.

    Releasing the gate when the caller gives up would put a second call through the
    same model object while the first is still inside it.
    """
    probe = _install_overlap_probe(monkeypatch, seconds=0.4)
    runner, definition = GuardrailRunner(timeout_s=0.1), _definition(_LOCAL, create_kwargs={})

    for _ in range(2):
        with pytest.raises(GuardrailsNotReachableError):
            await runner.run(definition=definition, cfg=_config(), input_text="hi")

    await asyncio.sleep(0.6)
    assert probe["value"] == 1


@pytest.mark.asyncio
async def test_the_cache_holds_no_more_entries_than_profiles_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What bounds the cache: a key is kept only while some profile resolves to it."""
    _install(monkeypatch)
    runner = GuardrailRunner()
    for index in range(5):
        await runner.run(
            definition=_definition(create_kwargs={"api_key": f"k{index}"}),
            cfg=_config(f"profile-{index}"),
            input_text="hi",
        )
        # The same profile, re-keyed: it replaces its entry rather than adding one.
        await runner.run(
            definition=_definition(create_kwargs={"api_key": f"rotated-{index}"}),
            cfg=_config(f"profile-{index}"),
            input_text="hi",
        )

    assert len(runner._built) == 5
    for index in range(5):
        runner.evict(f"profile-{index}")
    assert len(runner._built) == 0


@pytest.mark.asyncio
async def test_an_eviction_during_a_build_still_leaves_both_waiters_one_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One build makes one entry, so its gate is shared even when the cache drops it.

    Handing each waiter a gate of its own would put two calls through one model
    object at once, which is the overlap the gate exists to prevent.
    """
    probe = {"value": 0, "active": 0}
    tracker = threading.Lock()

    def _create(*_a: Any, **_k: Any) -> _Guardrail:
        time.sleep(0.15)
        return _Guardrail()

    def _evaluate(*_a: Any, **_k: Any) -> _Output:
        with tracker:
            probe["active"] += 1
            probe["value"] = max(probe["value"], probe["active"])
        time.sleep(0.2)
        with tracker:
            probe["active"] -= 1
        return _Output(True)

    _install(monkeypatch, create=_create, evaluate=_evaluate)
    runner, definition = GuardrailRunner(), _definition(_LOCAL, create_kwargs={})

    waiters = [
        asyncio.ensure_future(runner.run(definition=definition, cfg=_config(), input_text="hi"))
        for _ in range(2)
    ]
    await asyncio.sleep(0.05)  # both are now parked on the one build
    runner.evict("prompt-injection")  # so the finished build is never cached
    results = await asyncio.gather(*waiters)

    assert [result.valid for result in results] == [True, True]
    assert probe["value"] == 1
    assert len(runner._built) == 0


def test_importing_the_runner_loads_no_model_backend() -> None:
    """The base install runs the 9 hosted-API guardrails; nothing here pulls torch in."""
    assert "torch" not in sys.modules
    assert "transformers" not in sys.modules


@pytest.fixture(autouse=True)
def _drop_the_shared_runner() -> Iterator[None]:
    """Never let one test's runner answer another's call.

    Its locks and tasks bind to the loop that first used them, and
    pytest-asyncio gives each test a loop of its own, so a leaked instance would
    fail the next test from inside asyncio rather than where the mistake was.
    """
    reset_guardrail_runner()
    yield
    reset_guardrail_runner()


def test_the_shared_runner_is_one_instance() -> None:
    """The cache only pays for itself if every caller reaches the same one."""
    assert get_guardrail_runner() is get_guardrail_runner()


def test_resetting_drops_the_shared_runner() -> None:
    """Shutdown drops it, so the next lifespan does not inherit the last one's locks."""
    first = get_guardrail_runner()

    reset_guardrail_runner()

    assert get_guardrail_runner() is not first


def test_resetting_twice_is_harmless() -> None:
    """The lifespan's finally runs in hybrid too, where nothing ever built one."""
    reset_guardrail_runner()
    reset_guardrail_runner()


@pytest.mark.asyncio
async def test_the_shared_runner_holds_what_it_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store write reaches this instance, so what it evicts is what serves traffic."""
    builds = 0

    def _create(_name: Any, **_kwargs: Any) -> _Guardrail:
        nonlocal builds
        builds += 1
        return _Guardrail()

    _install(monkeypatch, create=_create)

    await get_guardrail_runner().run(definition=_definition(), cfg=_config(), input_text="hi")
    await get_guardrail_runner().run(definition=_definition(), cfg=_config(), input_text="hi")
    assert builds == 1

    get_guardrail_runner().evict("prompt-injection")
    await get_guardrail_runner().run(definition=_definition(), cfg=_config(), input_text="hi")
    assert builds == 2
