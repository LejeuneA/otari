"""A bootstrap's own Alembic chain runs on startup, beside Otari's and never in it.

``init_db`` upgrades Otari's chain to ``head`` and then each contributed chain,
on the same database, each stamping the version table its contribution names.
The fixture chain under ``tests/fixtures/plugin_alembic`` is the shape a plugin
ships: an ``env.py`` honoring the ``version_table`` attribute and one revision
creating a table of its own.
"""

import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from click.testing import CliRunner
from sqlalchemy import create_engine, inspect, text

from gateway.container import MigrationContribution
from gateway.core.config import GatewayConfig
from gateway.core.database import init_db, reset_db

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE_ALEMBIC = _REPO_ROOT / "alembic"
_PLUGIN_ALEMBIC = _REPO_ROOT / "tests" / "fixtures" / "plugin_alembic"

PLUGIN = MigrationContribution(
    name="plugin_demo",
    script_location=str(_PLUGIN_ALEMBIC),
    version_table="plugin_demo_alembic_version",
)


def _head(script_location: Path) -> str:
    config = Config()
    config.set_main_option("script_location", str(script_location))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _version_rows(url: str, version_table: str) -> list[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return [row[0] for row in connection.execute(text(f"SELECT version_num FROM {version_table}"))]
    finally:
        engine.dispose()


def test_init_db_runs_the_core_chain_then_each_contributed_chain(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'with-plugin.db'}"

    init_db(GatewayConfig(database_url=url, auto_migrate=True), migration_contributions=(PLUGIN,))
    reset_db()

    # Core schema and the plugin's table, side by side in one database.
    assert {"users", "api_keys", "plugin_demo"} <= _tables(url)
    # Each chain's history lives in its own table and holds only its own head.
    assert _version_rows(url, "alembic_version") == [_head(_CORE_ALEMBIC)]
    assert _version_rows(url, PLUGIN.version_table) == [_head(_PLUGIN_ALEMBIC)]


def test_init_db_with_no_contribution_leaves_no_contributed_table(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'core-only.db'}"

    init_db(GatewayConfig(database_url=url, auto_migrate=True))
    reset_db()

    tables = _tables(url)
    assert "users" in tables
    assert "plugin_demo" not in tables
    assert PLUGIN.version_table not in tables


def test_a_second_boot_finds_both_chains_at_head(tmp_path: Path) -> None:
    """Re-running is idempotent: neither chain re-applies or re-stamps."""
    url = f"sqlite:///{tmp_path / 'twice.db'}"
    config = GatewayConfig(database_url=url, auto_migrate=True)

    for _ in range(2):
        init_db(config, migration_contributions=(PLUGIN,))
        reset_db()

    assert _version_rows(url, "alembic_version") == [_head(_CORE_ALEMBIC)]
    assert _version_rows(url, PLUGIN.version_table) == [_head(_PLUGIN_ALEMBIC)]


def test_auto_migrate_off_runs_no_chain(tmp_path: Path) -> None:
    db_path = tmp_path / "no-migrate.db"

    init_db(GatewayConfig(database_url=f"sqlite:///{db_path}", auto_migrate=False), migration_contributions=(PLUGIN,))
    reset_db()

    # The async engine is lazy, so with no chain run nothing has created the file.
    assert not db_path.exists()


def test_the_migrate_command_runs_the_contributed_chain_on_a_real_database(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    """``otari migrate`` is the whole schema, not just Otari's half.

    The command a deployment with ``auto_migrate`` off runs by hand. Asserted
    against a real database rather than a stubbed runner, because what broke
    here before was the command reaching a different chain-runner than the boot
    path did.
    """
    import gateway.cli as gateway_cli

    (tmp_path / "demo_bootstrap.py").write_text(
        "from gateway.container import MigrationContribution\n\n\n"
        "def register(container):\n"
        "    container.contribute_migrations(\n"
        f"        MigrationContribution(name={PLUGIN.name!r}, script_location={PLUGIN.script_location!r}, "
        f"version_table={PLUGIN.version_table!r})\n"
        "    )\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("demo_bootstrap", None)

    url = f"sqlite:///{tmp_path / 'cli-migrated.db'}"
    monkeypatch.setattr(
        gateway_cli,
        "load_config",
        lambda config_path=None: GatewayConfig(
            database_url=url, auto_migrate=False, bootstrap="demo_bootstrap:register"
        ),
    )

    result = CliRunner().invoke(gateway_cli.cli, ["migrate"])
    sys.modules.pop("demo_bootstrap", None)

    assert result.exit_code == 0, result.output
    tables = _tables(url)
    assert "alembic_version" in tables and PLUGIN.version_table in tables
    assert "plugin_demo" in tables
    assert _version_rows(url, PLUGIN.version_table) == [_head(_PLUGIN_ALEMBIC)]
