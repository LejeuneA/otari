"""Find plugins without importing them.

Two sources, in this order: Python entry points in the ``otari.plugins`` group
(a plugin installed into the gateway's environment with pip or uv), and the
plugins directory (a plugin dropped in by hand, by ``otari plugins install``, or
by the dashboard). Both resolve to the same thing, a package directory holding
an ``otari-plugin.toml``, and nothing here runs plugin code: a manifest that
cannot be read is reported, and the package is only imported when the registry
loads it.
"""

import importlib.metadata
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from gateway.models.plugins import (
    ENTRY_POINT_GROUP,
    MANIFEST_FILENAME,
    PluginManifest,
    PluginManifestError,
    parse_manifest,
)

PluginSource = Literal["entry_point", "directory"]


@dataclass(frozen=True)
class DiscoveredPlugin:
    """A plugin found on disk, described by its manifest and not yet imported."""

    manifest: PluginManifest
    source: PluginSource
    # The package directory, where the manifest lives and the UI directory is
    # resolved from.
    package_dir: Path
    # For a directory plugin: the entry under the plugins directory that holds
    # it, which is what removal deletes and what goes on ``sys.path``. ``None``
    # for an installed distribution.
    install_dir: Path | None = None


@dataclass(frozen=True)
class DiscoveryProblem:
    """A candidate that could not be described, kept so an operator can see it."""

    source: PluginSource
    location: str
    error: str


def read_manifest(path: Path) -> PluginManifest:
    """Read and validate the manifest at ``path``.

    Raises:
        PluginManifestError: If the file is missing or invalid.

    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        msg = f"cannot read {path}: {error}"
        raise PluginManifestError(msg) from error
    return parse_manifest(text)


def find_manifest_in_tree(root: Path) -> Path:
    """Locate the one manifest an unpacked plugin tree holds.

    Accepts a flat layout (``<pkg>/otari-plugin.toml``) and a src layout
    (``src/<pkg>/otari-plugin.toml``), each possibly under one extra directory
    the way a GitHub archive nests its contents.

    Raises:
        PluginManifestError: If no manifest, or more than one, is found.

    """
    patterns = (
        f"*/{MANIFEST_FILENAME}",
        f"src/*/{MANIFEST_FILENAME}",
        f"*/*/{MANIFEST_FILENAME}",
        f"*/src/*/{MANIFEST_FILENAME}",
    )
    found: list[Path] = []
    for pattern in patterns:
        found.extend(candidate for candidate in root.glob(pattern) if candidate.is_file())
    unique = sorted(set(found))
    if not unique:
        msg = f"no {MANIFEST_FILENAME} found under {root}"
        raise PluginManifestError(msg)
    if len(unique) > 1:
        listed = ", ".join(str(path.relative_to(root)) for path in unique)
        msg = f"more than one {MANIFEST_FILENAME} under {root}: {listed}"
        raise PluginManifestError(msg)
    return unique[0]


def _check_package_dir(manifest: PluginManifest, manifest_path: Path) -> None:
    """A directory plugin's package must be the directory the manifest is in."""
    package_dir = manifest_path.parent
    if "." in manifest.package or package_dir.name != manifest.package:
        msg = (
            f"{manifest_path} declares package {manifest.package!r} but sits in {package_dir.name!r}; "
            "a directory plugin's manifest lives inside its top-level package"
        )
        raise PluginManifestError(msg)


def discover_directory_plugin(install_dir: Path) -> DiscoveredPlugin:
    """Describe the plugin one entry under the plugins directory holds.

    Raises:
        PluginManifestError: If the entry holds no usable plugin.

    """
    manifest_path = find_manifest_in_tree(install_dir)
    manifest = read_manifest(manifest_path)
    _check_package_dir(manifest, manifest_path)
    return DiscoveredPlugin(
        manifest=manifest,
        source="directory",
        package_dir=manifest_path.parent,
        install_dir=install_dir,
    )


def discover_entry_point_plugins() -> tuple[list[DiscoveredPlugin], list[DiscoveryProblem]]:
    """Describe every distribution that registers an ``otari.plugins`` entry point."""
    found: list[DiscoveredPlugin] = []
    problems: list[DiscoveryProblem] = []
    for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        package = entry_point.value.partition(":")[0].strip()
        location = f"{entry_point.name} = {entry_point.value!r}"
        try:
            spec = importlib.util.find_spec(package)
        except (ImportError, ValueError) as error:
            problems.append(
                DiscoveryProblem("entry_point", location, f"package {package!r} cannot be located: {error}")
            )
            continue
        if spec is None or not spec.submodule_search_locations:
            problems.append(
                DiscoveryProblem("entry_point", location, f"package {package!r} is not an importable package")
            )
            continue
        package_dir = Path(next(iter(spec.submodule_search_locations)))
        try:
            manifest = read_manifest(package_dir / MANIFEST_FILENAME)
        except PluginManifestError as error:
            problems.append(DiscoveryProblem("entry_point", location, str(error)))
            continue
        if manifest.package != package:
            problems.append(
                DiscoveryProblem(
                    "entry_point",
                    location,
                    f"manifest declares package {manifest.package!r} but the entry point names {package!r}",
                )
            )
            continue
        found.append(DiscoveredPlugin(manifest=manifest, source="entry_point", package_dir=package_dir))
    return found, problems


def discover_directory_plugins(directory: Path) -> tuple[list[DiscoveredPlugin], list[DiscoveryProblem]]:
    """Describe every entry under the plugins directory, in name order."""
    found: list[DiscoveredPlugin] = []
    problems: list[DiscoveryProblem] = []
    if not directory.is_dir():
        return found, problems
    for entry in sorted(directory.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            found.append(discover_directory_plugin(entry))
        except PluginManifestError as error:
            problems.append(DiscoveryProblem("directory", str(entry), str(error)))
    return found, problems


def discover_plugins(directory: Path | None) -> tuple[list[DiscoveredPlugin], list[DiscoveryProblem]]:
    """Describe every plugin from both sources, entry points first.

    A name found twice keeps its first occurrence and reports the second, so an
    installed distribution is not shadowed by a directory of the same name.
    """
    found, problems = discover_entry_point_plugins()
    if directory is not None:
        from_directory, directory_problems = discover_directory_plugins(directory)
        found.extend(from_directory)
        problems.extend(directory_problems)
    seen: dict[str, DiscoveredPlugin] = {}
    unique: list[DiscoveredPlugin] = []
    for plugin in found:
        name = plugin.manifest.name
        if name in seen:
            first = seen[name]
            problems.append(
                DiscoveryProblem(
                    plugin.source,
                    str(plugin.install_dir or plugin.package_dir),
                    f"plugin {name!r} is already provided by {first.source} at {first.package_dir}",
                )
            )
            continue
        seen[name] = plugin
        unique.append(plugin)
    return unique, problems
