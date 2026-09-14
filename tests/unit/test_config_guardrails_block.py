"""The ``guardrails:`` block in config.yml, validated at load.

A guardrail is defined in the database and edited in the dashboard. This block
is the read-only baseline beside it, the way ``providers:`` and ``search_tools:``
already are, and it is the *only* definition source a hybrid gateway has: hybrid
skips ``init_db``, mounts no management router and serves no dashboard, so there
is no row to read and no page to write one on (otari#1108).

Validated at load rather than at first use, because a guardrail that will not
build is one a request finds out about by failing closed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gateway.core.config import load_config

_MINIMAL = """
master_key: test-master-key
database_url: sqlite+aiosqlite:///./test.db
"""


def _config_file(tmp_path: Path, body: str) -> str:
    path = tmp_path / "config.yml"
    path.write_text(_MINIMAL + body, encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Load from the file alone, with no developer's own environment leaking in.

    Every ``OTARI_`` name, not a list of the ones that bite today: ``load_config``
    layers a scalar override for any field, and reads ``OTARI_CONFIG_YAML`` and
    ``OTARI_CONFIG_B64`` as whole config sources. A named list would silently stop
    covering a field somebody adds later. ``chdir`` is what keeps the ``.env`` in
    the repository root out of it.
    """
    monkeypatch.chdir(tmp_path)
    for name in [key for key in os.environ if key.startswith("OTARI_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LAKERA_API_KEY", raising=False)


def test_no_block_means_no_guardrails(tmp_path: Path) -> None:
    """The block is optional, so a config without one is not a config with an error."""
    config = load_config(_config_file(tmp_path, ""))

    assert config.guardrails == {}


def test_a_valid_block_loads(tmp_path: Path) -> None:
    config = load_config(
        _config_file(
            tmp_path,
            """
guardrails:
  prompt-injection:
    guardrail_name: lakera_guard
    create_kwargs:
      api_key: lakera-secret
      breakdown: true
    validate_kwargs:
      payload: true
""",
        )
    )

    entry = config.guardrails["prompt-injection"]
    assert entry["guardrail_name"] == "lakera_guard"
    assert entry["create_kwargs"] == {"api_key": "lakera-secret", "breakdown": True}
    assert entry["validate_kwargs"] == {"payload": True}


def test_enabled_is_optional_and_must_be_a_boolean(tmp_path: Path) -> None:
    """Omitting it means enabled; the service applies that default, not the loader.

    The loader leaves the key absent rather than filling it in, so one place
    decides what absent means.
    """
    config = load_config(
        _config_file(
            tmp_path,
            """
guardrails:
  quiet:
    guardrail_name: lakera_guard
    create_kwargs: {api_key: k}
    enabled: false
""",
        )
    )
    assert config.guardrails["quiet"]["enabled"] is False

    with pytest.raises(ValueError, match="guardrails.quiet.enabled must be true or false"):
        load_config(
            _config_file(
                tmp_path,
                """
guardrails:
  quiet:
    guardrail_name: lakera_guard
    create_kwargs: {api_key: k}
    enabled: "no"
""",
            )
        )


def test_env_interpolation_reaches_a_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``${VAR}`` is how an operator keeps the key out of the file they commit."""
    monkeypatch.setenv("LAKERA_API_KEY", "lakera-from-env")

    config = load_config(
        _config_file(
            tmp_path,
            """
guardrails:
  prompt-injection:
    guardrail_name: lakera_guard
    create_kwargs:
      api_key: "${LAKERA_API_KEY}"
""",
        )
    )

    assert config.guardrails["prompt-injection"]["create_kwargs"]["api_key"] == "lakera-from-env"


def test_an_unknown_guardrail_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="is not a guardrail this gateway ships"):
        load_config(
            _config_file(
                tmp_path,
                """
guardrails:
  typo:
    guardrail_name: lakera-guard
""",
            )
        )


def test_guardrail_name_is_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="guardrails.nameless.guardrail_name is required"):
        load_config(_config_file(tmp_path, "guardrails:\n  nameless:\n    create_kwargs: {}\n"))


def test_a_name_used_as_a_path_segment_may_not_contain_a_slash(tmp_path: Path) -> None:
    """The name reaches ``/api/v1/guardrail-credentials/{name}`` once a row shares it."""
    with pytest.raises(ValueError, match="must not contain '/'"):
        load_config(
            _config_file(
                tmp_path,
                "guardrails:\n  a/b:\n    guardrail_name: lakera_guard\n    create_kwargs: {api_key: k}\n",
            )
        )


@pytest.mark.parametrize("field", ["create_kwargs", "validate_kwargs"])
def test_kwargs_must_be_mappings(tmp_path: Path, field: str) -> None:
    with pytest.raises(ValueError, match=f"guardrails.bad.{field} must be a mapping"):
        load_config(
            _config_file(
                tmp_path,
                f"guardrails:\n  bad:\n    guardrail_name: lakera_guard\n    {field}: [1, 2]\n",
            )
        )


def test_an_entry_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    """The field's own type catches this before the entry validator runs.

    Asserted anyway, because "refused at load" is the contract; which of the two
    layers refuses it is an implementation detail that may move.
    """
    with pytest.raises(ValueError, match="valid dictionary"):
        load_config(_config_file(tmp_path, "guardrails:\n  oops: lakera_guard\n"))


def test_an_unknown_constructor_argument_is_refused(tmp_path: Path) -> None:
    """No guardrail takes ``**kwargs``, so this would be a TypeError at build time.

    Caught at load instead, where it names the field the operator mistyped.
    """
    with pytest.raises(ValueError, match="guardrails.typo.create_kwargs.api_ky is not an argument"):
        load_config(
            _config_file(
                tmp_path,
                "guardrails:\n  typo:\n    guardrail_name: lakera_guard\n    create_kwargs: {api_ky: k}\n",
            )
        )


def test_a_missing_required_constructor_argument_is_refused(tmp_path: Path) -> None:
    """``guardrail_identifier`` has no default, so Bedrock cannot be built without it."""
    with pytest.raises(ValueError, match="guardrails.aws.create_kwargs.guardrail_identifier is required"):
        load_config(
            _config_file(
                tmp_path,
                "guardrails:\n  aws:\n    guardrail_name: bedrock_guardrails\n"
                "    create_kwargs: {region_name: us-east-1}\n",
            )
        )


def test_an_argument_an_environment_variable_can_supply_is_not_required(tmp_path: Path) -> None:
    """Lakera's ``api_key`` is effectively required, not signature-required.

    Upstream reads it from ``LAKERA_API_KEY`` when it is absent, so demanding it
    here would refuse a deployment that configures the key the documented way.
    """
    config = load_config(
        _config_file(tmp_path, "guardrails:\n  from-env:\n    guardrail_name: lakera_guard\n")
    )

    # Left absent rather than filled in, the same rule ``enabled`` follows.
    assert "create_kwargs" not in config.guardrails["from-env"]


def test_a_guardrail_with_no_constructor_arguments_loads(tmp_path: Path) -> None:
    """``any_llm`` takes everything per call, so its create stage is empty."""
    config = load_config(_config_file(tmp_path, "guardrails:\n  judge:\n    guardrail_name: any_llm\n"))

    assert config.guardrails["judge"]["guardrail_name"] == "any_llm"


def test_a_hybrid_config_still_carries_the_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the block: hybrid has no database, so this is its only source."""
    monkeypatch.setenv("OTARI_AI_TOKEN", "platform-token")

    config = load_config(
        _config_file(
            tmp_path,
            "guardrails:\n  prompt-injection:\n    guardrail_name: lakera_guard\n    create_kwargs: {api_key: k}\n",
        )
    )

    assert config.is_hybrid_mode
    assert config.guardrails["prompt-injection"]["guardrail_name"] == "lakera_guard"
