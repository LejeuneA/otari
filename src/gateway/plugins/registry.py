"""Load discovered plugins and hold what each one contributed.

A plugin's ``register`` receives a :class:`PluginContext` and adds routers, CLI
groups, and migration directories to it. Everything a plugin adds is recorded on
its :class:`LoadedPlugin`; ``gateway.api.main`` mounts the routers,
``gateway.main`` mounts the UI and runs the migrations, and ``gateway.cli``
attaches the groups. A plugin that fails to import or register is recorded as
failed with its error and skipped, so one broken plugin does not take the
gateway down with it; the dashboard shows the failure.
"""

import importlib
import inspect
import sys
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import click
from fastapi import APIRouter

from gateway.log_config import logger
from gateway.models.plugins import PluginManifest, PluginsConfig
from gateway.plugins.discovery import DiscoveredPlugin, DiscoveryProblem, PluginSource, discover_plugins
from gateway.version import __version__

if TYPE_CHECKING:
    from gateway.container import Container
    from gateway.core.config import GatewayConfig

PluginStatus = Literal["loaded", "failed", "disabled", "pending_restart"]


class PluginError(Exception):
    """Raised by :class:`PluginContext` when a contribution is not usable."""


@dataclass(frozen=True)
class UiContribution:
    """The static dashboard page a plugin ships."""

    directory: Path
    label: str


# What a plugin's router asks of a caller, applied at the mount. The default is
# the gateway's own rule for a management router: a route is open only when its
# plugin said so.
RouterAuth = Literal["operator", "session", "api_key", "none"]


@dataclass(frozen=True)
class PluginRouter:
    router: APIRouter
    auth: RouterAuth


@dataclass
class LoadedPlugin:
    """One discovered plugin and what loading it produced."""

    manifest: PluginManifest
    source: PluginSource
    package_dir: Path
    install_dir: Path | None
    status: PluginStatus
    error: str | None = None
    # A change on disk that takes effect on the next start: a newer version
    # installed over this one, or its directory removed.
    pending: str | None = None
    routers: list[PluginRouter] = field(default_factory=list)
    cli_groups: list[click.Group] = field(default_factory=list)
    migrations: list[Path] = field(default_factory=list)
    observers: list[Any] = field(default_factory=list)
    ui: UiContribution | None = None

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def api_prefix(self) -> str:
        """Where the plugin's routers mount, below the API root."""
        return f"/plugins/{self.name}"

    @property
    def ui_url(self) -> str | None:
        """Where the plugin's page is served, or ``None`` when it ships none."""
        return f"/plugins/{self.name}/ui/" if self.ui is not None else None


class PluginContext:
    """What a plugin's ``register(ctx)`` is handed.

    ``config`` is the plugin's own block of ``config.yml`` (``plugins.<name>``),
    passed raw: the plugin validates it. ``container`` is the composition root,
    so a plugin can resolve a port; it is ``None`` when plugins are loaded for
    the CLI alone, where no app exists.
    """

    def __init__(self, plugin: LoadedPlugin, config: dict[str, Any], container: "Container | None") -> None:
        self._plugin = plugin
        self.name = plugin.name
        self.config = config
        self.container = container

    def add_router(self, router: APIRouter, *, auth: RouterAuth = "operator") -> None:
        """Mount ``router`` under ``/api/v1/plugins/<name>``, behind ``auth``.

        The router's own prefix, if any, applies below that. ``auth`` is applied
        to every route on the router: ``"operator"`` (the default) is a
        deployment operator, as the gateway's management routers require;
        ``"session"`` is any signed-in dashboard session or the master key;
        ``"api_key"`` is an API key or the master key, for a route a client
        program calls; ``"none"`` mounts the router open, for a route that
        authenticates in some way of its own.
        """
        if not isinstance(router, APIRouter):
            msg = f"plugin {self.name!r} added a router that is not an APIRouter: {router!r}"
            raise PluginError(msg)
        if auth not in ("operator", "session", "api_key", "none"):
            msg = f"plugin {self.name!r} added a router with auth {auth!r}; use operator, session, api_key, or none"
            raise PluginError(msg)
        self._plugin.routers.append(PluginRouter(router, auth))

    def add_cli(self, group: click.Group) -> None:
        """Attach ``group`` to the ``otari`` command line as a top-level group."""
        if not isinstance(group, click.Group):
            msg = f"plugin {self.name!r} added a CLI group that is not a click.Group: {group!r}"
            raise PluginError(msg)
        if not group.name:
            msg = f"plugin {self.name!r} added a CLI group with no name"
            raise PluginError(msg)
        self._plugin.cli_groups.append(group)

    def add_migrations(self, script_location: Path | str) -> None:
        """Register an Alembic script directory, run against the plugin's own version table."""
        path = Path(script_location)
        if not path.is_dir():
            msg = f"plugin {self.name!r} named a migrations directory that does not exist: {path}"
            raise PluginError(msg)
        self._plugin.migrations.append(path)

    def add_traffic_observer(self, observer: Any) -> None:
        """Watch inference traffic: see ``gateway.plugins.traffic``.

        ``observer`` implements ``on_request`` and/or ``on_tool_call``, sync or
        async. Its annotations reach the usage row; its decisions are recorded.
        """
        if not any(callable(getattr(observer, name, None)) for name in ("on_request", "on_tool_call")):
            msg = (
                f"plugin {self.name!r} added a traffic observer with neither on_request nor on_tool_call: {observer!r}"
            )
            raise PluginError(msg)
        self._plugin.observers.append(observer)


class PluginRegistry:
    """Every plugin this process discovered, loaded or not, in load order."""

    def __init__(self, directory: Path, plugins: list[LoadedPlugin], problems: list[DiscoveryProblem]) -> None:
        self.directory = directory
        self._plugins = plugins
        self.problems = problems

    def __iter__(self) -> Iterator[LoadedPlugin]:
        return iter(self._plugins)

    def __len__(self) -> int:
        return len(self._plugins)

    def get(self, name: str) -> LoadedPlugin | None:
        return next((plugin for plugin in self._plugins if plugin.name == name), None)

    def loaded(self) -> list[LoadedPlugin]:
        return [plugin for plugin in self._plugins if plugin.status == "loaded"]

    def traffic_observers(self) -> list[tuple[str, Any]]:
        """Every loaded plugin's traffic observers, as (plugin name, observer) pairs."""
        return [(plugin.name, observer) for plugin in self.loaded() for observer in plugin.observers]

    def record_pending(self, manifest: PluginManifest, package_dir: Path, install_dir: Path) -> LoadedPlugin:
        """Record a plugin installed into the directory since startup.

        It takes effect on the next start. A plugin already running keeps every
        contribution until then, so an upload of its next version never leaves
        a half-loaded plugin behind; the running entry only says what is waiting.
        A plugin not running is listed as pending so the dashboard can say so.
        """
        current = self.get(manifest.name)
        if current is not None and current.status != "pending_restart":
            current.pending = f"version {manifest.version} is installed and loads on the next start"
            return current
        pending = LoadedPlugin(
            manifest=manifest,
            source="directory",
            package_dir=package_dir,
            install_dir=install_dir,
            status="pending_restart",
        )
        self._plugins = [plugin for plugin in self._plugins if plugin.name != manifest.name]
        self._plugins.append(pending)
        return pending

    def forget(self, name: str) -> None:
        """Note that a plugin's directory was removed.

        A running plugin keeps every contribution until restart (a router cannot
        be unmounted, and dropping the rest would leave it half loaded), so the
        entry stays loaded and says a restart is owed; a pending one is dropped.
        """
        for plugin in self._plugins:
            if plugin.name == name and plugin.status != "pending_restart":
                plugin.pending = "removed from disk; unloads on the next start"
                return
        self._plugins = [plugin for plugin in self._plugins if plugin.name != name]

    def changes_on_disk(self) -> bool:
        """Whether the plugins directory no longer matches what this process loaded.

        Read from disk rather than from memory, so every worker of a deployment
        answers the same after one of them took an install or a removal.
        """
        discovered, _ = discover_plugins(self.directory)
        on_disk = {d.manifest.name: d.manifest.version for d in discovered if d.install_dir is not None}
        loaded = {p.name: p.manifest.version for p in self._plugins if p.install_dir is not None}
        return on_disk != loaded or any(p.pending for p in self._plugins)

    @property
    def summary(self) -> str:
        loaded = ", ".join(f"{plugin.name} {plugin.manifest.version}" for plugin in self.loaded())
        failed = ", ".join(plugin.name for plugin in self._plugins if plugin.status == "failed")
        parts = [f"loaded {loaded}" if loaded else "no plugins loaded"]
        if failed:
            parts.append(f"failed {failed}")
        if self.problems:
            parts.append(f"{len(self.problems)} undescribable")
        return "; ".join(parts)


def _actual_contributions(plugin: LoadedPlugin) -> set[str]:
    """What a plugin registered, in the manifest's vocabulary."""
    actual: set[str] = set()
    if plugin.routers:
        actual.add("routes")
    if plugin.cli_groups:
        actual.add("cli")
    if plugin.migrations:
        actual.add("migrations")
    if plugin.observers:
        actual.add("traffic")
    if plugin.manifest.ui is not None:
        actual.add("ui")
    return actual


def _undeclared_contributions(plugin: LoadedPlugin) -> set[str]:
    return _actual_contributions(plugin) - set(plugin.manifest.contributes)


def _version_tuple(text: str) -> tuple[int, ...]:
    """The leading numeric components of a version string, for a soft comparison."""
    numbers: list[int] = []
    for part in text.split("."):
        digits = ""
        for character in part:
            if not character.isdigit():
                break
            digits += character
        if not digits:
            break
        numbers.append(int(digits))
    return tuple(numbers)


def _imported_from_elsewhere(manifest: PluginManifest, package_dir: Path) -> Path | None:
    """Where ``manifest.package`` is already imported from, when that is not ``package_dir``.

    ``import_module`` returns whatever ``sys.modules`` holds, so a package this
    process imported from another directory (the plugin the CLI attached from
    the default directory, say, before ``serve`` loaded the configured one)
    would silently stand in for this plugin's code.
    """
    module = sys.modules.get(manifest.package)
    origin = getattr(module, "__file__", None) if module is not None else None
    if not origin:
        return None
    loaded_from = Path(origin).resolve().parent
    return None if loaded_from == package_dir.resolve() else loaded_from


def _resolve_register(manifest: PluginManifest, package_dir: Path) -> Callable[[PluginContext], None]:
    elsewhere = _imported_from_elsewhere(manifest, package_dir)
    if elsewhere is not None:
        msg = f"package {manifest.package!r} is already imported from {elsewhere}, not from {package_dir}"
        raise PluginError(msg)
    module = importlib.import_module(manifest.package)
    register = getattr(module, manifest.entrypoint, None)
    if register is None:
        msg = f"package {manifest.package!r} has no attribute {manifest.entrypoint!r}"
        raise PluginError(msg)
    if not callable(register):
        msg = f"{manifest.package}.{manifest.entrypoint} is not callable"
        raise PluginError(msg)
    if inspect.iscoroutinefunction(register):
        msg = f"{manifest.package}.{manifest.entrypoint} is async; register must be a plain def"
        raise PluginError(msg)
    return register  # type: ignore[no-any-return]


def _load_one(discovered: DiscoveredPlugin, settings: dict[str, Any], container: "Container | None") -> LoadedPlugin:
    manifest = discovered.manifest
    plugin = LoadedPlugin(
        manifest=manifest,
        source=discovered.source,
        package_dir=discovered.package_dir,
        install_dir=discovered.install_dir,
        status="loaded",
    )
    if manifest.min_otari_version and _version_tuple(__version__) < _version_tuple(manifest.min_otari_version):
        logger.warning(
            "Plugin %s wants otari >= %s and this is %s; loading it anyway",
            manifest.name,
            manifest.min_otari_version,
            __version__,
        )
    if discovered.install_dir is not None:
        # The directory holding the package, so the package name imports. Appended,
        # not put first: a plugin's tree must not shadow a module the gateway
        # imports lazily later (its password hashing, its provider SDKs).
        entry = str(discovered.package_dir.parent)
        if entry not in sys.path:
            sys.path.append(entry)
    try:
        register = _resolve_register(manifest, discovered.package_dir)
        outcome = register(PluginContext(plugin, settings, container))
        if inspect.isawaitable(outcome):
            outcome.close()
            msg = f"{manifest.package}.{manifest.entrypoint} returned an awaitable; register must run when called"
            raise PluginError(msg)
    except Exception as error:  # noqa: BLE001 one plugin's failure must not stop the load
        logger.error("Plugin %s failed to load: %s\n%s", manifest.name, error, traceback.format_exc())
        plugin.status = "failed"
        plugin.error = f"{type(error).__name__}: {error}"
        plugin.routers.clear()
        plugin.cli_groups.clear()
        plugin.migrations.clear()
        plugin.observers.clear()
        return plugin
    undeclared = _undeclared_contributions(plugin)
    if undeclared:
        # The declaration is what an operator read before installing; a plugin
        # that does more than it said is refused rather than trusted.
        plugin.status = "failed"
        plugin.error = (
            f"registered {', '.join(sorted(undeclared))} without declaring it in the manifest's "
            f"contributes list ({', '.join(manifest.contributes) or 'empty'})"
        )
        logger.error("Plugin %s: %s", manifest.name, plugin.error)
        plugin.routers.clear()
        plugin.cli_groups.clear()
        plugin.migrations.clear()
        plugin.observers.clear()
        return plugin
    for declared in manifest.contributes:
        if declared not in _actual_contributions(plugin) and declared != "ui":
            logger.warning("Plugin %s declares %r but registered nothing of the kind", manifest.name, declared)
    if manifest.ui is not None:
        ui_dir = (discovered.package_dir / manifest.ui.path).resolve()
        if ui_dir.is_dir() and (ui_dir / "index.html").is_file():
            plugin.ui = UiContribution(directory=ui_dir, label=manifest.ui.label)
        else:
            logger.warning("Plugin %s declares a UI at %s but there is no index.html there", manifest.name, ui_dir)
    return plugin


def plugins_directory(config: "GatewayConfig") -> Path:
    return Path(config.plugins.directory).expanduser().resolve()


def load_plugins(config: "GatewayConfig", container: "Container | None" = None) -> PluginRegistry:
    """Discover and load every plugin for this process.

    Called once per app in ``create_app`` and once per CLI invocation. Never
    raises for a plugin's own failure; a plugin that cannot load is listed as
    failed.
    """
    plugins_config: PluginsConfig = config.plugins
    directory = plugins_directory(config)
    if not plugins_config.enabled:
        registry = PluginRegistry(directory, [], [])
        logger.info("Plugins: disabled by configuration")
        return registry
    discovered, problems = discover_plugins(directory)
    for problem in problems:
        logger.warning("Plugin at %s (%s) could not be described: %s", problem.location, problem.source, problem.error)
    disabled = set(plugins_config.disabled)
    loaded: list[LoadedPlugin] = []
    for candidate in discovered:
        if candidate.manifest.name in disabled:
            loaded.append(
                LoadedPlugin(
                    manifest=candidate.manifest,
                    source=candidate.source,
                    package_dir=candidate.package_dir,
                    install_dir=candidate.install_dir,
                    status="disabled",
                )
            )
            continue
        loaded.append(_load_one(candidate, plugins_config.plugin_settings(candidate.manifest.name), container))
    registry = PluginRegistry(directory, loaded, problems)
    logger.info("Plugins: %s", registry.summary)
    return registry
