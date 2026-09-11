# Plugins

A plugin adds a feature to a running Otari without a change to Otari itself:
API routes, `otari` command groups, database tables with their own migrations,
and a page in the dashboard. Otari's first plugin, Agent Gates, checks whether a
coding agent's turn followed a repository's stated rules; it lives in its own
repository and is installed like any other.

A plugin runs inside the gateway process with everything the gateway can reach:
provider credentials, the database, every request. Treat installing one the way
you would treat installing a package into the gateway's environment, because
that is what it is. The dashboard says so before every install, and installing
through the API is off until an operator turns it on in `config.yml`.

## Installing a plugin

Three ways, all ending in the same place: a plugin the gateway discovers at
startup.

**From the dashboard.** The Marketplace page lists plugins mozilla.ai verifies
and plugins anyone has tagged on GitHub with the `otari-plugin` topic. Installing
one downloads the repository archive into the plugins directory. This needs
`plugins.allow_install: true` (or `OTARI_PLUGINS_ALLOW_INSTALL=true`) and a
restart afterwards. The same page uploads a `.zip` or `.tar.gz` you built
yourself.

**From the command line**, on the machine running the gateway:

```bash
otari plugins install njbrake/warden                # a GitHub repository, default branch
otari plugins install njbrake/warden --ref v0.1.0
otari plugins install ./otari-warden.zip             # a local archive
otari plugins list
otari plugins remove agent-gates
```

`install` writes into the plugins directory and needs no `allow_install`
setting: whoever runs it already has the gateway's filesystem. For a gateway
you reach over HTTP, the upload is one request:

```bash
curl --fail-with-body -X POST "$OTARI_URL/api/v1/plugins/upload" \
  -H "Authorization: Bearer $OTARI_MASTER_KEY" -F "file=@./otari-warden.zip"
```

**As a Python distribution**, for an image you build yourself:

```bash
uv pip install otari-agent-gates
```

A distribution registers itself through the `otari.plugins` entry-point group,
where the entry point's value names the package (`agent-gates =
"otari_agent_gates"`), and ships `otari-plugin.toml` as package data so the
manifest is beside the code once installed. The dashboard lists it with source
`entry_point` and cannot remove it; `uv pip uninstall` does.

A plugin takes effect on the next start, whichever way it arrived. The
Marketplace page says when a restart is owed; it reads that from the plugins
directory rather than from memory, so every worker of a deployment answers the
same after one of them took an install. A plugin that is running keeps every
contribution until that restart, including when a newer version is installed
over it or its directory is removed: the listing names what is waiting.

An install records where the plugin came from (`otari-install.json` beside the
tree: the repository or `upload`, the ref, and the time), which the listing
shows. An archive whose version is older than the installed one is refused,
because its migration chain may not know the revisions the newer version ran;
`--force` (or `force` on the API) installs it anyway. Removing a plugin leaves
its tables in place, so a reinstall picks up where the chain left off;
`otari plugins remove <name> --drop-tables` runs the chain back to base and
drops its version table too.

## Configuration

```yaml
plugins:
  enabled: true                  # off, nothing is discovered or imported (OTARI_PLUGINS_ENABLED)
  directory: ./otari-plugins     # drop-in plugins; where install and upload write (OTARI_PLUGINS_DIR)
  allow_install: false           # let the API and dashboard install (OTARI_PLUGINS_ALLOW_INSTALL)
  disabled: []                   # discovered plugins to leave unloaded
  marketplace:
    verified_index_url: https://raw.githubusercontent.com/mozilla-ai/otari-plugins/main/index.json
    github_topic: otari-plugin
    github_token: null           # raises the GitHub search rate limit
  observer_timeout_ms: 250       # budget per traffic-observer call
  event_timeout_ms: 5000         # budget per event handler, off the request path
  hook_timeout_ms: 30000         # budget per startup, shutdown, or health hook
  agent-gates:                   # a plugin's own settings, under its name
    judge_timeout_seconds: 120
```

Everything under `plugins:` that is not one of the settings above is a
plugin's own block. The keys a plugin types in its manifest are checked
against it and rendered as a form on the plugin's card in the Marketplace,
where an operator can change them without a restart; a value set there wins
over `config.yml`, the way the gateway's own runtime overrides do. Untyped keys
are handed to the plugin as they are, and the plugin documents them.

A plugin loads in the modes its manifest names and is listed as disabled in
the others. Migrations run only where the gateway has a database of its own,
which excludes hybrid mode.

## Writing a plugin

A plugin is a Python package holding an `otari-plugin.toml` and a
`register(ctx)` function. The manifest is read before any plugin code runs,
which is how the installer and the marketplace describe a plugin without
importing it.

```
otari-agent-gates/
  pyproject.toml
  src/otari_agent_gates/
    otari-plugin.toml
    __init__.py            # register(ctx)
    routes.py
    cli.py
    models.py              # the plugin's own SQLAlchemy Base
    migrations/            # an Alembic script directory
      env.py
      versions/
    static/                # the built dashboard page, index.html and assets
```

```toml
# src/otari_agent_gates/otari-plugin.toml
[plugin]
name = "agent-gates"              # letters, digits, hyphens, up to 47; the URL segment the plugin mounts at
version = "0.1.0"
description = "Checks a coding agent's turn against a repository's stated rules."
package = "otari_agent_gates"     # the importable package; the manifest sits inside it
plugin_api = 1                    # the version of gateway.plugins.api it is written against
modes = ["standalone"]            # standalone, hosted, hybrid; loaded in these only
homepage = "https://github.com/mozilla-ai/otari-agent-gates"
getting_started = "https://github.com/mozilla-ai/otari-agent-gates#quick-start"
min_otari_version = "0.30.0"      # optional; an older gateway refuses to load the plugin
contributes = ["routes", "cli", "migrations", "ui", "traffic"]
config_keys = ["traffic"]         # keys the plugin reads from its config block beyond the typed ones

[plugin.settings.judge_timeout_seconds]   # typed keys of the plugins.agent-gates block
type = "int"                      # str, int, float, bool, list, object
default = 120
description = "Cap on one judge call."
# secret = true                   # masked in the dashboard, never returned
# editable = false                # config.yml only

[[plugin.pages]]                  # dashboard pages; [plugin.ui] with path and label is the one-page short form
id = "runs"
label = "Agent gates"
path = "static"                   # directory relative to the package
section = "extend"                # observe, build, access, extend, or none (no sidebar row)
# parent = "tools"                # nest under a rail item that has children: tools or routing
icon = "shield"
order = 100
audience = "operator"             # or member: every signed-in session sees the row
# entry = "#/runs"                # appended to the page URL, so pages can share one bundle
```

`contributes` is the plugin's own account of what it adds, in a closed
vocabulary: `routes`, `cli`, `migrations`, `ui`, `traffic`, `lifecycle`,
`events`, `guardrails`, `tools`, `routing`. It is read before any code runs,
so the Marketplace can say what an install will do, and it is enforced when
the plugin loads: a plugin that registers something it did not declare is
refused with the reason. It is a label, not a boundary: a loaded plugin is
Python in the gateway's process, and the dashboard says so before an install.

`plugin_api` names the contract the plugin is written against. The gateway
provides one version (`gateway.plugins.api.PLUGIN_API_VERSION`) and refuses a
plugin that wants a newer one, so a plugin fails at install time with a clear
reason rather than at runtime with an import error. `modes` keeps a plugin out
of a runtime it was not written for: one that resolves provider credentials
locally has nothing to do on a hybrid gateway, and is listed as disabled there.

The manifest is read leniently, so a plugin written for a newer gateway still
describes itself on an older one: a key the gateway does not know is ignored,
and a `contributes` kind it does not know is kept and shown. What the gateway
cannot honor it refuses before importing the plugin, and says so in the
install dialog and on the installed row: a `min_otari_version` above its own,
or a contribution kind it does not know. A plugin that needs a gateway
feature should therefore name the version that introduced it rather than
probe for it at load.

`GET /api/v1/plugins/marketplace/describe?repo=owner/name` reads a
repository's manifest without downloading the plugin, which is what the
install dialog shows.

### What a plugin may import

Import from `gateway.plugins.api` and nothing else under `gateway`. That module
re-exports the whole plugin contract: `PluginContext`, the FastAPI dependencies
a plugin's routes take (`get_db`, `get_config`, `require_deployment_operator`,
`verify_api_key_or_master_key`, and the rest), the traffic events and
decisions, the backend protocols, and `Event`. Everything else in `gateway` is
internal and moves without notice; what `gateway.plugins.api` exports is kept
stable within one `plugin_api` version.

```python
# src/otari_agent_gates/__init__.py
from pathlib import Path

from gateway.plugins.api import PluginContext

from .cli import policy
from .routes import router


def register(ctx: PluginContext) -> None:
    settings = ctx.config                       # the plugins.agent-gates block over the manifest defaults
    ctx.add_router(router, auth="api_key")      # served under /api/v1/plugins/agent-gates, to API keys
    ctx.add_cli(policy)                         # `otari policy ...`
    ctx.add_migrations(Path(__file__).parent / "migrations")
```

`ctx.config` is the plugin's block of `config.yml` laid over the defaults its
manifest declares, type-checked against the manifest (a wrong type fails the
load). It is the live dict: when an operator changes a setting from the
dashboard the same dict is updated in place and every `ctx.on_settings_change`
listener runs, so a plugin that reads `ctx.config` at use time needs nothing
more. Stored dashboard values win over `config.yml` the way the gateway's own
runtime overrides do.

### What a plugin can add

- **Routes** mount under `/api/v1/plugins/<name>`, plus the router's own
  prefix. The mount applies the credential `auth` names to every route on the
  router: `"operator"` (the default) is a deployment operator, as the gateway's
  own management routers require; `"session"` is any signed-in dashboard session
  or the master key; `"api_key"` is an API key or the master key, for a route a
  client program calls; `"none"` mounts it open, for a route that authenticates
  in a way of its own. A route may still add a dependency of its own the way Otari's
  routers do, with `verify_master_key`, `require_deployment_operator`, or
  `verify_api_key_or_master_key` from `gateway.api.deps`.
- **CLI groups** attach to `otari` at the top level, under the group's own
  name. A name that collides with a built-in command is skipped and logged.
- **Migrations** are an Alembic script directory. The plugin's `env.py` is two
  lines, and the chain stamps `alembic_version_<name>` rather than Otari's
  `alembic_version`, so the two chains never see each other:

  ```python
  from alembic import context

  from gateway.plugins.migrations import run_plugin_env
  from otari_agent_gates.models import Base

  run_plugin_env(context, Base.metadata, "agent-gates")
  ```

  Declare tables on a `Base` of the plugin's own, not on `gateway.models`'s:
  Otari's autogenerate compares its metadata against the database and would
  otherwise propose dropping the plugin's tables. Migrations run after Otari's
  own, on startup when `auto_migrate` is on and from `otari migrate`, so a
  plugin table may reference a core one.
- **Pages** are directories of static files with an `index.html`. The first
  page is served at `/plugins/<name>/ui/` and every page at
  `/plugins/<name>/ui/<id>/`; the dashboard frames each at
  `/plugins/<name>` and `/plugins/<name>/<id>`, on the same origin, so the
  page calls the plugin's routes with the session cookie and needs no token
  handling of its own. The manifest says where the row goes (`section`,
  `parent`, `order`, `icon`) and who sees it (`audience`); `section = "none"`
  ships a page with no row, reachable from the plugin's card. Build it with
  whatever you like; hash-based client-side routing avoids needing server
  rewrites under the static mount.

  To look like the rest of the dashboard, link the dashboard's own stylesheet
  rather than bundling a theme: `<link rel="stylesheet" href="/dashboard.css">`
  serves the current build's CSS, which carries the semantic tokens
  (`--color-surface`, `--color-muted`, and so on), the HeroUI component styles,
  and the self-hosted fonts. The dashboard talks to the frame with
  `postMessage`: it sends `{type: "otari:theme", theme: "light" | "dark"}` on
  load and on every change, and the page may send `{type: "otari:navigate",
  to: "/keys"}` to move the dashboard, or `{type: "otari:toast", title,
  description?, variant?: "success" | "danger"}` to show a notice. The
  dashboard frames the page under its own title, so the page should not repeat
  a title or a sidebar of its own.
- **A traffic observer** watches inference requests as they pass through the
  gateway and can block, steer, or deny. See [Watching traffic](#watching-traffic).
- **Lifecycle hooks.** `ctx.on_startup(fn)` runs after Otari's and the
  plugin's migrations and after stored settings are applied, with an event
  loop, which is where a client pool or a background task belongs;
  `ctx.on_shutdown(fn)` runs before the database engine is disposed. A
  startup hook that raises marks the plugin failed (its routes stay mounted
  until the next start). `ctx.add_health_check(fn, critical=False)` reports
  into `/health` under the plugin's name; a `critical` check that fails takes
  `/health/readiness` to 503. Every hook may be sync or async and is cut off
  at `plugins.hook_timeout_ms`.
- **Events.** `ctx.subscribe(name, handler)` runs `handler(event)` for a
  gateway event: `usage.logged` (one usage row: model, provider, status, cost,
  tokens, annotations), `budget.exceeded` (user, subject, axis),
  `key.created`, `key.deleted`, and `plugin.settings_changed`; `"*"` gets them
  all. Handlers run as tasks off the request path, each cut off at
  `plugins.event_timeout_ms` and fenced by a `try`, so a notifier that is slow
  or broken never delays or fails a response. `event.payload` carries ids and
  numbers, never request or response content.
- **Guardrail backends.** `ctx.add_guardrail(name, backend)` offers a profile
  named `<plugin>:<name>` that a request, an organization entry, or a routing
  policy names like any service profile, and that gets block or monitor,
  fail-open or fail-closed, the mandate merge, and the result header for
  free. `backend.check(text, direction=, kwargs=)` is async and returns a
  `GuardrailOutcome(valid, explanation, score)`; a raise is the guardrail
  being unevaluable, handled by the entry's `mode` and `on_unavailable`.
- **Tools.** `ctx.add_tool(name, factory)` offers a tool the gateway runs for
  the model. `factory()` is called per request and returns an object with the
  `ToolBackend` members (`openai_tools`, `owns_tool`, `call_tool`,
  `purpose_hints`), optionally an async context manager. A request opts in with
  a tool entry `{"type": "plugin", "name": "<plugin>:<name>"}`, and the
  gateway's tool loop drives it the way it drives web search and code
  execution, which a request cannot combine with in one call.
- **Router backends.** `ctx.add_router_backend(name, backend)` offers a
  strategy a routing policy selects with `backend: <plugin>:<name>`.
  `backend.rank(ctx)` returns a `RoutingDecision`, or declines to let the
  policy's default serve.
- **`ctx.container`** is the composition root, for a plugin that needs a port.
  It is `None` when plugins are loaded for the command line alone.

A plugin that raises during import, `register`, or its own migrations is
listed as failed with its error, and the gateway boots without it: its routes
answer 503 until the next start. Otari never fails to start over a plugin.
Removing a plugin deletes its code, not its tables: the plugin's chain is never
downgraded, so its tables and `alembic_version_<name>` stay until you drop them.

For a plugin installed from a directory, the directory holding the package is
put on `sys.path`. A src layout (`src/<package>/`) and a flat layout
(`<package>/`) both work, with or without the one extra directory a GitHub
archive nests everything under.

## Watching traffic

An agent that talks to its model through Otari puts its whole conversation on
the wire: every tool call it made and every result it got back arrive in the
next request's messages, and the model's next tool call leaves in the response.
A plugin can watch that, and act on it, without installing anything on the
client:

```python
from gateway.plugins.api import RequestDecision, ResponseDecision, ToolCallDecision


class Watcher:
    def on_request(self, event):
        # event.caller: api_key_id, user_id, workspace_id, organization_id
        # event.conversation: api, model, system, turns, session_key, latest_user_text
        last = event.conversation.last_turn
        ran = [call.arguments.get("command") for call in last.tool_calls] if last else []
        return RequestDecision(inject_system="Never force-push.", annotations={"ran": ran})

    async def on_tool_call(self, event):
        if event.tool_call.name == "Bash" and "--force" in event.tool_call.arguments.get("command", ""):
            return ToolCallDecision(deny="Never force-push.", annotations={"fired": ["no-force-push"]})
        return None

    def on_response(self, event):
        # event.text: the answer's text; event.tool_calls: the calls that survived; event.streamed
        return ResponseDecision(annotations={"length": len(event.text)})


def register(ctx):
    ctx.add_traffic_observer(Watcher())
```

The shapes are provider-neutral: a `Conversation` is a system prompt, the
`tools` the request declared (`ToolSpec`: name, description, parameters), and
a list of assistant `Turn`s, each with the `user_text` that prompted it, the
`ToolCall`s the model made, and the `ToolResult`s the client returned,
whichever of the chat, messages, or responses APIs carried them.
`session_key` is the request's `session_label`, Claude Code's metadata user
id, or the `Otari-Conversation-Id` header, and a digest of the API key, the
system prompt, and the first user turn when none is present.

`on_request` runs before dispatch, after the input guardrails. `on_tool_call`
runs for each tool call in the model's response, once the call is whole: a
stream keeps flowing while a call's fragments are collected, then waits while
the observers answer before the completing chunk is sent on. Only calls the
client will run are offered; a tool the gateway runs itself (`otari_*` tools,
MCP servers) is settled inside the tool loop and never reaches the response.
`on_response` runs once the answer is complete. Every method may be sync or
async, and any may be omitted.

Decisions apply. A request `block` answers 403 with the message and never
calls the provider. `inject_system` is prepended to the system text of the
provider call, in whichever shape the API carries it. A tool call `deny`
removes the call from the response and puts `[Otari refused this tool call:
<message>]` where it was; in a stream the call's fragments are held back until
it is whole and judged, while the text around it keeps flowing. A response
`block` withholds a non-streamed answer with a 403 after the usage row is
written, since the provider was called; for a streamed answer, whose bytes are
gone, it is recorded as `would_block`. `annotations` from every observer are
merged under the plugin's name into the usage row's `plugin_annotations`
column, alongside what the seam itself records (`denied`, `blocked`,
`injected_system`: keys a plugin cannot set itself), and read back through
the usage API and the Activity page. Annotations must be JSON and stay under
16 KiB per plugin per request; a batch that is not JSON, or that would take
the plugin's annotations over the cap, is logged and left off the row while
what was recorded before it stays. A request that fails, or a stream the
client abandons after tool work, carries them on its error row too. Hybrid
mode asks every hook the same way but writes no local usage row, so nothing
is recorded there.

Observers are fenced. One that raises is logged and skipped, one that runs
past `plugins.observer_timeout_ms` (default 250) is abandoned and skipped, and
nothing runs at all when no plugin registered one. An async observer runs on
the event loop and is cancelled at the budget. A sync one runs in a worker
thread: the request stops waiting at the budget, the thread finishes on its
own, and the answer is discarded, so a sync observer that touches shared state
must be thread-safe. Either way a plugin can slow a request by that budget per
call, and no more.

## Getting listed

Tag the repository with the GitHub topic `otari-plugin` and it appears in the
Marketplace's community list on every gateway, marked unverified. The verified
list is a JSON index mozilla.ai maintains:

```json
{"plugins": [{"name": "agent-gates", "repo": "mozilla-ai/otari-agent-gates", "description": "...", "version": "0.1.0", "ref": "v0.1.0", "manifest": {"name": "agent-gates", "version": "0.1.0", "package": "otari_agent_gates", "contributes": ["routes", "cli", "migrations", "ui", "traffic"]}}]}
```

`ref` pins what an install fetches; without it, the default branch. `manifest`
is the plugin's own declaration, the `[plugin]` table of its `otari-plugin.toml`
as JSON; with it, the install dialog says what the plugin adds without reading
the repository.
