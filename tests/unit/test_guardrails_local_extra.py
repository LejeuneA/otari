"""The extra the guardrail catalog and runner tell an operator to install (#1112).

Both name one string and neither can check it: the catalog publishes it as
``missing_extra`` and the runner logs it when an import fails, so an extra that
is not declared here turns both into advice that pip and uv reject. Static
only - reads pyproject.toml, resolves nothing, and imports no model backend.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement

from gateway.services.guardrail_catalog import LOCAL_GUARDRAILS_EXTRA

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Upstream's own aggregate extra. The 31 local guardrails span seven upstream
# extras, so taking the aggregate is what keeps this one name honest.
_UPSTREAM_AGGREGATE = "all"


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())


def _requirement(raws: list[str], name: str) -> Requirement:
    for raw in raws:
        requirement = Requirement(raw)
        if requirement.name == name:
            return requirement
    pytest.fail(f"{name} is not among {raws}")


def _local_extra() -> list[str]:
    extras = _pyproject()["project"]["optional-dependencies"]
    if LOCAL_GUARDRAILS_EXTRA not in extras:
        pytest.fail(
            f"the catalog reports {LOCAL_GUARDRAILS_EXTRA!r} as the extra that makes a local-model "
            f"guardrail runnable, but pyproject.toml declares only {sorted(extras)}. An operator "
            "following that advice gets a resolution error naming an extra nobody ships."
        )
    declared: list[str] = extras[LOCAL_GUARDRAILS_EXTRA]
    return declared


def test_the_extra_the_catalog_names_is_declared() -> None:
    assert _local_extra()


def test_the_extra_carries_every_optional_backend() -> None:
    requirement = _requirement(_local_extra(), "any-guardrail")
    assert requirement.extras == {_UPSTREAM_AGGREGATE}, (
        f"the extra takes any-guardrail{sorted(requirement.extras)}, so a guardrail outside those "
        "families stays unrunnable while the catalog keeps naming this one extra as its remedy."
    )


def test_the_extra_tracks_the_base_pin() -> None:
    """One distribution resolved twice, so a ceiling raised in one place is raised in both."""
    base = _requirement(_pyproject()["project"]["dependencies"], "any-guardrail")
    local = _requirement(_local_extra(), "any-guardrail")
    assert local.specifier == base.specifier, (
        f"any-guardrail is pinned '{base.specifier}' as a dependency and '{local.specifier}' in the "
        f"{LOCAL_GUARDRAILS_EXTRA} extra. Installing the extra would then move the version the base "
        "install resolved."
    )
