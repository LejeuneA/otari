"""The marketplace listing: plugins mozilla.ai verifies, and plugins anyone tagged.

Two sources, fetched server-side and cached, because the GitHub search API is
rate limited hard for an unauthenticated caller and a browser tab would spend
that budget on every visit. The verified list is a JSON index mozilla.ai
maintains; the community list is a GitHub topic search, which anyone can join by
tagging a repository. A source that cannot be reached is reported in ``errors``
and the other still answers.
"""

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from gateway.log_config import logger
from gateway.models.plugins import MarketplaceConfig

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
CACHE_TTL_SECONDS = 600.0
FETCH_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class MarketplaceEntry:
    """One plugin the marketplace lists."""

    name: str
    repo: str
    description: str
    url: str
    verified: bool
    version: str | None = None
    stars: int | None = None
    ref: str | None = None
    updated_at: str | None = None


@dataclass
class MarketplaceListing:
    verified: list[MarketplaceEntry] = field(default_factory=list)
    community: list[MarketplaceEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fetched_at: float = 0.0


def _entry_from_index(item: dict[str, Any]) -> MarketplaceEntry | None:
    repo = item.get("repo")
    name = item.get("name")
    if not isinstance(repo, str) or not isinstance(name, str):
        return None
    return MarketplaceEntry(
        name=name,
        repo=repo,
        description=str(item.get("description") or ""),
        url=str(item.get("homepage") or f"https://github.com/{repo}"),
        verified=True,
        version=str(item["version"]) if item.get("version") else None,
        ref=str(item["ref"]) if item.get("ref") else None,
    )


def _entry_from_github(item: dict[str, Any], verified_repos: set[str]) -> MarketplaceEntry | None:
    repo = item.get("full_name")
    if not isinstance(repo, str):
        return None
    return MarketplaceEntry(
        name=str(item.get("name") or repo.split("/")[-1]),
        repo=repo,
        description=str(item.get("description") or ""),
        url=str(item.get("html_url") or f"https://github.com/{repo}"),
        verified=repo.lower() in verified_repos,
        stars=item.get("stargazers_count"),
        ref=str(item["default_branch"]) if item.get("default_branch") else None,
        updated_at=str(item["pushed_at"]) if item.get("pushed_at") else None,
    )


class Marketplace:
    """Fetches and caches the two lists for one process."""

    def __init__(
        self,
        config: MarketplaceConfig,
        ttl: float = CACHE_TTL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._ttl = ttl
        self._transport = transport
        self._cached: MarketplaceListing | None = None

    async def listing(self, *, refresh: bool = False) -> MarketplaceListing:
        cached = self._cached
        if cached is not None and not refresh and time.monotonic() - cached.fetched_at < self._ttl:
            return cached
        listing = MarketplaceListing(fetched_at=time.monotonic())
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True, transport=self._transport
        ) as client:
            await self._fill_verified(client, listing)
            await self._fill_community(client, listing)
        self._cached = listing
        return listing

    async def _fill_verified(self, client: httpx.AsyncClient, listing: MarketplaceListing) -> None:
        url = self._config.verified_index_url
        if not url:
            return
        try:
            response = await client.get(url)
            if response.status_code == 404:
                # No index published at that address yet: an empty verified
                # list, not a failure the dashboard has to explain on every visit.
                logger.info("Marketplace: no verified index is published at %s", url)
                return
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("Marketplace: verified index %s unavailable: %s", url, error)
            listing.errors.append("The verified plugin index could not be fetched.")
            return
        items = payload.get("plugins") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            listing.errors.append("The verified plugin index is not in the expected shape.")
            return
        listing.verified = [
            entry for entry in (_entry_from_index(item) for item in items if isinstance(item, dict)) if entry
        ]

    async def _fill_community(self, client: httpx.AsyncClient, listing: MarketplaceListing) -> None:
        topic = self._config.github_topic
        if not topic:
            return
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._config.github_token:
            headers["Authorization"] = f"Bearer {self._config.github_token}"
        params = {"q": f"topic:{topic}", "sort": "stars", "order": "desc", "per_page": "50"}
        try:
            response = await client.get(GITHUB_SEARCH_URL, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("Marketplace: GitHub topic search for %s unavailable: %s", topic, error)
            listing.errors.append(f"GitHub repositories tagged '{topic}' could not be fetched.")
            return
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            listing.errors.append("The GitHub search answer is not in the expected shape.")
            return
        verified_repos = {entry.repo.lower() for entry in listing.verified}
        entries = (_entry_from_github(item, verified_repos) for item in items if isinstance(item, dict))
        listing.community = [entry for entry in entries if entry and not entry.verified]
