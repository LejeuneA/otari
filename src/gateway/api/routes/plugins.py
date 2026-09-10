"""Installed plugins and the marketplace.

Operator-only, and gated a second time for anything that writes code to disk:
``plugins.allow_install`` is off by default because a plugin runs inside the
gateway with everything the gateway can reach, and turning that on is a
decision an operator makes in config, not one a dashboard session makes for
them.
"""

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from gateway.api.deps import get_config, require_deployment_operator
from gateway.core.config import GatewayConfig
from gateway.log_config import logger
from gateway.plugins import LoadedPlugin, PluginRegistry
from gateway.plugins.archive import (
    MAX_ARCHIVE_BYTES,
    PluginInstallError,
    fetch_github_archive,
    install_archive,
    remove_installed,
)
from gateway.plugins.marketplace import Marketplace, MarketplaceEntry

router = APIRouter(prefix="/plugins", tags=["plugins"], dependencies=[Depends(require_deployment_operator)])

INSTALL_OFF_DETAIL = (
    "Plugin installation is off for this deployment. Set plugins.allow_install: true in config.yml "
    "(or OTARI_PLUGINS_ALLOW_INSTALL=true) and restart the gateway to turn it on."
)


class PluginUiInfo(BaseModel):
    label: str
    url: str = Field(description="Where the plugin's page is served; the dashboard frames it.")


class InstalledPlugin(BaseModel):
    name: str
    version: str
    description: str
    source: Literal["entry_point", "directory"]
    status: Literal["loaded", "failed", "disabled", "pending_restart"]
    error: str | None = None
    homepage: str | None = None
    ui: PluginUiInfo | None = None
    api_prefix: str = Field(description="Where the plugin's routes mount, below the API root.")
    routes: int = Field(description="How many routes the plugin registered.")
    cli_commands: list[str] = Field(description="Top-level `otari` command groups the plugin added.")
    migrations: bool = Field(description="Whether the plugin owns database migrations.")


class PluginProblem(BaseModel):
    source: Literal["entry_point", "directory"]
    location: str
    error: str


class PluginsResponse(BaseModel):
    plugins: list[InstalledPlugin]
    problems: list[PluginProblem] = Field(description="Candidates that could not be described, with why.")
    directory: str = Field(description="The plugins directory: where uploads and installs land.")
    install_allowed: bool
    restart_required: bool = Field(description="Whether a plugin was installed or removed since startup.")


class MarketplacePlugin(BaseModel):
    name: str
    repo: str = Field(description="GitHub repository, as owner/name.")
    description: str
    url: str
    verified: bool = Field(description="Listed in mozilla.ai's verified index.")
    version: str | None = None
    stars: int | None = None
    ref: str | None = Field(default=None, description="The git ref an install fetches; the default branch when unset.")
    updated_at: str | None = None
    installed: bool


class MarketplaceResponse(BaseModel):
    verified: list[MarketplacePlugin]
    community: list[MarketplacePlugin]
    errors: list[str] = Field(description="Sources that could not be reached this time.")
    topic: str = Field(description="The GitHub topic the community list is drawn from.")
    install_allowed: bool


class InstallPluginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str = Field(description="GitHub repository, as owner/name.", max_length=200)
    ref: str | None = Field(
        default=None, description="Branch, tag, or commit; the default branch when unset.", max_length=200
    )


class InstallPluginResponse(BaseModel):
    plugin: InstalledPlugin
    restart_required: bool = True


def _registry(request: Request) -> PluginRegistry:
    registry: PluginRegistry = request.app.state.plugins
    return registry


def _marketplace(request: Request) -> Marketplace:
    marketplace: Marketplace = request.app.state.marketplace
    return marketplace


def _describe(plugin: LoadedPlugin) -> InstalledPlugin:
    return InstalledPlugin(
        name=plugin.name,
        version=plugin.manifest.version,
        description=plugin.manifest.description,
        source=plugin.source,
        status=plugin.status,
        error=plugin.error,
        homepage=plugin.manifest.homepage,
        ui=PluginUiInfo(label=plugin.ui.label, url=plugin.ui_url or "") if plugin.ui else None,
        api_prefix=plugin.api_prefix,
        routes=sum(len(item.routes) for item in plugin.routers),
        cli_commands=[group.name for group in plugin.cli_groups if group.name],
        migrations=bool(plugin.migrations),
    )


def _restart_required(registry: PluginRegistry) -> bool:
    return any(plugin.status == "pending_restart" for plugin in registry)


def _require_install_allowed(config: GatewayConfig) -> None:
    if not config.plugins.allow_install:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=INSTALL_OFF_DETAIL)


@router.get("", response_model=PluginsResponse)
async def list_plugins(request: Request, config: Annotated[GatewayConfig, Depends(get_config)]) -> PluginsResponse:
    """List every plugin this gateway discovered, loaded or not."""
    registry = _registry(request)
    return PluginsResponse(
        plugins=[_describe(plugin) for plugin in registry],
        problems=[PluginProblem(source=p.source, location=p.location, error=p.error) for p in registry.problems],
        directory=str(registry.directory),
        install_allowed=config.plugins.allow_install,
        restart_required=_restart_required(registry),
    )


def _marketplace_plugin(entry: MarketplaceEntry, registry: PluginRegistry) -> MarketplacePlugin:
    installed = any(
        plugin.name == entry.name or (plugin.manifest.homepage or "").rstrip("/").endswith(entry.repo)
        for plugin in registry
    )
    return MarketplacePlugin(
        name=entry.name,
        repo=entry.repo,
        description=entry.description,
        url=entry.url,
        verified=entry.verified,
        version=entry.version,
        stars=entry.stars,
        ref=entry.ref,
        updated_at=entry.updated_at,
        installed=installed,
    )


@router.get("/marketplace", response_model=MarketplaceResponse)
async def marketplace(
    request: Request,
    config: Annotated[GatewayConfig, Depends(get_config)],
    refresh: Annotated[bool, Query(description="Bypass the cached listing.")] = False,
) -> MarketplaceResponse:
    """The plugins available to install: mozilla.ai's verified list, then the GitHub topic."""
    registry = _registry(request)
    listing = await _marketplace(request).listing(refresh=refresh)
    return MarketplaceResponse(
        verified=[_marketplace_plugin(entry, registry) for entry in listing.verified],
        community=[_marketplace_plugin(entry, registry) for entry in listing.community],
        errors=listing.errors,
        topic=config.plugins.marketplace.github_topic,
        install_allowed=config.plugins.allow_install,
    )


async def _install_bytes(request: Request, data: bytes) -> InstallPluginResponse:
    registry = _registry(request)
    try:
        discovered = await asyncio.to_thread(install_archive, data, registry.directory)
    except PluginInstallError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
    assert discovered.install_dir is not None  # noqa: S101 a directory install always has one
    pending = registry.record_pending(discovered.manifest, discovered.package_dir, discovered.install_dir)
    logger.info(
        "Installed plugin %s %s into %s; restart to load it",
        pending.name,
        pending.manifest.version,
        pending.install_dir,
    )
    return InstallPluginResponse(plugin=_describe(pending))


@router.post("/upload", response_model=InstallPluginResponse, status_code=status.HTTP_201_CREATED)
async def upload_plugin(
    request: Request,
    config: Annotated[GatewayConfig, Depends(get_config)],
    file: UploadFile,
) -> InstallPluginResponse:
    """Install a plugin from an uploaded zip or tar.gz. It loads on the next start."""
    _require_install_allowed(config)
    chunks: list[bytes] = []
    received = 0
    while chunk := await file.read(1024 * 1024):
        received += len(chunk)
        if received > MAX_ARCHIVE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"Plugin archives are limited to {MAX_ARCHIVE_BYTES // (1024 * 1024)} MiB.",
            )
        chunks.append(chunk)
    return await _install_bytes(request, b"".join(chunks))


@router.post("/install", response_model=InstallPluginResponse, status_code=status.HTTP_201_CREATED)
async def install_plugin(
    request: Request,
    config: Annotated[GatewayConfig, Depends(get_config)],
    body: InstallPluginRequest,
) -> InstallPluginResponse:
    """Install a plugin from a GitHub repository archive. It loads on the next start."""
    _require_install_allowed(config)
    try:
        data = await fetch_github_archive(body.repo, body.ref)
    except PluginInstallError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
    return await _install_bytes(request, data)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_plugin(request: Request, config: Annotated[GatewayConfig, Depends(get_config)], name: str) -> None:
    """Delete a plugin installed in the plugins directory. It unloads on the next start."""
    _require_install_allowed(config)
    registry = _registry(request)
    plugin = registry.get(name)
    if plugin is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No plugin named {name!r}.")
    if plugin.install_dir is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Plugin {name!r} is installed as a Python distribution; uninstall it with pip or uv.",
        )
    try:
        await asyncio.to_thread(remove_installed, plugin.install_dir, registry.directory)
    except PluginInstallError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    registry.forget(name)
    logger.info("Removed plugin %s from %s; restart to unload it", name, plugin.install_dir)
