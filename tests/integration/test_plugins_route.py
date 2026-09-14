"""The plugin seam, end to end on a running app.

A directory plugin is written to a temporary plugins directory, and the booted
app is asked for everything loading it should have produced: its routes under
``/api/v1/plugins/<name>``, its page under ``/plugins/<name>/ui/``, the
``plugins`` surface in the bootstrap, and the operator listing. The install
routes are exercised against the same directory: refused until
``allow_install`` is on, then landing an archive that the listing reports as
pending a restart.
"""

import io
import sys
import zipfile
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.core.config import API_ROOT, GatewayConfig
from gateway.models.plugins import PluginsConfig

from .conftest import build_test_client

MANIFEST = """
[plugin]
name = "probe"
version = "0.1.0"
description = "A probe plugin."
package = "probe_route_plugin"

[plugin.ui]
path = "static"
label = "Probe"
"""

PACKAGE = """
from fastapi import APIRouter, Depends
from gateway.api.deps import verify_master_key
from gateway.plugins import PluginContext

router = APIRouter(prefix="/probe")


@router.get("/open")
async def open_probe() -> dict[str, str]:
    return {"probe": "open"}


@router.get("/guarded", dependencies=[Depends(verify_master_key)])
async def guarded_probe() -> dict[str, str]:
    return {"probe": "guarded"}


admin = APIRouter(prefix="/admin")


@admin.get("/status")
async def admin_status() -> dict[str, str]:
    return {"probe": "admin"}


def register(ctx: PluginContext) -> None:
    ctx.add_router(router, auth="none")
    ctx.add_router(admin)
"""

HEADERS = {"Authorization": "Bearer test-master-key"}


def write_probe(directory: Path, package: str = "probe_route_plugin", name: str = "probe") -> Path:
    package_dir = directory / name / package
    package_dir.mkdir(parents=True)
    manifest = MANIFEST.replace('package = "probe_route_plugin"', f'package = "{package}"').replace(
        'name = "probe"', f'name = "{name}"'
    )
    (package_dir / "otari-plugin.toml").write_text(manifest)
    (package_dir / "__init__.py").write_text(PACKAGE)
    (package_dir / "static").mkdir()
    (package_dir / "static" / "index.html").write_text("<html>probe page</html>")
    return package_dir


@pytest.fixture
def plugins_dir(tmp_path: Path) -> Generator[Path]:
    directory = tmp_path / "otari-plugins"
    write_probe(directory)
    sys.modules.pop("probe_route_plugin", None)
    yield directory
    sys.modules.pop("probe_route_plugin", None)


def config_with(postgres_url: str, plugins_dir: Path, *, allow_install: bool) -> GatewayConfig:
    return GatewayConfig(
        database_url=postgres_url,
        master_key="test-master-key",
        auto_migrate=False,
        require_pricing=False,
        model_discovery=False,
        plugins=PluginsConfig(directory=str(plugins_dir), allow_install=allow_install),
    )


@pytest.fixture
def plugin_client(postgres_url: str, plugins_dir: Path) -> Generator[TestClient]:
    yield from build_test_client(config_with(postgres_url, plugins_dir, allow_install=False))


@pytest.fixture
def installing_client(postgres_url: str, plugins_dir: Path) -> Generator[TestClient]:
    yield from build_test_client(config_with(postgres_url, plugins_dir, allow_install=True))


def test_a_plugin_route_is_served_under_the_plugin_prefix(plugin_client: TestClient) -> None:
    open_response = plugin_client.get(f"{API_ROOT}/plugins/probe/probe/open")
    guarded_without = plugin_client.get(f"{API_ROOT}/plugins/probe/probe/guarded")
    guarded_with = plugin_client.get(f"{API_ROOT}/plugins/probe/probe/guarded", headers=HEADERS)

    assert open_response.status_code == 200
    assert open_response.json() == {"probe": "open"}
    # A router mounted with auth="none" adds nothing; the plugin's own dependency does.
    assert guarded_without.status_code == 401
    assert guarded_with.status_code == 200
    # A router mounted with the default is an operator's: refused without a credential.
    admin_without = plugin_client.get(f"{API_ROOT}/plugins/probe/admin/status")
    admin_with = plugin_client.get(f"{API_ROOT}/plugins/probe/admin/status", headers=HEADERS)
    assert admin_without.status_code == 401
    assert admin_with.status_code == 200
    assert admin_with.json() == {"probe": "admin"}


def test_a_plugin_page_is_served_as_static_files_and_may_be_framed(plugin_client: TestClient) -> None:
    response = plugin_client.get("/plugins/probe/ui/")

    assert response.status_code == 200
    assert "probe page" in response.text
    # The dashboard shows the page in a same-origin frame; nothing else may be framed.
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert plugin_client.get(f"{API_ROOT}/plugins/probe/probe/open").headers["X-Frame-Options"] == "DENY"
    assert plugin_client.get("/").headers["X-Frame-Options"] == "DENY"


def test_the_bootstrap_reports_the_plugins_surface(plugin_client: TestClient) -> None:
    assert "plugins" in plugin_client.get(f"{API_ROOT}/bootstrap").json()["surfaces"]


def test_the_listing_is_operator_only_and_describes_the_plugin(plugin_client: TestClient) -> None:
    assert plugin_client.get(f"{API_ROOT}/plugins").status_code == 401

    response = plugin_client.get(f"{API_ROOT}/plugins", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["install_allowed"] is False
    assert body["restart_required"] is False
    (plugin,) = body["plugins"]
    assert plugin["name"] == "probe"
    assert plugin["status"] == "loaded"
    assert plugin["source"] == "directory"
    assert plugin["routes"] == 3
    assert plugin["ui"] == {"label": "Probe", "url": "/plugins/probe/ui/"}
    assert plugin["api_prefix"] == "/plugins/probe"


def probe_archive(name: str = "second", package: str = "second_route_plugin") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        manifest = MANIFEST.replace('name = "probe"', f'name = "{name}"').replace(
            'package = "probe_route_plugin"', f'package = "{package}"'
        )
        archive.writestr(f"repo-main/{package}/otari-plugin.toml", manifest)
        archive.writestr(f"repo-main/{package}/__init__.py", PACKAGE)
    return buffer.getvalue()


def test_installing_is_refused_until_the_operator_turns_it_on(plugin_client: TestClient) -> None:
    upload = plugin_client.post(
        f"{API_ROOT}/plugins/upload", headers=HEADERS, files={"file": ("second.zip", probe_archive())}
    )
    install = plugin_client.post(f"{API_ROOT}/plugins/install", headers=HEADERS, json={"repo": "a/b"})
    remove = plugin_client.delete(f"{API_ROOT}/plugins/probe", headers=HEADERS)

    assert upload.status_code == 403
    assert "allow_install" in upload.json()["detail"]
    assert install.status_code == 403
    assert remove.status_code == 403


def test_an_uploaded_archive_lands_in_the_directory_as_pending(
    installing_client: TestClient, plugins_dir: Path
) -> None:
    response = installing_client.post(
        f"{API_ROOT}/plugins/upload", headers=HEADERS, files={"file": ("second.zip", probe_archive())}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["restart_required"] is True
    assert body["plugin"]["name"] == "second"
    assert body["plugin"]["status"] == "pending_restart"
    assert (plugins_dir / "second" / "repo-main" / "second_route_plugin" / "otari-plugin.toml").is_file()

    listing = installing_client.get(f"{API_ROOT}/plugins", headers=HEADERS).json()
    assert listing["restart_required"] is True
    assert {plugin["name"]: plugin["status"] for plugin in listing["plugins"]} == {
        "probe": "loaded",
        "second": "pending_restart",
    }
    # Nothing is imported until restart: the new plugin's routes are not mounted.
    assert installing_client.get(f"{API_ROOT}/plugins/second/probe/open").status_code == 404


def test_a_plugin_page_asset_is_cached_like_the_dashboard_s_own(plugin_client: TestClient, plugins_dir: Path) -> None:
    # Vite hashes the file name, so the URL is immutable for as long as it exists.
    (static_dir,) = [path for path in plugins_dir.glob("probe/**/static") if path.is_dir()]
    assets = static_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "index-abc123.js").write_text("console.log('probe')")

    response = plugin_client.get("/plugins/probe/ui/assets/index-abc123.js")

    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    page = plugin_client.get("/plugins/probe/ui/")
    assert "immutable" not in page.headers.get("cache-control", "")


def test_a_bad_archive_is_refused_with_the_reason(installing_client: TestClient, plugins_dir: Path) -> None:
    response = installing_client.post(
        f"{API_ROOT}/plugins/upload", headers=HEADERS, files={"file": ("junk.zip", b"not an archive")}
    )

    assert response.status_code == 422
    assert "neither a zip" in response.json()["detail"]
    assert sorted(entry.name for entry in plugins_dir.iterdir()) == ["probe"]


def test_removing_a_directory_plugin_deletes_it_and_reports_the_restart(
    installing_client: TestClient, plugins_dir: Path
) -> None:
    response = installing_client.delete(f"{API_ROOT}/plugins/probe", headers=HEADERS)

    assert response.status_code == 204
    assert not (plugins_dir / "probe").exists()
    listing = installing_client.get(f"{API_ROOT}/plugins", headers=HEADERS).json()
    assert listing["restart_required"] is True
    (plugin,) = listing["plugins"]
    # Still loaded: a running plugin keeps every contribution until restart, and
    # the entry says what is waiting.
    assert plugin["status"] == "loaded"
    assert "next start" in plugin["pending"]
    assert installing_client.delete(f"{API_ROOT}/plugins/nowhere", headers=HEADERS).status_code == 404
    # Deleting it again finds the directory gone: a conflict naming the restart, not a 500.
    again = installing_client.delete(f"{API_ROOT}/plugins/probe", headers=HEADERS)
    assert again.status_code == 409
    assert "restart" in again.json()["detail"]
