"""Describe a plugin on GitHub before anything is downloaded or run.

Reads the repository tree to find its ``otari-plugin.toml``, then the file
itself, both through GitHub's public API and the raw file host. What comes
back is the same manifest the installer validates, so the install dialog can
say what the plugin adds from the plugin's own declaration.
"""

import time

import httpx

from gateway.models.plugins import MANIFEST_FILENAME, PluginManifest, PluginManifestError, parse_manifest
from gateway.plugins.archive import GITHUB_REF_PATTERN, GITHUB_REPO_PATTERN, PluginInstallError

CACHE_TTL_SECONDS = 600.0
FETCH_TIMEOUT_SECONDS = 10.0
_cache: dict[tuple[str, str], tuple[float, PluginManifest]] = {}


def _check_names(repo: str, ref: str | None) -> str:
    if not GITHUB_REPO_PATTERN.fullmatch(repo) or repo.startswith(".") or "/." in repo:
        msg = f"not a GitHub repository name (owner/name): {repo!r}"
        raise PluginInstallError(msg)
    target = ref or "HEAD"
    if not GITHUB_REF_PATTERN.fullmatch(target) or ".." in target:
        msg = f"not a usable git ref: {ref!r}"
        raise PluginInstallError(msg)
    return target


async def describe_github_plugin(
    repo: str,
    ref: str | None,
    *,
    token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PluginManifest:
    """The manifest a GitHub repository ships, without installing anything.

    Raises:
        PluginInstallError: If the repository or ref cannot be read, holds no
            manifest, or holds more than one.
        PluginManifestError: If the manifest is invalid.

    """
    target = _check_names(repo, ref)
    key = (repo, target)
    cached = _cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True, transport=transport) as client:
        try:
            tree = await client.get(
                f"https://api.github.com/repos/{repo}/git/trees/{target}", params={"recursive": "1"}, headers=headers
            )
        except httpx.HTTPError as error:
            msg = f"could not read {repo}: {error}"
            raise PluginInstallError(msg) from error
        if tree.status_code == 404:
            msg = f"GitHub has no repository {repo} at {ref or 'its default branch'}"
            raise PluginInstallError(msg)
        if tree.status_code != 200:
            msg = f"GitHub answered {tree.status_code} reading {repo}"
            raise PluginInstallError(msg)
        entries = tree.json().get("tree") if isinstance(tree.json(), dict) else None
        paths = [
            str(entry["path"])
            for entry in (entries or [])
            if isinstance(entry, dict) and str(entry.get("path", "")).endswith(f"/{MANIFEST_FILENAME}")
        ]
        if not paths:
            msg = f"{repo} holds no {MANIFEST_FILENAME}"
            raise PluginInstallError(msg)
        if len(paths) > 1:
            msg = f"{repo} holds more than one {MANIFEST_FILENAME}: {', '.join(paths)}"
            raise PluginInstallError(msg)
        try:
            raw = await client.get(f"https://raw.githubusercontent.com/{repo}/{target}/{paths[0]}")
        except httpx.HTTPError as error:
            msg = f"could not read {paths[0]} from {repo}: {error}"
            raise PluginInstallError(msg) from error
        if raw.status_code != 200:
            msg = f"GitHub answered {raw.status_code} reading {paths[0]} from {repo}"
            raise PluginInstallError(msg)
    try:
        manifest = parse_manifest(raw.text)
    except PluginManifestError:
        raise
    _cache[key] = (time.monotonic(), manifest)
    return manifest
