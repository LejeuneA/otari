"""Plugin manifest and the ``plugins:`` config block.

Schema-less like ``routing`` and ``guardrails`` beside it: nothing here declares
a table, so it stays out of ``gateway.models.__init__``.
"""

import itertools
import re
import tomllib
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MANIFEST_FILENAME = "otari-plugin.toml"
ENTRY_POINT_GROUP = "otari.plugins"
DEFAULT_PLUGINS_DIRECTORY = "./otari-plugins"
DEFAULT_VERIFIED_INDEX_URL = "https://raw.githubusercontent.com/mozilla-ai/otari-plugins/main/index.json"
DEFAULT_GITHUB_TOPIC = "otari-plugin"

# The contract between a plugin and the gateway: what ``gateway.plugins.api``
# exports and how ``PluginContext`` behaves. A manifest names the version it
# was written against; a gateway refuses a plugin that wants a newer one.
PLUGIN_API_VERSION = 1

# The name is also a URL segment (``/api/v1/plugins/<name>``, ``/plugins/<name>/ui``),
# a directory name under the plugins directory, and a suffix on the plugin's
# Alembic version table, so it is kept to the characters all three accept. The
# length keeps ``alembic_version_<name>`` inside PostgreSQL's 63-byte identifier
# limit, past which two names would be truncated onto one table.
PLUGIN_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,46}$")
# Segments the core already answers under ``/api/v1/plugins/`` and keys the
# ``plugins:`` block owns: a plugin named one of these would be shadowed or
# would have no settings block of its own. (``allow_install`` cannot match the
# pattern, so it needs no entry.)
RESERVED_PLUGIN_NAMES = frozenset({"install", "upload", "marketplace", "directory", "disabled"})
PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
SETTING_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# The kinds of thing a plugin can add. Each maps to one PluginContext method,
# and the registry checks what a plugin registered against what it declared.
Contribution = Literal[
    "routes", "cli", "migrations", "ui", "traffic", "lifecycle", "events", "guardrails", "tools", "routing"
]
KNOWN_CONTRIBUTIONS: frozenset[str] = frozenset(get_args(Contribution))
CONTRIBUTION_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def version_tuple(text: str) -> tuple[int, ...]:
    """The leading numeric components of a version string, for a soft comparison."""
    numbers: list[int] = []
    for part in text.split("."):
        digits = "".join(itertools.takewhile(str.isdigit, part))
        if not digits:
            break
        numbers.append(int(digits))
    return tuple(numbers)


RuntimeMode = Literal["standalone", "hosted", "hybrid"]
ALL_MODES: tuple[RuntimeMode, ...] = ("standalone", "hosted", "hybrid")

# Where a page's sidebar row goes. The section ids are the dashboard rail's own
# (``web/src/app/nav/registry.ts``); ``none`` is a page reachable only from the
# plugin's card, for a plugin that wants no row of its own.
PageSection = Literal["observe", "build", "access", "extend", "none"]
# A page can instead nest under one of the rail's items that already has
# children, so a guardrail plugin's page sits beside Otari's own guardrails page.
PageParent = Literal["tools", "routing"]
PageAudience = Literal["operator", "member"]
# A closed set, mapped to the dashboard's Feather icons; a name it does not know
# falls back to the generic one.
PageIcon = Literal[
    "layout",
    "shield",
    "activity",
    "tool",
    "zap",
    "package",
    "check-circle",
    "git-branch",
    "eye",
    "bell",
    "book",
    "database",
    "globe",
    "message-square",
    "sliders",
    "terminal",
    "search",
    "lock",
]

SettingType = Literal["str", "int", "float", "bool", "list", "object"]
_SETTING_PYTHON_TYPES: dict[str, tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
    "list": (list,),
    "object": (dict,),
}


class PluginManifestError(ValueError):
    """Raised when an ``otari-plugin.toml`` is missing, malformed, or invalid."""


class PluginUiManifest(BaseModel):
    """The one dashboard page a plugin ships, the short form of ``[[plugin.pages]]``."""

    model_config = ConfigDict(extra="ignore")

    path: str = Field(default="static", description="Directory of static files, relative to the package directory.")
    label: str = Field(min_length=1, max_length=40, description="Sidebar label for the page.")


class PluginPageManifest(BaseModel):
    """One dashboard page: a static directory, and where its row goes in the rail."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(
        description="Page identifier, unique within the plugin; the dashboard path is /plugins/<plugin>/<id>."
    )
    label: str = Field(min_length=1, max_length=40, description="Sidebar and breadcrumb label.")
    path: str = Field(default="static", description="Directory of static files, relative to the package directory.")
    entry: str = Field(
        default="",
        max_length=200,
        description="Appended to the page's URL, so several pages can share one bundle: for example '#/runs'.",
    )
    icon: PageIcon = "layout"
    section: PageSection = Field(default="extend", description="The rail section the row goes in.")
    parent: PageParent | None = Field(
        default=None, description="Nest the row under this rail item instead of placing it in a section."
    )
    order: int = Field(default=100, ge=0, le=1000, description="Sort key among plugin rows in the same place.")
    audience: PageAudience = Field(
        default="operator", description="Who sees the row: deployment operators only, or every signed-in member."
    )

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not PLUGIN_NAME_PATTERN.fullmatch(value):
            msg = f"page id {value!r} must match {PLUGIN_NAME_PATTERN.pattern}"
            raise ValueError(msg)
        return value

    @field_validator("entry")
    @classmethod
    def _valid_entry(cls, value: str) -> str:
        if value.startswith("/") or "://" in value or ".." in value:
            msg = f"page entry {value!r} must be relative to the page's own directory"
            raise ValueError(msg)
        return value


class PluginSettingSpec(BaseModel):
    """One key of a plugin's config block, typed so the dashboard can render and validate it."""

    model_config = ConfigDict(extra="ignore")

    type: SettingType
    default: Any = None
    description: str = Field(default="", max_length=500)
    secret: bool = Field(default=False, description="Masked in the dashboard and never returned once set.")
    editable: bool = Field(default=True, description="Whether the dashboard may change it; config.yml always can.")

    @model_validator(mode="after")
    def _default_matches_type(self) -> "PluginSettingSpec":
        if self.default is not None and not setting_value_matches(self.type, self.default):
            msg = f"default {self.default!r} is not of type {self.type}"
            raise ValueError(msg)
        return self


def setting_value_matches(kind: str, value: Any) -> bool:
    """Whether ``value`` is of the manifest type ``kind`` (bool is not an int here)."""
    if kind in ("int", "float") and isinstance(value, bool):
        return False
    return isinstance(value, _SETTING_PYTHON_TYPES[kind])


class PluginManifest(BaseModel):
    """What ``otari-plugin.toml`` declares.

    Read before any plugin code is imported, so the marketplace and the installer
    can describe a plugin without running it.

    A key this gateway does not know is ignored, and a ``contributes`` kind it
    does not know is kept: a plugin written for a newer gateway still describes
    itself here, and is refused at load with the reason (see
    :meth:`needs_newer_gateway`) rather than refused at parse with a stack trace.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(description="Plugin identifier; also the API and UI mount segment.")
    version: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    package: str = Field(description="The importable Python package the plugin lives in.")
    entrypoint: str = Field(default="register", description="Attribute on the package that registers the plugin.")
    plugin_api: int = Field(
        default=1,
        ge=1,
        description=(
            "The plugin API version the plugin is written against (gateway.plugins.api). "
            "A gateway refuses a plugin that needs a newer one than it provides."
        ),
    )
    modes: list[RuntimeMode] = Field(
        default_factory=lambda: list(ALL_MODES),
        description="The runtime modes the plugin supports; it is not loaded in the others.",
    )
    homepage: str | None = Field(default=None, max_length=500)
    min_otari_version: str | None = Field(
        default=None, max_length=64, description="The oldest gateway that loads this plugin; older ones refuse it."
    )
    getting_started: str | None = Field(
        default=None,
        max_length=500,
        description="URL of the page that walks a new user through setting the plugin up.",
    )
    contributes: list[str] = Field(
        default_factory=list,
        description=(
            "What the plugin adds to the gateway. Declared before any code runs, shown before "
            "install, and enforced at load: a plugin that registers something it did not declare "
            "is refused, and one declaring a kind this gateway does not know needs a newer gateway."
        ),
    )
    config_keys: list[str] = Field(
        default_factory=list,
        description="Keys the plugin reads from its own block of config.yml, beyond those under [plugin.settings].",
    )
    settings: dict[str, PluginSettingSpec] = Field(
        default_factory=dict,
        description="Typed keys of the plugin's config block; the dashboard renders a form from them.",
    )
    ui: PluginUiManifest | None = None
    pages: list[PluginPageManifest] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not PLUGIN_NAME_PATTERN.fullmatch(value):
            msg = f"plugin name {value!r} must match {PLUGIN_NAME_PATTERN.pattern}"
            raise ValueError(msg)
        if value in RESERVED_PLUGIN_NAMES:
            msg = f"plugin name {value!r} is reserved"
            raise ValueError(msg)
        return value

    @field_validator("package")
    @classmethod
    def _valid_package(cls, value: str) -> str:
        if not PACKAGE_NAME_PATTERN.fullmatch(value):
            msg = f"plugin package {value!r} is not an importable module path"
            raise ValueError(msg)
        return value

    @field_validator("entrypoint")
    @classmethod
    def _valid_entrypoint(cls, value: str) -> str:
        if not value.isidentifier():
            msg = f"plugin entrypoint {value!r} is not an identifier"
            raise ValueError(msg)
        return value

    @field_validator("contributes")
    @classmethod
    def _contribution_words(cls, value: list[str]) -> list[str]:
        for kind in value:
            if not isinstance(kind, str) or not CONTRIBUTION_PATTERN.fullmatch(kind):
                msg = f"contributes entry {kind!r} is not a contribution kind"
                raise ValueError(msg)
        return value

    @field_validator("modes")
    @classmethod
    def _some_mode(cls, value: list[RuntimeMode]) -> list[RuntimeMode]:
        if not value:
            msg = "modes must name at least one runtime mode"
            raise ValueError(msg)
        return list(dict.fromkeys(value))

    @field_validator("settings")
    @classmethod
    def _valid_setting_keys(cls, value: dict[str, PluginSettingSpec]) -> dict[str, PluginSettingSpec]:
        for key in value:
            if not SETTING_KEY_PATTERN.fullmatch(key):
                msg = f"setting key {key!r} must match {SETTING_KEY_PATTERN.pattern}"
                raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _one_page_form(self) -> "PluginManifest":
        if self.ui is not None and self.pages:
            msg = "declare either [plugin.ui] or [[plugin.pages]], not both"
            raise ValueError(msg)
        if self.ui is not None:
            self.pages = [PluginPageManifest(id="index", label=self.ui.label, path=self.ui.path)]
            self.ui = None
        if self.pages and "ui" not in self.contributes:
            # Both halves of this check are in the manifest, so it is refused at
            # parse time (upload, install, describe) rather than one restart later.
            msg = 'the manifest ships a dashboard page but does not declare "ui" in contributes'
            raise ValueError(msg)
        ids = [page.id for page in self.pages]
        if len(ids) != len(set(ids)):
            msg = f"page ids must be unique, got {ids}"
            raise ValueError(msg)
        return self

    @property
    def unsupported_contributions(self) -> list[str]:
        """The declared kinds this gateway does not know."""
        return [kind for kind in self.contributes if kind not in KNOWN_CONTRIBUTIONS]

    def needs_newer_gateway(self, current_version: str) -> str | None:
        """Why this gateway cannot load the plugin, or ``None`` when it can.

        Known before the plugin's code is imported: shown in the install dialog,
        and the reason a plugin is listed as failed without having run.
        """
        if self.min_otari_version and version_tuple(current_version) < version_tuple(self.min_otari_version):
            return f"needs otari {self.min_otari_version} or newer; this is {current_version}"
        if unsupported := self.unsupported_contributions:
            return f"declares {', '.join(unsupported)}, which this gateway ({current_version}) does not know"
        return None

    @property
    def version_table(self) -> str:
        """The Alembic version table this plugin's migration chain stamps."""
        return f"alembic_version_{self.name.replace('-', '_')}"

    @property
    def all_config_keys(self) -> list[str]:
        """Every key the plugin reads from its config block, typed or not."""
        return list(dict.fromkeys([*self.settings, *self.config_keys]))

    @property
    def setting_defaults(self) -> dict[str, Any]:
        return {key: spec.default for key, spec in self.settings.items() if spec.default is not None}


def parse_manifest(text: str) -> PluginManifest:
    """Parse the text of an ``otari-plugin.toml``.

    Raises:
        PluginManifestError: If the TOML does not parse or the ``[plugin]`` table
            fails validation.

    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        msg = f"{MANIFEST_FILENAME} is not valid TOML: {error}"
        raise PluginManifestError(msg) from error
    table = data.get("plugin")
    if not isinstance(table, dict):
        msg = f"{MANIFEST_FILENAME} has no [plugin] table"
        raise PluginManifestError(msg)
    try:
        return PluginManifest.model_validate(table)
    except ValueError as error:
        msg = f"{MANIFEST_FILENAME} is invalid: {error}"
        raise PluginManifestError(msg) from error


class MarketplaceConfig(BaseModel):
    """Where the dashboard's marketplace tab reads its two lists from."""

    model_config = ConfigDict(extra="forbid")

    verified_index_url: str = Field(
        default=DEFAULT_VERIFIED_INDEX_URL,
        description="JSON index of plugins mozilla.ai verifies. Empty string disables the verified list.",
    )
    github_topic: str = Field(
        default=DEFAULT_GITHUB_TOPIC,
        description="GitHub topic that lists community plugins. Empty string disables the community list.",
    )
    github_token: str | None = Field(
        default=None,
        description="Optional GitHub token for the topic search, which is rate limited unauthenticated.",
    )


class PluginsConfig(BaseModel):
    """The ``plugins:`` block of ``config.yml``.

    Every key that is not one of the fields below is a plugin's own block, handed
    to that plugin raw as ``PluginContext.config``.
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = Field(
        default=True,
        description=(
            "Whether plugins are discovered and loaded at all. Off, nothing is imported "
            "(OTARI_PLUGINS_ENABLED); the spec generator runs that way so plugin routes never enter the API document."
        ),
    )
    directory: str = Field(
        default=DEFAULT_PLUGINS_DIRECTORY,
        description="Where drop-in plugins live; also where upload and install write.",
    )
    disabled: list[str] = Field(default_factory=list, description="Discovered plugins to leave unloaded.")
    allow_install: bool = Field(
        default=False,
        description=(
            "Let an operator upload or install a plugin through the API and dashboard. A plugin is "
            "code that runs inside the gateway with its access, so this is off until turned on."
        ),
    )
    marketplace: MarketplaceConfig = Field(default_factory=MarketplaceConfig)
    observer_timeout_ms: int = Field(
        default=250,
        ge=1,
        le=10_000,
        description=(
            "How long one plugin's traffic observer may take per call before it is skipped. "
            "Applies to on_request, each tool call, and on_response; see docs/plugins.md."
        ),
    )
    event_timeout_ms: int = Field(
        default=5_000,
        ge=1,
        le=60_000,
        description="How long one plugin's event handler may run before it is cancelled; it runs off the request path.",
    )
    hook_timeout_ms: int = Field(
        default=30_000,
        ge=1,
        le=600_000,
        description="How long a plugin's startup, shutdown, or health hook may take before it is cancelled.",
    )

    def plugin_settings(self, name: str) -> dict[str, Any]:
        """Return the raw block a plugin's ``register`` receives, or an empty one."""
        extra = self.model_extra or {}
        block = extra.get(name)
        return dict(block) if isinstance(block, dict) else {}
