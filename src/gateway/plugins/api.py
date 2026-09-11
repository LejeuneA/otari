"""The plugin API: what a plugin may import from the gateway.

Everything here is kept stable within one :data:`PLUGIN_API_VERSION`. A plugin
that imports anything else from ``gateway`` is reaching into internals that
move without notice. A manifest names the version it was written against as
``plugin_api``; the loader refuses one that wants a newer version than this.

The names are grouped by what a plugin does with them:

* registration: ``PluginContext`` and ``PluginError``;
* routes: the FastAPI dependencies a plugin's own routes take, and the
  request-scoped session and config they hand out;
* traffic: the events and decisions a traffic observer sees and returns;
* backends: the protocols a guardrail, a tool, or a router backend implements;
* events: what a subscribed handler receives.
"""

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
from gateway.log_config import logger
from gateway.models.entities import APIKey
from gateway.models.plugins import PLUGIN_API_VERSION, PluginManifest
from gateway.plugins.events import EVENT_NAMES, Event
from gateway.plugins.guardrails import GuardrailBackend, GuardrailOutcome
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
    Turn,
)
from gateway.services._tool_loop import ToolBackend
from gateway.services.provider_kwargs import resolve_provider_selector
from gateway.services.routing.backends import RouterBackend, RoutingContext, RoutingDecision

__all__ = [
    "EVENT_NAMES",
    "PLUGIN_API_VERSION",
    "APIKey",
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
    "ToolBackend",
    "ToolCall",
    "ToolCallDecision",
    "ToolCallEvent",
    "ToolResult",
    "Turn",
    "get_config",
    "get_container",
    "get_current_identity",
    "get_db",
    "load_config",
    "logger",
    "require_deployment_operator",
    "resolve_provider_selector",
    "verify_api_key",
    "verify_api_key_or_master_key",
    "verify_master_key",
]
