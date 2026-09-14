"""The plugin API: what a plugin may import from the gateway.

Everything here is kept stable within one :data:`PLUGIN_API_VERSION`. A plugin
that imports anything else from ``gateway`` is reaching into internals that
move without notice. A manifest names the version it was written against as
``plugin_api``; the loader refuses one that wants a newer version than this.

The names are grouped by what a plugin does with them:

* registration: ``PluginContext`` and ``PluginError``;
* routes: the FastAPI dependencies a plugin's own routes take, the
  request-scoped session and config they hand out, and ``get_caller`` for who
  is asking;
* data: ``create_session`` for a session outside a request (a startup hook, a
  background task) and ``run_plugin_env`` for the plugin's Alembic ``env.py``;
* traffic: the events and decisions a traffic observer sees and returns;
* backends: the protocols a guardrail, a tool, or a router backend implements;
* events: what a subscribed handler receives.

``PLUGIN_API_MIN_VERSION`` is the oldest ``plugin_api`` this gateway still
loads. The test harness for plugin authors is :mod:`gateway.plugins.testing`.
"""

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from gateway.api.deps import (
    get_config,
    get_container,
    get_current_identity,
    get_db,
    require_deployment_operator,
    verify_api_key,
    verify_api_key_or_master_key,
    verify_master_key,
)
from gateway.core.config import GatewayConfig, load_config
from gateway.core.database import create_session
from gateway.log_config import logger
from gateway.models.plugins import PLUGIN_API_MIN_VERSION, PLUGIN_API_VERSION, PluginManifest, RuntimeMode
from gateway.plugins.events import EVENT_NAMES, Event
from gateway.plugins.guardrails import GuardrailBackend, GuardrailOutcome
from gateway.plugins.migrations import run_plugin_env
from gateway.plugins.registry import PluginContext, PluginError
from gateway.plugins.traffic import (
    Caller,
    Conversation,
    RequestDecision,
    RequestEvent,
    ResponseDecision,
    ResponseEvent,
    ToolCall,
    ToolCallDecision,
    ToolCallEvent,
    ToolResult,
    ToolSpec,
    Turn,
)
from gateway.services._tool_loop import ToolBackend
from gateway.services.provider_kwargs import resolve_provider_selector
from gateway.services.routing.backends import RouterBackend, RoutingContext, RoutingDecision

if TYPE_CHECKING:
    from gateway.models.entities import APIKey

__all__ = [
    "EVENT_NAMES",
    "PLUGIN_API_MIN_VERSION",
    "PLUGIN_API_VERSION",
    "Caller",
    "Conversation",
    "Event",
    "GatewayConfig",
    "GuardrailBackend",
    "GuardrailOutcome",
    "PluginContext",
    "PluginError",
    "PluginManifest",
    "RequestDecision",
    "RequestEvent",
    "ResponseDecision",
    "ResponseEvent",
    "RouterBackend",
    "RoutingContext",
    "RoutingDecision",
    "RuntimeMode",
    "ToolBackend",
    "ToolCall",
    "ToolCallDecision",
    "ToolCallEvent",
    "ToolResult",
    "ToolSpec",
    "Turn",
    "create_session",
    "get_caller",
    "get_config",
    "get_container",
    "get_current_identity",
    "get_db",
    "load_config",
    "logger",
    "require_deployment_operator",
    "resolve_provider_selector",
    "run_plugin_env",
    "verify_api_key",
    "verify_api_key_or_master_key",
    "verify_master_key",
]


async def get_caller(
    auth: Annotated["tuple[APIKey | None, bool]", Depends(verify_api_key_or_master_key)],
) -> Caller:
    """Who is calling a plugin route, as ids: the API key's, or none at all for the master key.

    The same shape a traffic observer sees, so a plugin keys its own rows the
    same way in both places. Takes the place of the key row itself, which is
    the gateway's own table and moves without notice.
    """
    api_key, _is_master = auth
    if api_key is None:
        return Caller(api_key_id=None, user_id=None, workspace_id=None, organization_id=None)
    return Caller(
        api_key_id=api_key.id,
        user_id=api_key.user_id,
        workspace_id=str(api_key.workspace_id) if api_key.workspace_id else None,
        organization_id=None,
    )
