"""The plugin seam: manifests, discovery, loading, archives, and the marketplace.

Covers the mechanism without a database or a running app. The end-to-end path
(a directory plugin's routes, page, and listing on a booted app) is in
``tests/integration/test_plugins_route.py``.
"""

import io
import sys
import tarfile
import zipfile
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest

from gateway.core.config import GatewayConfig
from gateway.models.plugins import MarketplaceConfig, PluginManifestError, PluginsConfig, parse_manifest
from gateway.plugins import PluginContext, load_plugins
from gateway.plugins.archive import (
    PluginInstallError,
    github_archive_url,
    install_archive,
    remove_installed,
)
from gateway.plugins.discovery import discover_directory_plugins, discover_plugins, find_manifest_in_tree
from gateway.plugins.marketplace import Marketplace

MANIFEST = """
[plugin]
name = "probe"
version = "1.2.3"
description = "A probe."
package = "probe_plugin"
homepage = "https://github.com/example/probe"
contributes = ["routes", "cli", "ui"]
config_keys = ["judge"]

[plugin.ui]
path = "static"
label = "Probe"
"""

PACKAGE = '''
import click
from fastapi import APIRouter
from gateway.plugins import PluginContext

router = APIRouter(prefix="/probe")


@router.get("")
async def probe() -> dict[str, str]:
    return {"probe": "ok"}


@click.group()
def probe_cli() -> None:
    """Probe commands."""


SEEN_CONFIG = {}


def register(ctx: PluginContext) -> None:
    SEEN_CONFIG.update(ctx.config)
    ctx.add_router(router, auth="none")
    ctx.add_cli(probe_cli)
'''


def write_plugin(root: Path, name: str = "probe", package: str = "probe_plugin", body: str = PACKAGE) -> Path:
    """Lay out one directory plugin under ``root/<name>`` and return its package dir."""
    package_dir = root / name / package
    package_dir.mkdir(parents=True)
    manifest = MANIFEST.replace('name = "probe"', f'name = "{name}"').replace(
        'package = "probe_plugin"', f'package = "{package}"'
    )
    if "add_migrations" in body:
        manifest = manifest.replace(
            'contributes = ["routes", "cli", "ui"]', 'contributes = ["routes", "cli", "ui", "migrations"]'
        )
    (package_dir / "otari-plugin.toml").write_text(manifest)
    (package_dir / "__init__.py").write_text(body)
    (package_dir / "static").mkdir()
    (package_dir / "static" / "index.html").write_text("<html>probe</html>")
    return package_dir


@pytest.fixture(autouse=True)
def _forget_probe_packages() -> Generator[None]:
    """Keep one test's imported plugin package, and its sys.path entry, from serving the next."""
    path_before = list(sys.path)
    yield
    sys.path[:] = path_before
    for name in [
        module for module in sys.modules if module.startswith("probe_plugin") or module.startswith("broken_plugin")
    ]:
        del sys.modules[name]


def config_for(directory: Path, **plugins: object) -> GatewayConfig:
    return GatewayConfig(master_key="k", plugins=PluginsConfig(directory=str(directory), **plugins))  # type: ignore[arg-type]


# --- manifest ---------------------------------------------------------------


def test_manifest_parses_and_derives_the_version_table() -> None:
    manifest = parse_manifest(MANIFEST)

    assert manifest.name == "probe"
    assert manifest.package == "probe_plugin"
    assert manifest.ui is None  # the short form is normalized into one page
    assert [page.label for page in manifest.pages] == ["Probe"]
    assert manifest.pages[0].section == "extend"
    assert manifest.plugin_api == 1
    assert manifest.modes == ["standalone", "hosted", "hybrid"]
    assert manifest.version_table == "alembic_version_probe"


@pytest.mark.parametrize(
    "text",
    [
        "not toml [",
        "[other]\nname = 'x'",
        '[plugin]\nname = "Bad Name"\nversion = "1"\npackage = "p"',
        '[plugin]\nname = "ok"\nversion = "1"\npackage = "not a module"',
        '[plugin]\nname = "ok"\nversion = "1"\npackage = "p"\nentrypoint = "1bad"',
    ],
)
def test_manifest_rejects_malformed_input(text: str) -> None:
    with pytest.raises(PluginManifestError):
        parse_manifest(text)


@pytest.mark.parametrize("name", ["install", "upload", "marketplace", "directory", "disabled"])
def test_manifest_rejects_a_name_the_core_already_answers_to(name: str) -> None:
    with pytest.raises(PluginManifestError, match="reserved"):
        parse_manifest(MANIFEST.replace('name = "probe"', f'name = "{name}"'))


def test_manifest_keeps_the_version_table_inside_the_postgres_identifier_limit() -> None:
    longest = parse_manifest(MANIFEST.replace('name = "probe"', f'name = "{"a" * 47}"'))
    assert len(longest.version_table) == 63

    with pytest.raises(PluginManifestError):
        parse_manifest(MANIFEST.replace('name = "probe"', f'name = "{"a" * 48}"'))


def test_plugins_config_hands_a_plugin_its_own_block() -> None:
    config = PluginsConfig.model_validate({"directory": "x", "probe": {"judge": "claude"}, "other": 1})

    assert config.plugin_settings("probe") == {"judge": "claude"}
    assert config.plugin_settings("other") == {}
    assert config.plugin_settings("missing") == {}


# --- discovery --------------------------------------------------------------


def test_discovery_describes_a_directory_plugin_without_importing_it(tmp_path: Path) -> None:
    write_plugin(tmp_path, body="raise RuntimeError('must not import')")

    found, problems = discover_directory_plugins(tmp_path)

    assert problems == []
    assert [plugin.manifest.name for plugin in found] == ["probe"]
    assert found[0].install_dir == tmp_path / "probe"
    assert found[0].package_dir == tmp_path / "probe" / "probe_plugin"


def test_discovery_accepts_a_src_layout_under_one_extra_directory(tmp_path: Path) -> None:
    # <install>/repo-main/src/probe_plugin/otari-plugin.toml, the shape a GitHub
    # archive of a src-layout repository unpacks to.
    package_dir = tmp_path / "probe" / "repo-main" / "src" / "probe_plugin"
    package_dir.mkdir(parents=True)
    (package_dir / "otari-plugin.toml").write_text(MANIFEST)

    assert find_manifest_in_tree(tmp_path / "probe") == package_dir / "otari-plugin.toml"
    found, problems = discover_directory_plugins(tmp_path)
    assert problems == []
    assert found[0].package_dir == package_dir


def test_discovery_reports_a_manifest_that_names_another_package(tmp_path: Path) -> None:
    package_dir = tmp_path / "probe" / "elsewhere"
    package_dir.mkdir(parents=True)
    (package_dir / "otari-plugin.toml").write_text(MANIFEST)

    found, problems = discover_directory_plugins(tmp_path)

    assert found == []
    assert len(problems) == 1
    assert "sits in 'elsewhere'" in problems[0].error


def test_discovery_keeps_the_first_of_two_plugins_with_one_name(tmp_path: Path) -> None:
    write_plugin(tmp_path, name="probe")
    # A second install directory whose manifest claims the same name.
    write_plugin(tmp_path / "staging", name="probe")
    (tmp_path / "staging" / "probe").rename(tmp_path / "zz-probe-copy")

    found, problems = discover_plugins(tmp_path)

    assert [plugin.manifest.name for plugin in found] == ["probe"]
    assert found[0].install_dir == tmp_path / "probe"
    assert any("already provided" in problem.error for problem in problems)


def test_discovery_keeps_the_first_of_two_plugins_with_one_package(tmp_path: Path) -> None:
    write_plugin(tmp_path, name="alpha", package="probe_plugin")
    write_plugin(tmp_path, name="beta", package="probe_plugin")

    found, problems = discover_plugins(tmp_path)

    assert [plugin.manifest.name for plugin in found] == ["alpha"]
    assert len(problems) == 1
    assert "package 'probe_plugin' is already provided by plugin 'alpha'" in problems[0].error


def test_discovery_skips_dotfiles_and_ignores_a_missing_directory(tmp_path: Path) -> None:
    (tmp_path / ".staging-abc").mkdir()

    assert discover_directory_plugins(tmp_path) == ([], [])
    assert discover_directory_plugins(tmp_path / "nowhere") == ([], [])


# --- loading ----------------------------------------------------------------


def test_load_registers_router_cli_ui_and_hands_over_config(tmp_path: Path) -> None:
    write_plugin(tmp_path)
    config = config_for(tmp_path, probe={"judge": "claude"})

    registry = load_plugins(config)

    plugin = registry.get("probe")
    assert plugin is not None
    assert plugin.status == "loaded"
    assert len(plugin.routers) == 1
    assert [group.name for group in plugin.cli_groups] == ["probe-cli"]
    assert plugin.ui is not None
    assert plugin.ui_url == "/plugins/probe/ui/"
    assert plugin.api_prefix == "/plugins/probe"
    assert sys.modules["probe_plugin"].SEEN_CONFIG == {"judge": "claude"}
    assert "loaded probe 1.2.3" in registry.summary


def test_a_plugin_that_raises_is_listed_as_failed_and_the_rest_load(tmp_path: Path) -> None:
    write_plugin(
        tmp_path, name="broken", package="broken_plugin", body="def register(ctx):\n    raise ValueError('boom')"
    )
    write_plugin(tmp_path)

    registry = load_plugins(config_for(tmp_path))

    broken = registry.get("broken")
    assert broken is not None
    assert broken.status == "failed"
    assert broken.error == "ValueError: boom"
    assert registry.get("probe") is not None
    assert registry.get("probe").status == "loaded"  # type: ignore[union-attr]
    assert [plugin.name for plugin in registry.loaded()] == ["probe"]


def test_an_async_register_is_refused_by_name(tmp_path: Path) -> None:
    write_plugin(tmp_path, body="async def register(ctx):\n    pass")

    registry = load_plugins(config_for(tmp_path))

    plugin = registry.get("probe")
    assert plugin is not None
    assert plugin.status == "failed"
    assert "async" in (plugin.error or "")


def test_a_disabled_plugin_is_listed_and_not_imported(tmp_path: Path) -> None:
    write_plugin(tmp_path, body="raise RuntimeError('must not import')")

    registry = load_plugins(config_for(tmp_path, disabled=["probe"]))

    plugin = registry.get("probe")
    assert plugin is not None
    assert plugin.status == "disabled"
    assert registry.loaded() == []


def test_a_package_already_imported_from_elsewhere_is_refused_by_name(tmp_path: Path) -> None:
    # The CLI attaches plugins from the default directory before ``serve`` loads
    # the configured one; a package imported once stands in for every later
    # import of that name, so the second load must say so rather than serve it.
    write_plugin(tmp_path / "first", body="MARK = 'first'\n" + PACKAGE)
    write_plugin(tmp_path / "second", body="MARK = 'second'\n" + PACKAGE)
    assert load_plugins(config_for(tmp_path / "first")).get("probe").status == "loaded"  # type: ignore[union-attr]

    plugin = load_plugins(config_for(tmp_path / "second")).get("probe")

    assert plugin is not None
    assert plugin.status == "failed"
    assert "already imported from" in (plugin.error or "")
    assert sys.modules["probe_plugin"].MARK == "first"


def test_the_same_package_loads_again_from_the_same_directory(tmp_path: Path) -> None:
    write_plugin(tmp_path)
    load_plugins(config_for(tmp_path))

    assert load_plugins(config_for(tmp_path)).get("probe").status == "loaded"  # type: ignore[union-attr]


def test_context_refuses_the_wrong_kinds_of_contribution(tmp_path: Path) -> None:
    write_plugin(tmp_path, body="def register(ctx):\n    ctx.add_router(object())")

    registry = load_plugins(config_for(tmp_path))

    plugin = registry.get("probe")
    assert plugin is not None
    assert plugin.status == "failed"
    assert "not an APIRouter" in (plugin.error or "")


def test_context_refuses_a_missing_migrations_directory(tmp_path: Path) -> None:
    write_plugin(tmp_path, body="def register(ctx):\n    ctx.add_migrations('/nowhere/at/all')")

    registry = load_plugins(config_for(tmp_path))

    plugin = registry.get("probe")
    assert plugin is not None
    assert plugin.status == "failed"
    assert "does not exist" in (plugin.error or "")


def test_plugin_context_is_what_register_sees() -> None:
    assert PluginContext.add_router.__doc__ is not None
    assert "/api/v1/plugins/<name>" in PluginContext.add_router.__doc__


# --- migrations -------------------------------------------------------------


# The probe package, also registering a migrations directory beside it.
MIGRATING = "from pathlib import Path\n" + PACKAGE.replace(
    "    ctx.add_cli(probe_cli)\n",
    '    ctx.add_cli(probe_cli)\n    ctx.add_migrations(Path(__file__).parent / "migrations")\n',
)
assert MIGRATING != PACKAGE


def test_a_failed_migration_marks_the_plugin_failed_and_its_routes_refuse(tmp_path: Path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gateway.api.main import register_routers
    from gateway.container import build_container
    from gateway.plugins.migrations import run_plugin_migrations

    package_dir = write_plugin(tmp_path, body=MIGRATING)
    migrations = package_dir / "migrations"
    (migrations / "versions").mkdir(parents=True)
    (migrations / "env.py").write_text("raise RuntimeError('this chain is broken')\n")
    (migrations / "script.py.mako").write_text("")
    config = config_for(tmp_path)
    app = FastAPI()
    app.state.container = build_container(None)
    app.state.plugins = load_plugins(config, app.state.container)
    register_routers(app, config)
    client = TestClient(app)  # no lifespan: the routes are mounted, the chain has not run
    assert client.get("/api/v1/plugins/probe/probe").status_code == 200

    failed = run_plugin_migrations(f"sqlite:///{tmp_path}/probe.db", app.state.plugins)

    assert [plugin.name for plugin in failed] == ["probe"]
    plugin = app.state.plugins.get("probe")
    assert plugin is not None
    assert plugin.status == "failed"
    assert "this chain is broken" in (plugin.error or "")
    response = client.get("/api/v1/plugins/probe/probe")
    assert response.status_code == 503
    assert "this chain is broken" not in response.text


# --- archives ---------------------------------------------------------------


def zip_of(tree: Path, prefix: str = "") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in sorted(tree.rglob("*")):
            if path.is_file():
                archive.write(path, prefix + str(path.relative_to(tree)))
    return buffer.getvalue()


def test_install_archive_unpacks_a_zip_under_the_plugin_name(tmp_path: Path) -> None:
    source = tmp_path / "src"
    write_plugin(source)
    data = zip_of(source / "probe", prefix="probe-main/")
    directory = tmp_path / "plugins"

    installed = install_archive(data, directory)

    assert installed.manifest.name == "probe"
    assert installed.install_dir == directory / "probe"
    assert (directory / "probe" / "probe-main" / "probe_plugin" / "otari-plugin.toml").is_file()
    assert [entry.name for entry in directory.iterdir()] == ["probe"]


def test_install_archive_replaces_an_existing_install(tmp_path: Path) -> None:
    source = tmp_path / "src"
    write_plugin(source)
    directory = tmp_path / "plugins"
    install_archive(zip_of(source / "probe"), directory)
    (source / "probe" / "probe_plugin" / "otari-plugin.toml").write_text(MANIFEST.replace("1.2.3", "2.0.0"))

    installed = install_archive(zip_of(source / "probe"), directory)

    assert installed.manifest.version == "2.0.0"
    assert not (directory / ".probe.previous").exists()


def test_install_archive_accepts_a_tarball(tmp_path: Path) -> None:
    source = tmp_path / "src"
    write_plugin(source)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(source / "probe", arcname="probe")

    installed = install_archive(buffer.getvalue(), tmp_path / "plugins")

    assert installed.manifest.name == "probe"


@pytest.mark.parametrize("member", ["../escape.txt", "/abs/escape.txt"])
def test_install_archive_refuses_traversal(tmp_path: Path, member: str) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, "x")

    with pytest.raises(PluginInstallError, match="escapes"):
        install_archive(buffer.getvalue(), tmp_path / "plugins")
    assert not any((tmp_path / "plugins").iterdir())


def test_install_archive_refuses_symlinks(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("probe_plugin/link")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "/etc/passwd")

    with pytest.raises(PluginInstallError, match="symlink"):
        install_archive(buffer.getvalue(), tmp_path / "plugins")


def test_install_archive_refuses_an_archive_with_no_manifest(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("just/a/file.txt", "x")

    with pytest.raises(PluginInstallError, match="no otari-plugin.toml"):
        install_archive(buffer.getvalue(), tmp_path / "plugins")
    assert not any((tmp_path / "plugins").iterdir())


def test_install_archive_reports_a_corrupt_member(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("probe_plugin/otari-plugin.toml", MANIFEST)
    data = bytearray(buffer.getvalue())
    # Flip a byte of the stored member's data so its CRC no longer matches.
    data[30 + len("probe_plugin/otari-plugin.toml")] ^= 0xFF

    with pytest.raises(PluginInstallError, match="could not be unpacked"):
        install_archive(bytes(data), tmp_path / "plugins")
    assert not any((tmp_path / "plugins").iterdir())


def test_install_archive_refuses_junk(tmp_path: Path) -> None:
    with pytest.raises(PluginInstallError, match="neither"):
        install_archive(b"hello", tmp_path / "plugins")
    with pytest.raises(PluginInstallError, match="empty"):
        install_archive(b"", tmp_path / "plugins")


def test_remove_installed_stays_inside_the_plugins_directory(tmp_path: Path) -> None:
    directory = tmp_path / "plugins"
    write_plugin(directory)

    with pytest.raises(PluginInstallError):
        remove_installed(tmp_path, directory)
    with pytest.raises(PluginInstallError):
        remove_installed(directory, directory)
    remove_installed(directory / "probe", directory)
    assert not (directory / "probe").exists()
    with pytest.raises(PluginInstallError, match="already gone"):
        remove_installed(directory / "probe", directory)


def test_github_archive_url_validates_its_inputs() -> None:
    assert github_archive_url("mozilla-ai/otari-agent-gates", None) == (
        "https://github.com/mozilla-ai/otari-agent-gates/archive/HEAD.zip"
    )
    assert github_archive_url("mozilla-ai/otari-agent-gates", "v1.0.0").endswith("/archive/v1.0.0.zip")
    for bad in ("nope", "a/b/c", "../x/y", "owner/.git", "http://github.com/a/b"):
        with pytest.raises(PluginInstallError):
            github_archive_url(bad, None)
    with pytest.raises(PluginInstallError):
        github_archive_url("a/b", "../../etc")


# --- marketplace ------------------------------------------------------------


def fake_github(index_status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "raw.githubusercontent.com":
            if index_status != 200:
                return httpx.Response(index_status)
            return httpx.Response(
                200,
                json={
                    "plugins": [
                        {"name": "agent-gates", "repo": "mozilla-ai/otari-agent-gates", "version": "0.1.0"},
                        {"broken": True},
                    ]
                },
            )
        assert request.url.host == "api.github.com"
        assert request.url.params["q"] == "topic:otari-plugin"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "full_name": "someone/otari-thing",
                        "name": "otari-thing",
                        "description": "A thing.",
                        "html_url": "https://github.com/someone/otari-thing",
                        "stargazers_count": 3,
                        "default_branch": "main",
                    },
                    {"full_name": "mozilla-ai/otari-agent-gates", "name": "otari-agent-gates"},
                ]
            },
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_marketplace_splits_verified_from_community_and_caches() -> None:
    marketplace = Marketplace(MarketplaceConfig(), transport=fake_github())

    listing = await marketplace.listing()

    assert [entry.name for entry in listing.verified] == ["agent-gates"]
    assert listing.verified[0].verified is True
    assert [entry.repo for entry in listing.community] == ["someone/otari-thing"]
    assert listing.community[0].stars == 3
    assert listing.community[0].ref == "main"
    assert listing.errors == []
    assert await marketplace.listing() is listing
    assert await marketplace.listing(refresh=True) is not listing


@pytest.mark.asyncio
async def test_marketplace_reports_an_unreachable_source_and_keeps_the_other() -> None:
    marketplace = Marketplace(MarketplaceConfig(), transport=fake_github(index_status=500))

    listing = await marketplace.listing()

    assert listing.verified == []
    assert len(listing.community) == 2  # nothing is verified, so nothing is dropped
    assert listing.errors == ["The verified plugin index could not be fetched."]


@pytest.mark.asyncio
async def test_marketplace_skips_a_source_that_is_turned_off() -> None:
    marketplace = Marketplace(MarketplaceConfig(verified_index_url="", github_topic=""), transport=fake_github())

    listing = await marketplace.listing()

    assert listing.verified == [] and listing.community == [] and listing.errors == []


@pytest.mark.asyncio
async def test_a_missing_verified_index_is_an_empty_list_not_an_error() -> None:
    marketplace = Marketplace(MarketplaceConfig(), transport=fake_github(index_status=404))

    listing = await marketplace.listing()

    assert listing.verified == []
    assert listing.errors == []


# --- install record, pending beside loaded, the lazy CLI, dropping tables --------


def test_install_records_where_a_plugin_came_from_and_refuses_a_downgrade(tmp_path: Path) -> None:
    from gateway.plugins.archive import read_install_record

    directory = tmp_path / "plugins"
    v2 = _archive_with_version("2.0.0")
    installed = install_archive(v2, directory, source="example/probe", ref="v2.0.0")
    assert installed.install_dir is not None
    record = read_install_record(installed.install_dir)
    assert record is not None
    assert (record["source"], record["ref"], record["version"]) == ("example/probe", "v2.0.0", "2.0.0")
    assert record["installed_at"]

    with pytest.raises(PluginInstallError, match="older than the installed 2.0.0"):
        install_archive(_archive_with_version("1.0.0"), directory, source="upload")
    assert read_install_record(installed.install_dir)["version"] == "2.0.0"  # type: ignore[index]

    forced = install_archive(_archive_with_version("1.0.0"), directory, source="upload", force=True)
    assert forced.manifest.version == "1.0.0"


def _archive_with_version(version: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "probe_plugin/otari-plugin.toml", MANIFEST.replace('version = "1.2.3"', f'version = "{version}"')
        )
        archive.writestr("probe_plugin/__init__.py", PACKAGE)
    return buffer.getvalue()


def test_a_running_plugin_keeps_its_contributions_when_a_new_version_is_installed(tmp_path: Path) -> None:
    write_plugin(tmp_path)
    registry = load_plugins(config_for(tmp_path))
    running = registry.get("probe")
    assert running is not None and running.status == "loaded" and running.routers

    newer = parse_manifest(MANIFEST.replace('version = "1.2.3"', 'version = "2.0.0"'))
    entry = registry.record_pending(newer, tmp_path / "probe" / "probe_plugin", tmp_path / "probe")

    assert entry is running
    assert running.status == "loaded" and running.routers
    assert "2.0.0" in (running.pending or "")
    assert registry.changes_on_disk() is True

    registry.forget("probe")
    assert running.status == "loaded" and "unloads" in (running.pending or "")


def test_restart_required_is_read_from_disk(tmp_path: Path) -> None:
    write_plugin(tmp_path)
    registry = load_plugins(config_for(tmp_path))
    assert registry.changes_on_disk() is False

    # Another worker installs a second plugin: this process notices from the directory.
    write_plugin(tmp_path, name="second", package="second_plugin")
    assert registry.changes_on_disk() is True


def test_the_cli_loads_plugins_only_for_their_own_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import click
    from click.testing import CliRunner

    from gateway import cli as gateway_cli

    marker = tmp_path / "registered"
    registering = PACKAGE.replace(
        "def register(ctx: PluginContext) -> None:\n",
        f"def register(ctx: PluginContext) -> None:\n    open({str(marker)!r}, 'w').write('x')\n",
    )
    assert registering != PACKAGE
    write_plugin(tmp_path, body=registering)
    config_path = tmp_path / "config.yml"
    config_path.write_text(f"master_key: k\nplugins:\n  directory: {tmp_path}\n")

    sys.modules.pop("probe_plugin", None)
    group = gateway_cli._OtariCli(name="otari")
    group.add_command(click.Command("noop", callback=lambda: None))
    monkeypatch.setattr(sys, "argv", ["otari", "noop", "-c", str(config_path)])
    assert CliRunner().invoke(group, ["noop"]).exit_code == 0
    assert not marker.exists(), "a built-in command must not import plugins"

    monkeypatch.setattr(sys, "argv", ["otari", "probe", "-c", str(config_path)])
    assert group.get_command(click.Context(group), "probe-cli") is not None
    assert marker.exists()


def test_remove_drop_tables_runs_the_chain_back_and_drops_the_version_table(tmp_path: Path) -> None:
    import sqlite3

    from gateway.plugins.migrations import drop_plugin_tables, run_plugin_migrations

    package_dir = write_plugin(tmp_path, body=MIGRATING)
    migrations = package_dir / "migrations"
    (migrations / "versions").mkdir(parents=True)
    (migrations / "env.py").write_text(
        "from alembic import context\nfrom gateway.plugins.migrations import run_plugin_env\n"
        "run_plugin_env(context, None, 'probe')\n"
    )
    (migrations / "script.py.mako").write_text("")
    (migrations / "versions" / "0001_rows.py").write_text(
        "import sqlalchemy as sa\nfrom alembic import op\n"
        "revision = '0001'\ndown_revision = None\n"
        "def upgrade():\n    op.create_table('probe_rows', sa.Column('id', sa.Integer, primary_key=True))\n"
        "def downgrade():\n    op.drop_table('probe_rows')\n"
    )
    registry = load_plugins(config_for(tmp_path))
    plugin = registry.get("probe")
    assert plugin is not None and plugin.migrations
    url = f"sqlite:///{tmp_path / 'drop.db'}"
    run_plugin_migrations(url, [plugin])
    with sqlite3.connect(tmp_path / "drop.db") as db:
        before = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
    assert plugin.manifest.version_table in before

    drop_plugin_tables(url, plugin)

    with sqlite3.connect(tmp_path / "drop.db") as db:
        after = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
    assert plugin.manifest.version_table not in after
    assert not (before - {plugin.manifest.version_table}) & after


def test_a_plugin_that_registers_more_than_it_declared_is_refused(tmp_path: Path) -> None:
    write_plugin(
        tmp_path,
        body=PACKAGE.replace(
            "def register(ctx: PluginContext) -> None:",
            "class Watcher:\n    def on_request(self, event):\n        return None\n\n\n"
            "def register(ctx: PluginContext) -> None:\n    ctx.add_traffic_observer(Watcher())",
        ),
    )

    registry = load_plugins(config_for(tmp_path))

    plugin = registry.get("probe")
    assert plugin is not None
    assert plugin.status == "failed"
    assert "registered traffic without declaring it" in (plugin.error or "")
    assert plugin.routers == [] and plugin.observers == []


def test_manifest_contributions_are_a_closed_vocabulary_that_a_newer_gateway_may_extend() -> None:
    manifest = parse_manifest(
        '[plugin]\nname = "ok"\nversion = "1"\npackage = "p"\ncontributes = ["traffic"]\ngetting_started = "https://x/y"'
    )
    assert manifest.contributes == ["traffic"]
    assert manifest.getting_started == "https://x/y"
    assert manifest.needs_newer_gateway("1.0.0") is None

    # A kind this gateway does not know still parses (the marketplace and the
    # installer can describe the plugin) and is the reason a load refuses it.
    newer = parse_manifest('[plugin]\nname = "ok"\nversion = "1"\npackage = "p"\ncontributes = ["kernel"]')
    assert newer.unsupported_contributions == ["kernel"]
    assert "kernel" in (newer.needs_newer_gateway("1.0.0") or "")
    # Something that is not a word is not a kind at all.
    with pytest.raises(PluginManifestError, match="not a contribution kind"):
        parse_manifest('[plugin]\nname = "ok"\nversion = "1"\npackage = "p"\ncontributes = ["Kernel Mode"]')


def test_manifest_keys_from_a_newer_gateway_are_ignored() -> None:
    manifest = parse_manifest(
        '[plugin]\nname = "ok"\nversion = "1"\npackage = "p"\nfuture_key = 1\ncontributes = ["ui"]\n'
        '[plugin.ui]\nlabel = "x"\nfuture = 2\n'
    )

    assert manifest.name == "ok"
    assert [page.label for page in manifest.pages] == ["x"]


def test_min_otari_version_and_unknown_kinds_refuse_the_load_before_the_import(tmp_path: Path) -> None:
    marker = "import sys\nsys.modules['probe_plugin_ran'] = True\n"
    write_plugin(tmp_path, body=marker + PACKAGE)
    manifest_path = tmp_path / "probe" / "probe_plugin" / "otari-plugin.toml"
    manifest_path.write_text(MANIFEST.replace('version = "1.2.3"', 'version = "1.2.3"\nmin_otari_version = "999.0.0"'))
    sys.modules.pop("probe_plugin_ran", None)

    registry = load_plugins(config_for(tmp_path))

    plugin = registry.get("probe")
    assert plugin is not None
    assert plugin.status == "failed"
    assert "999.0.0 or newer" in (plugin.error or "")
    assert "probe_plugin_ran" not in sys.modules, "a refused plugin must not be imported"

    declared = 'contributes = ["routes", "cli", "ui"]'
    manifest_path.write_text(MANIFEST.replace(declared, 'contributes = ["ui", "kernel"]'))
    plugin = load_plugins(config_for(tmp_path)).get("probe")
    assert plugin is not None
    assert plugin.status == "failed"
    assert "kernel" in (plugin.error or "") and "newer" not in (plugin.error or "")
    assert "does not know" in (plugin.error or "")


def fake_github_repo(manifest_text: str | None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            paths = ["README.md", "src/probe_plugin/otari-plugin.toml"] if manifest_text is not None else ["README.md"]
            return httpx.Response(200, json={"tree": [{"path": path, "type": "blob"} for path in paths]})
        assert request.url.host == "raw.githubusercontent.com"
        assert request.url.path == "/example/probe/HEAD/src/probe_plugin/otari-plugin.toml"
        return httpx.Response(200, text=manifest_text or "")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_describe_reads_a_repository_manifest_before_install() -> None:
    from gateway.plugins.describe import describe_github_plugin

    manifest = await describe_github_plugin("example/probe", None, transport=fake_github_repo(MANIFEST))

    assert manifest.name == "probe"
    assert manifest.contributes == ["routes", "cli", "ui"]


@pytest.mark.asyncio
async def test_describe_refuses_a_repository_with_no_manifest() -> None:
    from gateway.plugins.describe import describe_github_plugin

    with pytest.raises(PluginInstallError, match="holds no otari-plugin.toml"):
        await describe_github_plugin("example/empty", None, transport=fake_github_repo(None))


def fake_github_tree(tree: dict[str, object] | str, manifest_text: str = MANIFEST) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(200, text=tree) if isinstance(tree, str) else httpx.Response(200, json=tree)
        return httpx.Response(200, text=manifest_text)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("otari-plugin.toml", "holds no otari-plugin.toml where an install would look"),
        ("vendor/src/probe_plugin/otari-plugin.toml", "holds no otari-plugin.toml where an install would look"),
        ("src/other_name/otari-plugin.toml", "declares package 'probe_plugin' but sits in 'other_name'"),
    ],
)
async def test_describe_refuses_what_an_install_would_refuse(path: str, reason: str) -> None:
    from gateway.plugins.describe import describe_github_plugin

    tree: dict[str, object] = {"tree": [{"path": "README.md", "type": "blob"}, {"path": path, "type": "blob"}]}
    with pytest.raises(PluginInstallError, match=reason):
        await describe_github_plugin("example/probe", "v1", transport=fake_github_tree(tree))


@pytest.mark.asyncio
async def test_describe_counts_a_root_manifest_the_installer_would_trip_over() -> None:
    # A GitHub archive nests the repository under one directory, so the installer
    # sees a root manifest as ``<dir>/otari-plugin.toml`` and refuses the pair.
    from gateway.plugins.describe import describe_github_plugin

    tree: dict[str, object] = {
        "tree": [{"path": "otari-plugin.toml", "type": "blob"}, {"path": "src/probe_plugin/otari-plugin.toml"}]
    }
    with pytest.raises(PluginInstallError, match="more than one otari-plugin.toml"):
        await describe_github_plugin("example/two", "v1", transport=fake_github_tree(tree))


@pytest.mark.asyncio
async def test_describe_reports_a_listing_github_cut_short_or_did_not_answer_as_json() -> None:
    from gateway.plugins.describe import describe_github_plugin

    with pytest.raises(PluginInstallError, match="too large for GitHub to list"):
        await describe_github_plugin("example/big", None, transport=fake_github_tree({"tree": [], "truncated": True}))
    with pytest.raises(PluginInstallError, match="is not JSON"):
        await describe_github_plugin("example/odd", None, transport=fake_github_tree("<html>not json</html>"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location", ["https://evil.example/tree", "http://api.github.com/repos/example/moved/git/trees/HEAD"]
)
async def test_describe_refuses_a_redirect_off_github(location: str) -> None:
    from gateway.plugins.describe import describe_github_plugin

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(PluginInstallError, match="off its own hosts"):
        await describe_github_plugin("example/moved", None, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_describe_follows_a_relative_redirect_within_github() -> None:
    from gateway.plugins.describe import describe_github_plugin

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com" and "renamed" not in request.url.path:
            return httpx.Response(301, headers={"location": "/repos/example/renamed/git/trees/HEAD"})
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"tree": [{"path": "src/probe_plugin/otari-plugin.toml"}]})
        return httpx.Response(200, text=MANIFEST)

    manifest = await describe_github_plugin("example/relative", None, transport=httpx.MockTransport(handler))

    assert manifest.name == "probe"


def test_a_manifest_that_ships_a_page_must_declare_it() -> None:
    with pytest.raises(PluginManifestError, match="does not declare"):
        parse_manifest(
            '[plugin]\nname = "p"\nversion = "1"\npackage = "p"\ncontributes = ["routes"]\n[plugin.ui]\nlabel = "P"'
        )
