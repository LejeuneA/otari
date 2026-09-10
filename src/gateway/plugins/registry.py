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


@dataclass
class LoadedPlugin:
    """One discovered plugin and what loading it produced."""

    manifest: PluginManifest
    source: PluginSource
    package_dir: Path
    install_dir: Path | None
    status: PluginStatus
    error: str | None = None
    routers: list[APIRouter] = field(default_factory=list)
    cli_groups: list[click.Group] = field(default_factory=list)
    migrations: list[Path] = field(default_factory=list)
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

    def add_router(self, router: APIRouter) -> None:
        """Mount ``router`` under ``/api/v1/plugins/<name>``.

        The router's own prefix, if any, applies below that. Authentication is
        the plugin's to declare per route, as the core routers do; mounting adds
        none.
        """
        if not isinstance(router, APIRouter):
            msg = f"plugin {self.name!r} added a router that is not an APIRouter: {router!r}"
            raise PluginError(msg)
        self._plugin.routers.append(router)

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

    def record_pending(self, manifest: PluginManifest, package_dir: Path, install_dir: Path) -> LoadedPlugin:
        """Record a plugin installed into the directory since startup.

        It takes effect on the next start; until then it is listed as pending so
        the dashboard can say so rather than show nothing.
        """
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
        """Drop a plugin from the listing after its directory was removed.

        A loaded plugin's routes stay mounted until restart; the entry is kept
        as pending so the listing says a restart is owed.
        """
        for plugin in self._plugins:
            if plugin.name == name and plugin.status == "loaded":
                plugin.status = "pending_restart"
                plugin.error = "removed; restart the gateway to unload it"
                return
        self._plugins = [plugin for plugin in self._plugins if plugin.name != name]

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


def _resolve_register(manifest: PluginManifest) -> Callable[[PluginContext], None]:
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
        # The directory holding the package, so the package name imports.
        entry = str(discovered.package_dir.parent)
        if entry not in sys.path:
            sys.path.insert(0, entry)
    try:
        register = _resolve_register(manifest)
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
        return plugin
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
