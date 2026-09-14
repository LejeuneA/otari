"""Plugin manifest and the ``plugins:`` config block.

Schema-less like ``routing`` and ``guardrails`` beside it: nothing here declares
a table, so it stays out of ``gateway.models.__init__``.
"""

import re
import tomllib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MANIFEST_FILENAME = "otari-plugin.toml"
ENTRY_POINT_GROUP = "otari.plugins"
DEFAULT_PLUGINS_DIRECTORY = "./otari-plugins"
DEFAULT_VERIFIED_INDEX_URL = "https://raw.githubusercontent.com/mozilla-ai/otari-plugins/main/index.json"
DEFAULT_GITHUB_TOPIC = "otari-plugin"

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


class PluginManifestError(ValueError):
    """Raised when an ``otari-plugin.toml`` is missing, malformed, or invalid."""


class PluginUiManifest(BaseModel):
    """The dashboard page a plugin ships, as a static directory."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(default="static", description="Directory of static files, relative to the package directory.")
    label: str = Field(min_length=1, max_length=40, description="Sidebar label for the page.")


class PluginManifest(BaseModel):
    """What ``otari-plugin.toml`` declares.

    Read before any plugin code is imported, so the marketplace and the installer
    can describe a plugin without running it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Plugin identifier; also the API and UI mount segment.")
    version: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    package: str = Field(description="The importable Python package the plugin lives in.")
    entrypoint: str = Field(default="register", description="Attribute on the package that registers the plugin.")
    homepage: str | None = Field(default=None, max_length=500)
    min_otari_version: str | None = Field(default=None, max_length=64)
    ui: PluginUiManifest | None = None

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

    @property
    def version_table(self) -> str:
        """The Alembic version table this plugin's migration chain stamps."""
        return f"alembic_version_{self.name.replace('-', '_')}"


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

    def plugin_settings(self, name: str) -> dict[str, Any]:
        """Return the raw block a plugin's ``register`` receives, or an empty one."""
        extra = self.model_extra or {}
        block = extra.get(name)
        return dict(block) if isinstance(block, dict) else {}
