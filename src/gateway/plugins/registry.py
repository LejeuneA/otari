"""Load discovered plugins and hold what each one contributed.

A plugin's ``register`` receives a :class:`PluginContext` and adds routers, CLI
groups, migration directories, observers, backends, hooks, and subscriptions to
it. Everything a plugin adds is recorded on its :class:`LoadedPlugin`;
``gateway.api.main`` mounts the routers, ``gateway.main`` mounts the pages and
runs the hooks and migrations, ``gateway.cli`` attaches the groups, and the
services read the backends. A plugin that fails to import or register is
recorded as failed with its error and skipped, so one broken plugin does not
take the gateway down with it; the dashboard shows the failure.
"""

import asyncio
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
from gateway.models.plugins import (
    PLUGIN_API_VERSION,
    PluginManifest,
    PluginPageManifest,
    PluginsConfig,
    RuntimeMode,
    setting_value_matches,
)
from gateway.plugins.discovery import DiscoveredPlugin, DiscoveryProblem, PluginSource, discover_plugins
from gateway.plugins.events import EventBus, Handler
from gateway.plugins.guardrails import GuardrailBackend
from gateway.version import __version__

if TYPE_CHECKING:
    from gateway.container import Container
    from gateway.core.config import GatewayConfig

PluginStatus = Literal["loaded", "failed", "disabled", "pending_restart"]
Hook = Callable[[], Any]
SettingsListener = Callable[[dict[str, Any]], Any]
ToolBackendFactory = Callable[[], Any]


class PluginError(Exception):
    """Raised by :class:`PluginContext` when a contribution is not usable."""


@dataclass(frozen=True)
class PageContribution:
    """One dashboard page a plugin ships, resolved to the directory it is served from."""

    manifest: PluginPageManifest
    directory: Path

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def label(self) -> str:
        return self.manifest.label


@dataclass
class HealthCheck:
    check: Hook
    critical: bool


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
    config: dict[str, Any] = field(default_factory=dict)
    routers: list[PluginRouter] = field(default_factory=list)
    cli_groups: list[click.Group] = field(default_factory=list)
    migrations: list[Path] = field(default_factory=list)
    observers: list[Any] = field(default_factory=list)
    pages: list[PageContribution] = field(default_factory=list)
    startup_hooks: list[Hook] = field(default_factory=list)
    shutdown_hooks: list[Hook] = field(default_factory=list)
    health_checks: list[HealthCheck] = field(default_factory=list)
    subscriptions: list[tuple[str, Handler]] = field(default_factory=list)
    guardrails: dict[str, GuardrailBackend] = field(default_factory=dict)
    tools: dict[str, ToolBackendFactory] = field(default_factory=dict)
    router_backends: dict[str, Any] = field(default_factory=dict)
    settings_listeners: list[SettingsListener] = field(default_factory=list)
    health: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def api_prefix(self) -> str:
        """Where the plugin's routers mount, below the API root."""
        return f"/plugins/{self.name}"

    def page_url(self, page: PageContribution) -> str:
        """Where a page is served: the first page at the bare ``/ui/`` mount, the rest under their id."""
        base = f"/plugins/{self.name}/ui/"
        if self.pages and page is not self.pages[0]:
            base = f"{base}{page.id}/"
        return f"{base}{page.manifest.entry}"

    @property
    def ui(self) -> PageContribution | None:
        """The plugin's first page, or ``None`` when it ships none."""
        return self.pages[0] if self.pages else None

    @property
    def ui_url(self) -> str | None:
        return self.page_url(self.pages[0]) if self.pages else None

    def clear_contributions(self) -> None:
        self.routers.clear()
        self.cli_groups.clear()
        self.migrations.clear()
        self.observers.clear()
        self.pages.clear()
        self.startup_hooks.clear()
        self.shutdown_hooks.clear()
        self.health_checks.clear()
        self.subscriptions.clear()
        self.guardrails.clear()
        self.tools.clear()
        self.router_backends.clear()
        self.settings_listeners.clear()


class PluginContext:
    """What a plugin's ``register(ctx)`` is handed.

    ``config`` is the plugin's own block of ``config.yml`` (``plugins.<name>``)
    over the manifest's setting defaults. It is the live dict: a dashboard edit
    updates it in place and calls every ``on_settings_change`` listener.
    ``container`` is the composition root, so a plugin can resolve a port; it is
    ``None`` when plugins are loaded for the CLI alone, where no app exists.
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

        ``observer`` implements any of ``on_request``, ``on_tool_call``, and
        ``on_response``, sync or async. Its annotations reach the usage row;
        its decisions (block, inject a system text, deny a tool call) apply.
        """
        methods = ("on_request", "on_tool_call", "on_response")
        if not any(callable(getattr(observer, name, None)) for name in methods):
            msg = f"plugin {self.name!r} added a traffic observer with none of {', '.join(methods)}: {observer!r}"
            raise PluginError(msg)
        self._plugin.observers.append(observer)

    def on_startup(self, hook: Hook) -> None:
        """Run ``hook`` (sync or async, no arguments) when the gateway starts, after migrations."""
        self._plugin.startup_hooks.append(_callable(self, hook, "startup hook"))

    def on_shutdown(self, hook: Hook) -> None:
        """Run ``hook`` when the gateway stops."""
        self._plugin.shutdown_hooks.append(_callable(self, hook, "shutdown hook"))

    def add_health_check(self, check: Hook, *, critical: bool = False) -> None:
        """Report into ``/health``: ``check`` returns truthy for healthy, or raises.

        A ``critical`` check that fails takes ``/health/readiness`` to 503; a
        plain one is reported and changes nothing else.
        """
        self._plugin.health_checks.append(HealthCheck(_callable(self, check, "health check"), critical))

    def subscribe(self, event: str, handler: Handler) -> None:
        """Run ``handler(event)`` for every event named ``event`` (or ``"*"``); see ``gateway.plugins.events``."""
        if not isinstance(event, str) or not event:
            msg = f"plugin {self.name!r} subscribed to an event with no name"
            raise PluginError(msg)
        self._plugin.subscriptions.append((event, _callable(self, handler, "event handler")))

    def add_guardrail(self, name: str, backend: GuardrailBackend) -> None:
        """Offer a guardrail profile named ``<plugin>:<name>``; see ``gateway.plugins.guardrails``."""
        _valid_key(self, name, "guardrail")
        if not callable(getattr(backend, "check", None)):
            msg = f"plugin {self.name!r} added a guardrail {name!r} with no check method"
            raise PluginError(msg)
        self._plugin.guardrails[name] = backend

    def add_tool(self, name: str, factory: ToolBackendFactory) -> None:
        """Offer a tool backend the gateway runs for the model when a request opts in.

        ``factory`` is called per request and returns an object with the
        ``ToolBackend`` members (``openai_tools``, ``owns_tool``, ``call_tool``,
        ``purpose_hints``); an async context manager is entered for the request.
        A request opts in with a tool entry ``{"type": "plugin", "name": "<plugin>:<name>"}``.
        """
        _valid_key(self, name, "tool")
        if not callable(factory):
            msg = f"plugin {self.name!r} added a tool {name!r} whose factory is not callable"
            raise PluginError(msg)
        self._plugin.tools[name] = factory

    def add_router_backend(self, name: str, backend: Any) -> None:
        """Offer a router backend a routing policy names as ``backend: <plugin>:<name>``."""
        _valid_key(self, name, "router backend")
        if not callable(getattr(backend, "rank", None)):
            msg = f"plugin {self.name!r} added a router backend {name!r} with no rank method"
            raise PluginError(msg)
        self._plugin.router_backends[name] = backend

    def on_settings_change(self, listener: SettingsListener) -> None:
        """Run ``listener(config)`` after the dashboard changes the plugin's settings."""
        self._plugin.settings_listeners.append(_callable(self, listener, "settings listener"))


def _callable(ctx: PluginContext, value: Any, what: str) -> Any:
    if not callable(value):
        msg = f"plugin {ctx.name!r} added a {what} that is not callable: {value!r}"
        raise PluginError(msg)
    return value


def _valid_key(ctx: PluginContext, name: str, what: str) -> None:
    from gateway.models.plugins import PLUGIN_NAME_PATTERN

    if not isinstance(name, str) or not PLUGIN_NAME_PATTERN.fullmatch(name):
        msg = f"plugin {ctx.name!r} added a {what} whose name {name!r} does not match {PLUGIN_NAME_PATTERN.pattern}"
        raise PluginError(msg)


class PluginRegistry:
    """Every plugin this process discovered, loaded or not, in load order."""

    def __init__(
        self,
        directory: Path,
        plugins: list[LoadedPlugin],
        problems: list[DiscoveryProblem],
        *,
        event_timeout_ms: int = 5_000,
        hook_timeout_ms: int = 30_000,
    ) -> None:
        self.directory = directory
        self._plugins = plugins
        self.problems = problems
        self._hook_timeout = hook_timeout_ms / 1000
        self.events = EventBus(
            ((plugin.name, name, handler) for plugin in self.loaded() for name, handler in plugin.subscriptions),
            timeout_ms=event_timeout_ms,
        )

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

    def guardrail_backends(self) -> dict[str, GuardrailBackend]:
        """Every loaded plugin's guardrails, keyed by the profile that names them."""
        from gateway.plugins.guardrails import profile_name

        return {
            profile_name(plugin.name, name): backend
            for plugin in self.loaded()
            for name, backend in plugin.guardrails.items()
        }

    def tool_backends(self) -> dict[str, ToolBackendFactory]:
        """Every loaded plugin's tools, keyed ``<plugin>:<name>``."""
        return {f"{plugin.name}:{name}": factory for plugin in self.loaded() for name, factory in plugin.tools.items()}

    def router_backends(self) -> dict[str, Any]:
        """Every loaded plugin's router backends, keyed ``<plugin>:<name>``."""
        return {
            f"{plugin.name}:{name}": backend
            for plugin in self.loaded()
            for name, backend in plugin.router_backends.items()
        }

    def pages(self) -> list[tuple[LoadedPlugin, PageContribution]]:
        return [(plugin, page) for plugin in self.loaded() for page in plugin.pages]

    async def _run_hook(self, plugin: LoadedPlugin, hook: Hook, what: str) -> Any:
        outcome = hook()
        if inspect.isawaitable(outcome):
            outcome = await asyncio.wait_for(outcome, timeout=self._hook_timeout)
        return outcome

    async def startup(self) -> None:
        """Run every loaded plugin's startup hooks; a plugin whose hook fails is marked failed."""
        for plugin in self.loaded():
            for hook in plugin.startup_hooks:
                try:
                    await self._run_hook(plugin, hook, "startup")
                except Exception as error:  # noqa: BLE001 one plugin's failure must not stop the boot
                    logger.error("Plugin %s failed at startup: %s\n%s", plugin.name, error, traceback.format_exc())
                    plugin.status = "failed"
                    plugin.error = f"startup hook: {type(error).__name__}: {error}"
                    break

    async def shutdown(self) -> None:
        for plugin in self._plugins:
            for hook in plugin.shutdown_hooks:
                try:
                    await self._run_hook(plugin, hook, "shutdown")
                except Exception:  # noqa: BLE001 shutdown runs every hook regardless
                    logger.exception("Plugin %s failed at shutdown", plugin.name)
        await self.events.drain()

    async def check_health(self) -> tuple[dict[str, str], bool]:
        """Run every loaded plugin's health checks.

        Returns each plugin's status text and whether a critical check failed.
        """
        report: dict[str, str] = {}
        critical_failure = False
        for plugin in self.loaded():
            if not plugin.health_checks:
                continue
            problems: list[str] = []
            for check in plugin.health_checks:
                try:
                    outcome = await self._run_hook(plugin, check.check, "health")
                    if outcome is not None and not outcome:
                        raise RuntimeError("check returned a falsy value")  # noqa: TRY301
                except Exception as error:  # noqa: BLE001 a failing check is a report, not a crash
                    problems.append(f"{type(error).__name__}: {error}")
                    critical_failure = critical_failure or check.critical
            report[plugin.name] = "ok" if not problems else "failing: " + "; ".join(problems)
            plugin.health = {"status": report[plugin.name]}
        return report, critical_failure

    async def apply_settings(self, name: str, values: dict[str, Any]) -> LoadedPlugin | None:
        """Update a loaded plugin's live config in place and tell its listeners."""
        plugin = self.get(name)
        if plugin is None or plugin.status != "loaded":
            return plugin
        plugin.config.update(values)
        for listener in plugin.settings_listeners:
            try:
                outcome = listener(plugin.config)
                if inspect.isawaitable(outcome):
                    await asyncio.wait_for(outcome, timeout=self._hook_timeout)
            except Exception:  # noqa: BLE001 the write already happened; the listener is best effort
                logger.exception("Plugin %s: settings listener raised", plugin.name)
        return plugin

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
    if plugin.manifest.pages:
        actual.add("ui")
    if plugin.startup_hooks or plugin.shutdown_hooks or plugin.health_checks:
        actual.add("lifecycle")
    if plugin.subscriptions:
        actual.add("events")
    if plugin.guardrails:
        actual.add("guardrails")
    if plugin.tools:
        actual.add("tools")
    if plugin.router_backends:
        actual.add("routing")
    return actual


def _undeclared_contributions(plugin: LoadedPlugin) -> set[str]:
    return _actual_contributions(plugin) - set(plugin.manifest.contributes)


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


def build_plugin_config(manifest: PluginManifest, raw: dict[str, Any]) -> dict[str, Any]:
    """The manifest's setting defaults under the operator's block, type-checked against the manifest.

    Raises:
        PluginError: When a declared setting is present with a value of the wrong type.

    """
    config: dict[str, Any] = {**manifest.setting_defaults, **raw}
    for key, spec in manifest.settings.items():
        value = config.get(key)
        if value is not None and not setting_value_matches(spec.type, value):
            msg = f"setting {key!r} must be of type {spec.type}, got {type(value).__name__}"
            raise PluginError(msg)
    declared = set(manifest.all_config_keys)
    unknown = sorted(key for key in raw if declared and key not in declared)
    if unknown:
        logger.warning("Plugin %s: config keys %s are not declared in its manifest", manifest.name, ", ".join(unknown))
    return config


def runtime_mode(config: "GatewayConfig") -> RuntimeMode:
    if config.is_hybrid_mode:
        return "hybrid"
    if config.is_hosted_mode:
        return "hosted"
    return "standalone"


def _load_one(
    discovered: DiscoveredPlugin,
    settings: dict[str, Any],
    container: "Container | None",
    mode: RuntimeMode,
) -> LoadedPlugin:
    manifest = discovered.manifest
    plugin = LoadedPlugin(
        manifest=manifest,
        source=discovered.source,
        package_dir=discovered.package_dir,
        install_dir=discovered.install_dir,
        status="loaded",
    )
    if manifest.plugin_api > PLUGIN_API_VERSION:
        plugin.status = "failed"
        plugin.error = f"needs plugin API {manifest.plugin_api}; this gateway provides {PLUGIN_API_VERSION}"
        logger.error("Plugin %s: %s", manifest.name, plugin.error)
        return plugin
    refusal = manifest.needs_newer_gateway(__version__)
    if refusal is not None:
        # Refused before the import: the plugin said what it needs, and running
        # it here would fail somewhere less legible than this.
        plugin.status = "failed"
        plugin.error = refusal
        logger.error("Plugin %s not loaded: %s", manifest.name, refusal)
        return plugin
    if mode not in manifest.modes:
        plugin.status = "disabled"
        plugin.error = f"not for {mode} mode (supports {', '.join(manifest.modes)})"
        logger.info("Plugin %s: %s", manifest.name, plugin.error)
        return plugin
    if discovered.install_dir is not None:
        # The directory holding the package, so the package name imports. Appended,
        # not put first: a plugin's tree must not shadow a module the gateway
        # imports lazily later (its password hashing, its provider SDKs).
        entry = str(discovered.package_dir.parent)
        if entry not in sys.path:
            sys.path.append(entry)
    try:
        plugin.config = build_plugin_config(manifest, settings)
        register = _resolve_register(manifest, discovered.package_dir)
        outcome = register(PluginContext(plugin, plugin.config, container))
        if inspect.isawaitable(outcome):
            outcome.close()
            msg = f"{manifest.package}.{manifest.entrypoint} returned an awaitable; register must run when called"
            raise PluginError(msg)
    except Exception as error:  # noqa: BLE001 one plugin's failure must not stop the load
        logger.error("Plugin %s failed to load: %s\n%s", manifest.name, error, traceback.format_exc())
        plugin.status = "failed"
        plugin.error = f"{type(error).__name__}: {error}"
        plugin.clear_contributions()
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
        plugin.clear_contributions()
        return plugin
    for declared in manifest.contributes:
        # "ui" is the manifest's own page, checked at parse time; the rest is what register did.
        if declared not in _actual_contributions(plugin) and (declared != "ui" or manifest.ui is None):
            logger.warning("Plugin %s declares %r but registered nothing of the kind", manifest.name, declared)
    for page in manifest.pages:
        page_dir = (discovered.package_dir / page.path).resolve()
        if page_dir.is_dir() and (page_dir / "index.html").is_file():
            plugin.pages.append(PageContribution(manifest=page, directory=page_dir))
        else:
            logger.warning(
                "Plugin %s declares page %s at %s but there is no index.html there", manifest.name, page.id, page_dir
            )
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
    mode = runtime_mode(config)
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
        loaded.append(_load_one(candidate, plugins_config.plugin_settings(candidate.manifest.name), container, mode))
    registry = PluginRegistry(
        directory,
        loaded,
        problems,
        event_timeout_ms=plugins_config.event_timeout_ms,
        hook_timeout_ms=plugins_config.hook_timeout_ms,
    )
    from gateway.services.routing.backends import set_plugin_router_backends

    set_plugin_router_backends(registry.router_backends())
    logger.info("Plugins: %s", registry.summary)
    return registry
