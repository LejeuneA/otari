"""Describe a plugin on GitHub before anything is downloaded or run.

Reads the repository tree to find its ``otari-plugin.toml``, then the file
itself, both through GitHub's public API and the raw file host. What comes
back is the same manifest the installer validates, so the install dialog can
say what the plugin adds from the plugin's own declaration, and the layouts
accepted are the ones an install would accept, so the dialog never describes
a plugin the install then refuses.
"""

import time
from pathlib import PurePosixPath
from typing import Any

import httpx

from gateway.models.plugins import MANIFEST_FILENAME, PluginManifest, parse_manifest
from gateway.plugins.archive import GITHUB_REF_PATTERN, GITHUB_REPO_PATTERN, PluginInstallError

CACHE_TTL_SECONDS = 600.0
FETCH_TIMEOUT_SECONDS = 10.0
# A redirect is followed only within GitHub's own hosts (a renamed repository
# redirects inside api.github.com); anywhere else is refused, as the archive
# fetch refuses it, so the gateway never reads a tree from a host a redirect named.
ALLOWED_HOSTS = frozenset({"api.github.com", "raw.githubusercontent.com"})
MAX_REDIRECTS = 3
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


def _manifest_paths(entries: list[Any]) -> list[str]:
    """The manifests in a repository tree that an install would find.

    The same layouts as ``discovery.find_manifest_in_tree``, seen from the
    repository root rather than from the directory a GitHub archive nests it
    under: the root itself, a flat ``<pkg>/``, or a ``src/<pkg>/``. A root
    manifest is refused later for sitting outside a package, as the installer
    refuses it, but it is counted here so that a tree holding two manifests is
    reported as the installer would report it. Anything deeper is invisible to
    the installer, so it is invisible here too.
    """
    paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        parts = PurePosixPath(str(entry.get("path", ""))).parts
        if parts[-1:] != (MANIFEST_FILENAME,):
            continue
        if len(parts) <= 2 or (len(parts) == 3 and parts[0] == "src"):
            paths.append("/".join(parts))
    return paths


async def _get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """GET ``url``, following a redirect only to one of GitHub's own hosts."""
    for _ in range(MAX_REDIRECTS + 1):
        response = await client.get(url, **kwargs)
        if not response.is_redirect or not response.headers.get("location"):
            return response
        next_url = response.url.join(response.headers["location"])
        if next_url.scheme != "https" or next_url.host not in ALLOWED_HOSTS:
            msg = f"GitHub redirected {url} off its own hosts, to {next_url.scheme}://{next_url.host}; refused"
            raise PluginInstallError(msg)
        url = str(next_url)
        kwargs.pop("params", None)
    msg = f"GitHub redirected {url} more than {MAX_REDIRECTS} times"
    raise PluginInstallError(msg)


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
            manifest where an install would look, or holds more than one.
        PluginManifestError: If the manifest is invalid.

    """
    target = _check_names(repo, ref)
    key = (repo, target)
    now = time.monotonic()
    for stale in [k for k, (at, _) in _cache.items() if now - at >= CACHE_TTL_SECONDS]:
        del _cache[stale]
    cached = _cache.get(key)
    if cached is not None:
        return cached[1]
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False, transport=transport) as client:
        try:
            tree = await _get(
                client,
                f"https://api.github.com/repos/{repo}/git/trees/{target}",
                params={"recursive": "1"},
                headers=headers,
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
        try:
            payload = tree.json()
        except ValueError as error:
            msg = f"GitHub's answer for {repo} is not JSON"
            raise PluginInstallError(msg) from error
        if not isinstance(payload, dict):
            msg = f"GitHub's answer for {repo} is not in the expected shape"
            raise PluginInstallError(msg)
        if payload.get("truncated"):
            # GitHub caps the listing; the manifest may be past the cap.
            msg = f"{repo} is too large for GitHub to list in full, so its {MANIFEST_FILENAME} cannot be found"
            raise PluginInstallError(msg)
        entries = payload.get("tree")
        paths = _manifest_paths(entries if isinstance(entries, list) else [])
        if len(paths) > 1:
            msg = f"{repo} holds more than one {MANIFEST_FILENAME}: {', '.join(paths)}"
            raise PluginInstallError(msg)
        if not paths or paths[0] == MANIFEST_FILENAME:
            msg = f"{repo} holds no {MANIFEST_FILENAME} where an install would look for one"
            raise PluginInstallError(msg)
        try:
            raw = await _get(client, f"https://raw.githubusercontent.com/{repo}/{target}/{paths[0]}")
        except httpx.HTTPError as error:
            msg = f"could not read {paths[0]} from {repo}: {error}"
            raise PluginInstallError(msg) from error
        if raw.status_code != 200:
            msg = f"GitHub answered {raw.status_code} reading {paths[0]} from {repo}"
            raise PluginInstallError(msg)
    manifest = parse_manifest(raw.text)
    package_dir = PurePosixPath(paths[0]).parent.name
    if "." in manifest.package or package_dir != manifest.package:
        msg = (
            f"{paths[0]} declares package {manifest.package!r} but sits in {package_dir!r}; "
            "a directory plugin's manifest lives inside its top-level package"
        )
        raise PluginInstallError(msg)
    _cache[key] = (time.monotonic(), manifest)
    return manifest
