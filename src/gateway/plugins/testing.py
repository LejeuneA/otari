"""A harness for a plugin's own test suite.

Loads one plugin the way the gateway does, without a gateway: the manifest is
parsed, ``register`` runs against a real ``PluginContext``, and what it
contributed is on the returned :class:`LoadedPlugin`. The traffic and event
seams can then be driven directly::

    from gateway.plugins.testing import load_plugin, observe

    plugin = load_plugin(Path(__file__).parent.parent / "src" / "my_plugin", settings={"strict": True})
    assert plugin.status == "loaded", plugin.error

    hooks = await observe(plugin, chat(messages=[{"role": "user", "content": "git push --force"}]))
    assert hooks.denied == {}
    denial = await hooks.tool_call(ToolCall("c1", "Bash", {"command": "git push --force"}))
    assert denial == "Never force-push."

Nothing here needs a database or a network.
"""

from pathlib import Path
from typing import Any

from gateway.models.plugins import RuntimeMode
from gateway.plugins.discovery import DiscoveredPlugin, find_manifest_in_tree, read_manifest
from gateway.plugins.events import Event
from gateway.plugins.registry import LoadedPlugin, _load_one
from gateway.plugins.traffic import (
    Caller,
    Conversation,
    TrafficHooks,
    TrafficObservers,
    conversation_from_chat,
    conversation_from_messages,
    conversation_from_responses,
)

__all__ = ["Event", "chat", "load_plugin", "messages", "observe", "responses"]

ANONYMOUS = Caller(api_key_id="test-key", user_id="test-user", workspace_id=None, organization_id=None)


def load_plugin(
    package_dir: Path | str,
    *,
    settings: dict[str, Any] | None = None,
    mode: RuntimeMode = "standalone",
) -> LoadedPlugin:
    """Load the plugin whose ``otari-plugin.toml`` is in ``package_dir`` (or one level below it).

    ``settings`` is the block ``config.yml`` would hold under the plugin's
    name. A plugin that fails to load comes back with ``status == "failed"``
    and the reason in ``error``, as the gateway would list it.
    """
    root = Path(package_dir).resolve()
    manifest_path = root / "otari-plugin.toml"
    if not manifest_path.is_file():
        manifest_path = find_manifest_in_tree(root)
    manifest = read_manifest(manifest_path)
    discovered = DiscoveredPlugin(
        manifest=manifest, source="directory", package_dir=manifest_path.parent, install_dir=root
    )
    return _load_one(discovered, dict(settings or {}), None, mode)


async def observe(plugin: LoadedPlugin, conversation: Conversation, *, caller: Caller = ANONYMOUS) -> TrafficHooks:
    """Show ``conversation`` to the plugin's traffic observers, as the gateway does before dispatch.

    The returned hooks carry what the observers decided (``blocked``,
    ``injected_system``, ``annotations``); ask ``tool_call`` and ``response``
    on them to drive the rest of a request.
    """
    observers = TrafficObservers((plugin.name, observer) for observer in plugin.observers)
    hooks = TrafficHooks(observers, caller, conversation)
    await hooks.request()
    return hooks


def chat(
    messages: list[Any], *, model: str = "test-model", session: str = "", tools: list[Any] | None = None
) -> Conversation:
    """A chat-shaped request, as an observer would see it."""
    return conversation_from_chat(model, messages, session=session, tools=tools)


def messages(
    messages: list[Any],
    *,
    system: Any = None,
    model: str = "test-model",
    session: str = "",
    tools: list[Any] | None = None,
) -> Conversation:
    """A messages-shaped request, as an observer would see it."""
    return conversation_from_messages(model, system, messages, session=session, tools=tools)


def responses(
    input_data: Any,
    *,
    instructions: Any = None,
    model: str = "test-model",
    session: str = "",
    tools: list[Any] | None = None,
) -> Conversation:
    """A responses-shaped request, as an observer would see it."""
    return conversation_from_responses(model, instructions, input_data, session=session, tools=tools)
