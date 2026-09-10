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
    ctx.add_router(router)
    ctx.add_cli(probe_cli)
'''


def write_plugin(root: Path, name: str = "probe", package: str = "probe_plugin", body: str = PACKAGE) -> Path:
    """Lay out one directory plugin under ``root/<name>`` and return its package dir."""
    package_dir = root / name / package
    package_dir.mkdir(parents=True)
    manifest = MANIFEST.replace('name = "probe"', f'name = "{name}"').replace(
        'package = "probe_plugin"', f'package = "{package}"'
    )
    (package_dir / "otari-plugin.toml").write_text(manifest)
    (package_dir / "__init__.py").write_text(body)
    (package_dir / "static").mkdir()
    (package_dir / "static" / "index.html").write_text("<html>probe</html>")
    return package_dir


@pytest.fixture(autouse=True)
def _forget_probe_packages() -> Generator[None]:
    """Keep one test's imported plugin package from serving the next."""
    yield
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
    assert manifest.ui is not None
    assert manifest.ui.label == "Probe"
    assert manifest.version_table == "alembic_version_probe"


@pytest.mark.parametrize(
    "text",
    [
        "not toml [",
        "[other]\nname = 'x'",
        '[plugin]\nname = "Bad Name"\nversion = "1"\npackage = "p"',
        '[plugin]\nname = "ok"\nversion = "1"\npackage = "not a module"',
        '[plugin]\nname = "ok"\nversion = "1"\npackage = "p"\nunknown = 1',
        '[plugin]\nname = "ok"\nversion = "1"\npackage = "p"\nentrypoint = "1bad"',
    ],
)
def test_manifest_rejects_malformed_input(text: str) -> None:
    with pytest.raises(PluginManifestError):
        parse_manifest(text)


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
