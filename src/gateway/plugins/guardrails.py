"""Guardrail backends a plugin provides.

A plugin registers a backend under a name; a request, an organization entry, or
a routing policy then names it as the profile ``<plugin>:<name>`` and gets
everything the guardrails pipeline already does for a service profile: block or
monitor, fail-open or fail-closed, the mandate merge, and the result header.
"""

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

GuardrailDirection = Literal["input", "output"]


@dataclass(frozen=True)
class GuardrailOutcome:
    """What a backend says about one text. ``valid=None`` is an inconclusive check."""

    valid: bool | None
    explanation: Any = None
    score: Any = None


@runtime_checkable
class GuardrailBackend(Protocol):
    """What ``PluginContext.add_guardrail`` takes."""

    async def check(self, text: str, *, direction: GuardrailDirection, kwargs: dict[str, Any]) -> GuardrailOutcome: ...


def profile_name(plugin: str, name: str) -> str:
    """The profile string that names a plugin's backend."""
    return f"{plugin}:{name}"
