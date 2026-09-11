from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.api.deps import get_config, get_db_if_needed
from gateway.core.config import DEFAULT_PLATFORM_HEALTH_PATH, GatewayConfig
from gateway.log_config import logger
from gateway.plugins import PluginRegistry
from gateway.version import __version__

router = APIRouter(prefix="/health", tags=["health"])


async def _check_platform_reachability(config: GatewayConfig) -> bool:
    """Report whether the platform peer serves its health route.

    Only a 2xx answers that question. A 404 from a peer that does not serve the
    configured path, a 401 from an authenticated route, and a redirect to a login
    page all prove something is listening without proving it is the peer this
    gateway resolves credentials from. Redirects are left unfollowed for the same
    reason: the 200 at the end of one belongs to whatever it landed on.
    """
    platform_base_url = config.platform.get("base_url")
    if not platform_base_url:
        return False

    timeout_ms = int(config.platform.get("resolve_timeout_ms", 5000))

    # health_url, when set, names the peer's health route directly rather than
    # joining health_path onto base_url -- the only way to reach a health route
    # that does not live under base_url's own path (see its definition).
    health_url = config.platform.get("health_url")
    if not health_url:
        health_path = config.platform.get("health_path", DEFAULT_PLATFORM_HEALTH_PATH)
        health_url = f"{platform_base_url.rstrip('/')}/{health_path.lstrip('/')}"

    try:
        async with httpx.AsyncClient(timeout=timeout_ms / 1000, follow_redirects=False) as client:
            response = await client.get(health_url)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        logger.debug("Platform health probe to %s failed: %s", health_url, e)
        return False

    if not response.is_success:
        logger.debug("Platform health probe to %s answered %s", health_url, response.status_code)
        return False
    return True


async def _plugin_health(request: Request) -> tuple[dict[str, str], bool]:
    """Every loaded plugin's health checks, and whether a critical one failed."""
    registry: PluginRegistry | None = getattr(request.app.state, "plugins", None)
    if registry is None:
        return {}, False
    return await registry.check_health()


@router.get("")
async def health_check(request: Request, config: GatewayConfig = Depends(get_config)) -> dict[str, Any]:
    """General health check endpoint.

    Returns basic health status. For infrastructure monitoring,
    use /health/readiness or /health/liveness instead. ``plugins`` lists each
    loaded plugin that registered a health check, with what it reported.
    """
    payload: dict[str, Any] = {"status": "healthy"}
    if config.is_hybrid_mode:
        payload["mode"] = "hybrid"
        payload["platform_reachable"] = "yes" if await _check_platform_reachability(config) else "no"
    plugins, _ = await _plugin_health(request)
    if plugins:
        payload["plugins"] = plugins
    return payload


@router.get("/liveness")
async def health_liveness() -> str:
    """Liveness probe endpoint.

    Simple check to verify the process is alive and responding.
    Used by Kubernetes/container orchestrators for liveness probes.

    Returns:
        Plain text "I'm alive!" message

    """
    return "I'm alive!"


@router.get("/readiness")
async def health_readiness(
    request: Request,
    config: GatewayConfig = Depends(get_config),
    db: Annotated[AsyncSession | None, Depends(get_db_if_needed)] = None,
) -> dict[str, Any]:
    """Readiness probe endpoint.

    Checks if the gateway is ready to serve requests by validating:
    - Database connectivity
    - Service availability

    Used by Kubernetes/container orchestrators for readiness probes.
    Returns HTTP 503 if any dependency is unavailable.

    Returns:
        dict: Status object with health details

    Raises:
        HTTPException: 503 if service is not ready

    """
    if config.is_hybrid_mode:
        platform_reachable = await _check_platform_reachability(config)
        if not platform_reachable:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "status": "unhealthy",
                    "mode": "hybrid",
                    "platform": "unavailable",
                    "version": __version__,
                },
            )
        return await _with_plugins(
            request,
            {
                "status": "healthy",
                "mode": "hybrid",
                "platform": "connected",
                "version": __version__,
            },
        )

    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unhealthy", "database": "unavailable", "version": __version__},
        )

    try:
        await db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        logger.error("Database connectivity check failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "unhealthy",
                "database": "unavailable",
                "version": __version__,
            },
        ) from e
    return await _with_plugins(
        request,
        {
            "status": "healthy",
            "database": db_status,
            "version": __version__,
        },
    )


async def _with_plugins(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Add the plugins' health to a ready payload, or refuse readiness when a critical check fails."""
    plugins, critical_failure = await _plugin_health(request)
    if plugins:
        payload["plugins"] = plugins
    if critical_failure:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={**payload, "status": "unhealthy"},
        )
    return payload
