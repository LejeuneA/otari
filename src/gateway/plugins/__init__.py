"""The plugin seam: what a plugin imports, and what the gateway loads it with.

A plugin is a Python package holding an ``otari-plugin.toml`` and a
``register(ctx: PluginContext)`` callable. See ``docs/plugins.md``.
"""

from gateway.models.plugins import PluginManifest, PluginManifestError
from gateway.plugins.registry import (
    LoadedPlugin,
    PluginContext,
    PluginError,
    PluginRegistry,
    load_plugins,
    plugins_directory,
)

__all__ = [
    "LoadedPlugin",
    "PluginContext",
    "PluginError",
    "PluginManifest",
    "PluginManifestError",
    "PluginRegistry",
    "load_plugins",
    "plugins_directory",
]
