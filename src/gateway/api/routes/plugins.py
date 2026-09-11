"""Installed plugins, their settings, their pages, and the marketplace.

Operator-only, and gated a second time for anything that writes code to disk:
``plugins.allow_install`` is off by default because a plugin runs inside the
gateway with everything the gateway can reach, and turning that on is a
decision an operator makes in config, not one a dashboard session makes for
them. The one member-visible route is ``GET /plugins/pages``, on a router of
its own: the rail needs it for every signed-in session, and it answers only
with the pages the caller is allowed to see.
"""

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.api.deps import get_config, get_db, get_session_identity, require_deployment_operator, verify_master_key
from gateway.core.config import GatewayConfig
from gateway.log_config import logger
from gateway.models.plugins import (
    PLUGIN_API_VERSION,
    PageAudience,
    PageIcon,
    PageParent,
    PageSection,
    PluginManifest,
    PluginManifestError,
    RuntimeMode,
    SettingType,
)
from gateway.models.tenancy import User as TenancyUser
from gateway.plugins import LoadedPlugin, PluginRegistry
from gateway.plugins.archive import (
    MAX_ARCHIVE_BYTES,
    PluginInstallError,
    fetch_github_archive,
    install_archive,
    read_install_record,
    remove_installed,
)
from gateway.plugins.describe import describe_github_plugin
from gateway.plugins.events import emit as emit_plugin_event
from gateway.plugins.marketplace import Marketplace, MarketplaceEntry
from gateway.services.plugin_settings_service import (
    PluginSettingsError,
    effective_values,
    save_plugin_settings,
    validate_plugin_settings,
)
from gateway.services.tenancy.deployment_user_service import DeploymentUserService
from gateway.version import __version__

router = APIRouter(prefix="/plugins", tags=["plugins"], dependencies=[Depends(require_deployment_operator)])
pages_router = APIRouter(prefix="/plugins", tags=["plugins"])

INSTALL_OFF_DETAIL = (
    "Plugin installation is off for this deployment. Set plugins.allow_install: true in config.yml "
    "(or OTARI_PLUGINS_ALLOW_INSTALL=true) and restart the gateway to turn it on."
)


class PluginPageInfo(BaseModel):
    """One dashboard page a plugin ships, and where its row goes."""

    id: str
    label: str
    url: str = Field(description="Where the page is served; the dashboard frames it.")
    path: str = Field(description="The dashboard path that frames it.")
    icon: PageIcon
    section: PageSection
    parent: PageParent | None = None
    order: int
    audience: PageAudience


class PluginUiInfo(BaseModel):
    label: str
    url: str = Field(description="Where the plugin's first page is served; the dashboard frames it.")


class PluginSettingField(BaseModel):
    """One typed setting from the manifest, as the dashboard renders it."""

    key: str
    type: SettingType
    default: Any = None
    description: str = ""
    secret: bool = False
    editable: bool = True


class InstallRecord(BaseModel):
    """Where a directory plugin came from, as recorded at install time."""

    source: str = Field(description="'upload', or the GitHub repository as owner/name.")
    ref: str | None = None
    installed_at: str | None = None


class PluginManifestSummary(BaseModel):
    """What a plugin declares about itself, readable before it is installed or run."""

    name: str
    version: str
    description: str
    plugin_api: int = Field(description="The plugin API version it is written against.")
    supported_here: bool = Field(description="Whether this gateway provides that plugin API version.")
    modes: list[RuntimeMode] = Field(description="The runtime modes it loads in.")
    homepage: str | None = None
    getting_started: str | None = Field(default=None, description="A page that walks a new user through setup.")
    contributes: list[str] = Field(
        description="What the plugin adds, from the closed vocabulary; enforced when it loads."
    )
    config_keys: list[str] = Field(description="Keys the plugin reads from its own block of config.yml.")
    needs_newer_gateway: str | None = Field(
        default=None,
        description="Why this gateway would refuse to load the plugin, when it would: known before install.",
    )
    settings: list[PluginSettingField] = Field(default_factory=list)
    pages: list[str] = Field(default_factory=list, description="Labels of the dashboard pages it ships.")


class InstalledPlugin(BaseModel):
    name: str
    version: str
    description: str
    source: Literal["entry_point", "directory"]
    status: Literal["loaded", "failed", "disabled", "pending_restart"]
    error: str | None = None
    pending: str | None = Field(
        default=None, description="A change on disk that takes effect on the next start, when one is waiting."
    )
    installed: InstallRecord | None = Field(
        default=None, description="Provenance, for a plugin installed from an archive."
    )
    plugin_api: int
    modes: list[RuntimeMode]
    homepage: str | None = None
    getting_started: str | None = None
    contributes: list[str] = Field(description="What the manifest declares; what loaded is enforced to match.")
    config_keys: list[str] = Field(default_factory=list)
    settings: list[PluginSettingField] = Field(default_factory=list)
    ui: PluginUiInfo | None = None
    pages: list[PluginPageInfo] = Field(default_factory=list)
    api_prefix: str = Field(description="Where the plugin's routes mount, below the API root.")
    routes: int = Field(description="How many routes the plugin registered.")
    cli_commands: list[str] = Field(description="Top-level `otari` command groups the plugin added.")
    migrations: bool = Field(description="Whether the plugin owns database migrations.")
    guardrails: list[str] = Field(default_factory=list, description="Guardrail profiles it offers.")
    tools: list[str] = Field(default_factory=list, description="Tool backends it offers.")
    router_backends: list[str] = Field(default_factory=list, description="Router backends it offers.")
    events: list[str] = Field(default_factory=list, description="Events it subscribed to.")
    traffic: bool = Field(default=False, description="Whether it watches inference traffic.")
    health: str | None = Field(default=None, description="What its last health check reported.")


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
    plugin_api: int = Field(description="The plugin API version this gateway provides.")


class PluginPagesResponse(BaseModel):
    pages: list[PluginPageInfo]


class PluginSettingsResponse(BaseModel):
    plugin: str
    fields: list[PluginSettingField]
    values: dict[str, Any] = Field(description="Each declared setting's live value; a set secret reads as masked.")


class UpdatePluginSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] = Field(description="Settings to change; null clears one back to config or default.")


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
    manifest: PluginManifestSummary | None = Field(
        default=None,
        description=(
            "The plugin's own declaration, when the listing carried it; "
            "GET /plugins/marketplace/describe reads it from the repository otherwise."
        ),
    )


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
    force: bool = Field(default=False, description="Install even when the version is older than the installed one.")


class InstallPluginResponse(BaseModel):
    plugin: InstalledPlugin
    restart_required: bool = True


def _registry(request: Request) -> PluginRegistry:
    registry: PluginRegistry = request.app.state.plugins
    return registry


def _marketplace(request: Request) -> Marketplace:
    marketplace: Marketplace = request.app.state.marketplace
    return marketplace


def _setting_fields(manifest: PluginManifest) -> list[PluginSettingField]:
    return [
        PluginSettingField(
            key=key,
            type=spec.type,
            default=None if spec.secret else spec.default,
            description=spec.description,
            secret=spec.secret,
            editable=spec.editable,
        )
        for key, spec in manifest.settings.items()
    ]


def _page_info(plugin: LoadedPlugin, page: Any) -> PluginPageInfo:
    manifest = page.manifest
    return PluginPageInfo(
        id=page.id,
        label=page.label,
        url=plugin.page_url(page),
        path=f"/plugins/{plugin.name}" if page is plugin.pages[0] else f"/plugins/{plugin.name}/{page.id}",
        icon=manifest.icon,
        section=manifest.section,
        parent=manifest.parent,
        order=manifest.order,
        audience=manifest.audience,
    )


def _describe(plugin: LoadedPlugin) -> InstalledPlugin:
    manifest = plugin.manifest
    return InstalledPlugin(
        name=plugin.name,
        version=manifest.version,
        description=manifest.description,
        source=plugin.source,
        status=plugin.status,
        error=plugin.error,
        pending=plugin.pending,
        installed=_install_record(plugin),
        plugin_api=manifest.plugin_api,
        modes=list(manifest.modes),
        homepage=manifest.homepage,
        getting_started=manifest.getting_started,
        contributes=list(manifest.contributes),
        config_keys=list(manifest.config_keys),
        settings=_setting_fields(manifest),
        ui=PluginUiInfo(label=plugin.ui.label, url=plugin.ui_url or "") if plugin.ui else None,
        pages=[_page_info(plugin, page) for page in plugin.pages],
        api_prefix=plugin.api_prefix,
        routes=sum(len(item.router.routes) for item in plugin.routers),
        cli_commands=[group.name for group in plugin.cli_groups if group.name],
        migrations=bool(plugin.migrations),
        guardrails=[f"{plugin.name}:{name}" for name in plugin.guardrails],
        tools=[f"{plugin.name}:{name}" for name in plugin.tools],
        router_backends=[f"{plugin.name}:{name}" for name in plugin.router_backends],
        events=sorted({name for name, _ in plugin.subscriptions}),
        traffic=bool(plugin.observers),
        health=plugin.health.get("status"),
    )


def _install_record(plugin: LoadedPlugin) -> InstallRecord | None:
    if plugin.install_dir is None:
        return None
    record = read_install_record(plugin.install_dir)
    if record is None:
        return None
    return InstallRecord(
        source=str(record.get("source", "upload")),
        ref=record.get("ref"),
        installed_at=record.get("installed_at"),
    )


def _restart_required(registry: PluginRegistry) -> bool:
    return any(plugin.status == "pending_restart" for plugin in registry) or registry.changes_on_disk()


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
        plugin_api=PLUGIN_API_VERSION,
    )


@pages_router.get("/pages", response_model=PluginPagesResponse)
async def list_plugin_pages(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    session_identity: Annotated[TenancyUser | None, Depends(get_session_identity)],
    _master_key: Annotated[str | None, Depends(verify_master_key)],
) -> PluginPagesResponse:
    """The dashboard pages loaded plugins ship, for the rail: every member page, and the operator pages for an operator.

    Authenticated the way the operator routes are, but not gated on the answer:
    a member sees the pages a plugin declared for members and nothing else.
    """
    registry = getattr(request.app.state, "plugins", None)
    if registry is None:
        return PluginPagesResponse(pages=[])
    operator = session_identity is None or await DeploymentUserService(db).has_administration_access(session_identity)
    pages = [
        _page_info(plugin, page) for plugin, page in registry.pages() if operator or page.manifest.audience == "member"
    ]
    # Stable, so pages with one order keep their declared sequence.
    pages.sort(key=lambda page: page.order)
    return PluginPagesResponse(pages=pages)


def _loaded_plugin_or_404(registry: PluginRegistry, name: str) -> LoadedPlugin:
    plugin = registry.get(name)
    if plugin is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No plugin named {name!r}.")
    return plugin


@router.get("/{name}/settings", response_model=PluginSettingsResponse)
async def get_plugin_settings(request: Request, name: str) -> PluginSettingsResponse:
    """A plugin's typed settings and their live values."""
    plugin = _loaded_plugin_or_404(_registry(request), name)
    return PluginSettingsResponse(
        plugin=plugin.name,
        fields=_setting_fields(plugin.manifest),
        values=effective_values(plugin.config, plugin.manifest),
    )


@router.put("/{name}/settings", response_model=PluginSettingsResponse)
async def update_plugin_settings(
    request: Request,
    name: str,
    body: UpdatePluginSettingsRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PluginSettingsResponse:
    """Change a plugin's settings from the dashboard.

    Validated against the manifest, persisted, then applied to the running
    plugin, which is told through its ``on_settings_change`` listeners. A
    ``null`` clears a key back to what config.yml or the manifest default says.
    """
    registry = _registry(request)
    plugin = _loaded_plugin_or_404(registry, name)
    try:
        values = validate_plugin_settings(plugin.manifest, body.values)
    except PluginSettingsError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
    await save_plugin_settings(db, plugin.name, values)
    # Applied after the write, like the gateway's own runtime overrides: a
    # cleared key falls back to config.yml's value, else the manifest default.
    raw_config = request.app.state.config.plugins.plugin_settings(plugin.name)
    applied: dict[str, Any] = {}
    for key, value in values.items():
        if value is not None:
            applied[key] = value
        else:
            applied[key] = raw_config.get(key, plugin.manifest.settings[key].default)
    if plugin.status == "loaded":
        await registry.apply_settings(plugin.name, applied)
    else:
        plugin.config.update(applied)
    emit_plugin_event("plugin.settings_changed", plugin=plugin.name, keys=sorted(values))
    logger.info("Plugin %s: settings %s changed from the dashboard", plugin.name, ", ".join(sorted(values)))
    return PluginSettingsResponse(
        plugin=plugin.name,
        fields=_setting_fields(plugin.manifest),
        values=effective_values(plugin.config, plugin.manifest),
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
        manifest=_summary(entry.manifest) if entry.manifest is not None else None,
    )


def _summary(manifest: PluginManifest) -> PluginManifestSummary:
    return PluginManifestSummary(
        name=manifest.name,
        version=manifest.version,
        description=manifest.description,
        plugin_api=manifest.plugin_api,
        supported_here=manifest.plugin_api <= PLUGIN_API_VERSION,
        modes=list(manifest.modes),
        homepage=manifest.homepage,
        getting_started=manifest.getting_started,
        contributes=list(manifest.contributes),
        config_keys=list(manifest.config_keys),
        needs_newer_gateway=manifest.needs_newer_gateway(__version__),
        settings=_setting_fields(manifest),
        pages=[page.label for page in manifest.pages],
    )


@router.get("/marketplace/describe", response_model=PluginManifestSummary)
async def describe_plugin(
    config: Annotated[GatewayConfig, Depends(get_config)],
    repo: Annotated[str, Query(description="GitHub repository, as owner/name.", max_length=200)],
    ref: Annotated[
        str | None, Query(description="Branch, tag, or commit; the default branch when unset.", max_length=200)
    ] = None,
) -> PluginManifestSummary:
    """Read a repository's plugin manifest, so an install can be understood before it happens."""
    try:
        manifest = await describe_github_plugin(repo, ref, token=config.plugins.marketplace.github_token)
    except (PluginInstallError, PluginManifestError) as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
    return _summary(manifest)


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


async def _install_bytes(
    request: Request, data: bytes, *, source: str, ref: str | None = None, force: bool = False
) -> InstallPluginResponse:
    registry = _registry(request)
    try:
        discovered = await asyncio.to_thread(
            install_archive, data, registry.directory, source=source, ref=ref, force=force
        )
    except PluginInstallError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
    current = registry.get(discovered.manifest.name)
    if current is not None and current.source == "entry_point":
        # Discovery lets the entry point win, so the directory copy would never
        # load; refuse rather than answer 201 for a plugin that stays pending.
        await asyncio.to_thread(remove_installed, discovered.install_dir or registry.directory, registry.directory)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Plugin {discovered.manifest.name!r} is installed as a Python distribution; "
                "uninstall it with pip or uv first."
            ),
        )
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
    force: Annotated[bool, Query(description="Install even when the version is older than the installed one.")] = False,
) -> InstallPluginResponse:
    """Install a plugin from an uploaded zip or tar.gz. It loads on the next start.

    ``force`` installs an archive whose version is older than the installed one.
    """
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
    return await _install_bytes(request, b"".join(chunks), source="upload", force=force)


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
    return await _install_bytes(request, data, source=body.repo, ref=body.ref, force=body.force)


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
