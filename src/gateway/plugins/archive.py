"""Install a plugin from an archive into the plugins directory, and remove one.

An archive is a zip or a gzipped tar holding a plugin tree (a flat or src
layout, possibly under one top-level directory the way a GitHub archive is
packed). It is unpacked into a scratch directory beside the target, checked for
exactly one manifest, and only then moved into place under the plugin's name,
replacing what was there. Nothing here imports the plugin: it takes effect on
the next start.
"""

import io
import re
import shutil
import tarfile
import tempfile
import zipfile
import zlib
from pathlib import Path

import httpx

from gateway.models.plugins import PluginManifestError
from gateway.plugins.discovery import DiscoveredPlugin, discover_directory_plugin

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 20_000
GITHUB_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GITHUB_REF_PATTERN = re.compile(r"^[A-Za-z0-9_./-]{1,200}$")
# Where a GitHub source archive is served from: the page host redirects there.
GITHUB_ARCHIVE_HOSTS = frozenset({"github.com", "codeload.github.com"})


class PluginInstallError(Exception):
    """Raised when an archive cannot be installed as a plugin."""


def _safe_relative(name: str) -> Path | None:
    """The member path if it stays inside the extraction root, else ``None``."""
    path = Path(name)
    if path.is_absolute() or not path.parts or any(part in ("..", "") for part in path.parts):
        return None
    if path.parts[0].endswith(":"):
        return None
    return path


def _extract_zip(data: bytes, root: Path) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        msg = f"not a zip archive: {error}"
        raise PluginInstallError(msg) from error
    with archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            msg = f"archive holds more than {MAX_MEMBERS} entries"
            raise PluginInstallError(msg)
        total = sum(member.file_size for member in members)
        if total > MAX_UNPACKED_BYTES:
            msg = f"archive unpacks to more than {MAX_UNPACKED_BYTES} bytes"
            raise PluginInstallError(msg)
        for member in members:
            relative = _safe_relative(member.filename)
            if relative is None:
                msg = f"archive entry escapes the plugin directory: {member.filename!r}"
                raise PluginInstallError(msg)
            mode = (member.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                msg = f"archive entry is a symlink, which is not allowed: {member.filename!r}"
                raise PluginInstallError(msg)
            target = root / relative
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)


def _extract_tar(data: bytes, root: Path) -> None:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as error:
        msg = f"not a tar archive: {error}"
        raise PluginInstallError(msg) from error
    with archive:
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            msg = f"archive holds more than {MAX_MEMBERS} entries"
            raise PluginInstallError(msg)
        if sum(member.size for member in members) > MAX_UNPACKED_BYTES:
            msg = f"archive unpacks to more than {MAX_UNPACKED_BYTES} bytes"
            raise PluginInstallError(msg)
        for member in members:
            if _safe_relative(member.name) is None:
                msg = f"archive entry escapes the plugin directory: {member.name!r}"
                raise PluginInstallError(msg)
            if member.issym() or member.islnk():
                msg = f"archive entry is a link, which is not allowed: {member.name!r}"
                raise PluginInstallError(msg)
        try:
            # The data filter refuses anything the checks above missed.
            archive.extractall(root, filter="data")
        except tarfile.TarError as error:
            msg = f"archive could not be unpacked: {error}"
            raise PluginInstallError(msg) from error


def _extract(data: bytes, root: Path) -> None:
    try:
        if data[:4] == b"PK\x03\x04":
            _extract_zip(data, root)
        elif data[:2] == b"\x1f\x8b" or data[257:262] == b"ustar":
            _extract_tar(data, root)
        else:
            msg = "archive is neither a zip nor a gzipped tar"
            raise PluginInstallError(msg)
    except (zipfile.BadZipFile, tarfile.TarError, zlib.error, EOFError) as error:
        # A member that is truncated or fails its checksum surfaces here, after
        # the header checks above passed.
        msg = f"archive could not be unpacked: {error}"
        raise PluginInstallError(msg) from error


def _replace_directory(staged: Path, target: Path) -> None:
    """Move ``staged`` to ``target``, keeping the old tree until the new one is in place."""
    previous = target.with_name(f".{target.name}.previous")
    if previous.exists():
        shutil.rmtree(previous)
    if target.exists():
        target.rename(previous)
    try:
        staged.rename(target)
    except OSError:
        if previous.exists():
            previous.rename(target)
        raise
    if previous.exists():
        shutil.rmtree(previous, ignore_errors=True)


def install_archive(data: bytes, directory: Path) -> DiscoveredPlugin:
    """Unpack ``data`` into ``directory/<plugin name>`` and describe what landed.

    Raises:
        PluginInstallError: If the archive is too large, unsafe, unreadable, or
            does not hold exactly one valid plugin.

    """
    if len(data) > MAX_ARCHIVE_BYTES:
        msg = f"archive is larger than {MAX_ARCHIVE_BYTES} bytes"
        raise PluginInstallError(msg)
    if not data:
        msg = "archive is empty"
        raise PluginInstallError(msg)
    directory.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".staging-", dir=directory))
    try:
        _extract(data, staged)
        try:
            discovered = discover_directory_plugin(staged)
        except PluginManifestError as error:
            raise PluginInstallError(str(error)) from error
        target = directory / discovered.manifest.name
        _replace_directory(staged, target)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return discover_directory_plugin(target)


def remove_installed(install_dir: Path, directory: Path) -> None:
    """Delete a directory plugin, refusing anything outside the plugins directory."""
    resolved = install_dir.resolve()
    root = directory.resolve()
    if resolved == root or root not in resolved.parents:
        msg = f"{install_dir} is not inside the plugins directory {directory}"
        raise PluginInstallError(msg)
    if not resolved.is_dir():
        msg = f"{install_dir} is already gone; restart the gateway to unload the plugin"
        raise PluginInstallError(msg)
    shutil.rmtree(resolved)


def github_archive_url(repo: str, ref: str | None) -> str:
    """The zip GitHub serves for ``owner/name`` at ``ref`` (its default branch when unset)."""
    if not GITHUB_REPO_PATTERN.fullmatch(repo) or repo.startswith(".") or "/." in repo:
        msg = f"not a GitHub repository name (owner/name): {repo!r}"
        raise PluginInstallError(msg)
    target = ref or "HEAD"
    if not GITHUB_REF_PATTERN.fullmatch(target) or ".." in target:
        msg = f"not a usable git ref: {ref!r}"
        raise PluginInstallError(msg)
    return f"https://github.com/{repo}/archive/{target}.zip"


async def fetch_github_archive(repo: str, ref: str | None, *, timeout: float = 30.0) -> bytes:
    """Download a repository archive from GitHub, following only GitHub's own redirects.

    Raises:
        PluginInstallError: If the name is malformed, the download fails, or a
            redirect leaves GitHub.

    """
    url = github_archive_url(repo, ref)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _ in range(5):
                response = await client.get(url)
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    next_url = httpx.URL(location) if location.startswith("http") else response.url.join(location)
                    if next_url.scheme != "https" or next_url.host not in GITHUB_ARCHIVE_HOSTS:
                        msg = f"GitHub redirected the download off GitHub, to {next_url.host!r}"
                        raise PluginInstallError(msg)
                    url = str(next_url)
                    continue
                break
            else:
                msg = "too many redirects fetching the archive"
                raise PluginInstallError(msg)
    except httpx.HTTPError as error:
        msg = f"could not download {url}: {error}"
        raise PluginInstallError(msg) from error
    if response.status_code == 404:
        msg = f"GitHub has no archive for {repo} at {ref or 'its default branch'}"
        raise PluginInstallError(msg)
    if response.status_code != 200:
        msg = f"GitHub answered {response.status_code} for {url}"
        raise PluginInstallError(msg)
    if len(response.content) > MAX_ARCHIVE_BYTES:
        msg = f"archive is larger than {MAX_ARCHIVE_BYTES} bytes"
        raise PluginInstallError(msg)
    return response.content
