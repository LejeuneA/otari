# Connections

A connection is a third-party account (Slack, GitHub, Google, Microsoft,
Notion, Linear, Atlassian, HubSpot, Typeform, Salesforce) that a user of
*your* application has authorized Otari to act on. You send the user to a
link; Otari runs the OAuth consent flow, stores the tokens encrypted, refreshes
them, and hands the user back to your page. From then on your application, or
anything Otari runs on its behalf, can act on that account without ever
touching an OAuth code.

This is the account-connection layer from Octonous, mozilla.ai's agent
product, brought into otari so that anyone building an agent on the gateway
gets it without writing OAuth. The protocol mechanics come from
[apron-auth](https://github.com/mozilla-ai/apron-auth); Otari adds the
deployment's part: which apps are configured, the pending state between the
two halves of a flow, the encrypted rows, and the identity of your users.

Standalone mode only.

## Who the user is

Your application talks to Otari with an API key and names its users with the
`user` string it already puts on completion requests. That string, scoped to
the API key's workspace, is what a connection belongs to. Two applications
naming a user `alice` never see each other's connections. Otari never needs to
know who alice is beyond that string.

## Configure the apps

Register an OAuth app with each provider, set its redirect URI to
`{public_base_url}/connected-accounts/{provider}/callback`, and put the client
credentials in `config.yml`:

```yaml
public_base_url: https://otari.example.com   # the redirect URI is derived from it

connected_apps:
  slack:
    client_id: ${SLACK_CLIENT_ID}
    client_secret: ${SLACK_CLIENT_SECRET}
    scopes: [chat:write, channels:join, im:write, reactions:write, users:read]   # bot scopes
    user_scopes: [channels:read, channels:history, chat:write, im:write, users:read, team:read]
  github:
    client_id: ${GITHUB_CLIENT_ID}
    client_secret: ${GITHUB_CLIENT_SECRET}
    scopes: [repo, read:org]
```

`scopes` (and, for Slack, `user_scopes`) are added to the provider preset's
base scopes, which cover identity so Otari can tell which account was
connected. Tokens are encrypted with `OTARI_SECRET_KEY`, so it must be set. An
app missing either credential, or a deployment without `public_base_url`, is
not offered: `GET /v1/connections/apps` does not list it and
`POST /{provider}/authorize` answers 400.

## Connect an account

```python
import httpx

otari = httpx.Client(base_url="https://otari.example.com", headers={"Authorization": f"Bearer {API_KEY}"})

# 1. Get a link and put it behind a "Connect Slack" button.
link = otari.post("/v1/connections/slack/authorize", json={
    "user": "alice@acme.com",
    "return_url": "https://myapp.com/settings",
}).json()["authorization_url"]

# 2. The user's browser visits the link, consents, and lands back on
#    https://myapp.com/settings?connection=ok&provider=slack&connection_id=…
#    (or ?connection=error&provider=slack&reason=access_denied).

# 3. Your settings page shows what alice has connected.
otari.get("/v1/connections", params={"user": "alice@acme.com"}).json()
# {"count": 1, "data": [{"provider": "slack", "account_label": "Alice (Acme Corp)", "scopes": [...], ...}]}
```

The link is good for ten minutes and one exchange. Otari records the state
with your user, the provider, the PKCE verifier (encrypted) and the return URL
when *you* start the flow, so the browser's callback carries nothing Otari has
to trust. `return_url` must be https, or http on localhost for development,
and is stored with the state rather than read back from the browser. Without
one, the browser lands on a page of this deployment.

### When you cannot see the browser come back

A popup the user closed, or an agent that asked for consent mid-run, leaves
you waiting without a redirect to tell you anything. `authorize` also returns
a `flow_id` for that:

```python
started = otari.post("/v1/connections/slack/authorize", json={"user": "alice@acme.com"}).json()
otari.get(f"/v1/connections/flows/{started['flow_id']}").json()
# {"status": "pending", ...}          the user is still at the provider
# {"status": "connected", "connection_id": "…"}
# {"status": "failed", "reason": "access_denied"}
# {"status": "expired", ...}          the link timed out unused
```

`flow_id` is not the OAuth state and cannot complete or replay the flow, so it
is safe to keep in your own page's URL or hand to a polling client. A resolved
flow stays answerable for an hour.

## More than one connection per app

Otari keys a grant by provider, but your product may not. If Gmail and Google
Drive are two things a user connects separately, pass a `key` of your choosing
and each becomes its own connection under one provider, with its own scopes,
its own account and its own disconnect:

```python
otari.post("/v1/connections/google/authorize", json={"user": u, "key": "mail", "scopes": [GMAIL_SCOPE]})
otari.post("/v1/connections/google/authorize", json={"user": u, "key": "drive", "scopes": [DRIVE_SCOPE]})
otari.get("/v1/connections/google/token", params={"user": u, "key": "drive"})
```

A user may also hold several accounts with the same app in the same partition
(two Slack workspaces). The token endpoint picks one; when your user has told
you which account an action is for, name it and Otari obeys:

```python
otari.get("/v1/connections/google/token", params={"user": u, "connection_id": chosen})
```

## Asking for more scopes later

Most providers replace a grant with whatever the authorization request names,
so asking for one new scope drops the ones the user already gave you. Say what
you want added and let Otari compute the union with what the connection holds:

```python
otari.post("/v1/connections/slack/authorize", json={
    "user": u, "scopes": ["files:read"], "scope_mode": "add",
})
```

`scope_mode: "exact"` (the default) asks for exactly your list, which is what
a scope *reduction* needs.

If the user picks a different account on the consent screen, an upgrade lands
somewhere else and the credential you resolve stays un-upgraded. Bind the flow
to the account you meant, and Otari refuses the mismatch instead of quietly
storing a second connection:

```python
otari.post("/v1/connections/slack/authorize", json={
    "user": u, "expected_account_identifier": current["account_identifier"],
})
# the browser returns with ?connection=error&reason=binding, and the flow says
# {"status": "failed", "reason": "binding_mismatch"}
```

## Credentials a whole team shares

`"shared": true` on `authorize` stores the grant for the workspace instead of
the person: one admin connects the team's Slack, and every user of your
application in that workspace resolves it. Consent still comes from a person,
and the connection records who gave it (`connected_by`).

The token endpoint resolves a user's own connection first and falls back to the
shared one, so "my account if I have one, the team's otherwise" needs no
branching from you. `GET /v1/connections?user=` lists both, each with an
`owner` of `user` or `workspace`; pass `include_shared=false` to see only what
the user connected themselves.

## Use a connection

| Call | Does |
|---|---|
| `GET /v1/connections/apps?user=` | Apps this deployment can connect, the scopes each asks for with labels, and how many accounts the user holds per app: one query, however many apps. |
| `GET /v1/connections?user=&provider=&key=&include_shared=` | Everything the user resolves: their own connections and the workspace's shared ones, across every app. Tokens never appear. |
| `GET /v1/connections/{provider}/token?user=&key=&connection_id=` | The live credential, refreshed first when it expires within a minute, with the account it belongs to and the scopes each token holds. For code that calls the app itself. |
| `GET /v1/connections/flows/{flow_id}` | How a flow you started ended. |
| `POST /v1/connections/{id}/rejected?user=` | Tell Otari the provider refused this credential (see below). |
| `PATCH /v1/connections/{id}?user=` | Set the user's label ("Work Slack"). |
| `DELETE /v1/connections/{id}?user=` | Revoke at the provider where supported, then forget the tokens. |
| `DELETE /v1/connections/user?user=` | Forget every connection this user has, revoking each grant first. |

The token endpoint is the one place a credential leaves Otari, and it goes
only to the application whose users these are, which is also the application
that registered the OAuth client. In-process consumers use
`ConnectedAccountService.access_token_for_provider(workspace_id, user, provider, key=…)`.

What comes back carries enough to build a provider client in one answer: the
primary token and its scopes, the secondary tokens next to it (Slack's user
token under `extra.user`) with *their* scopes under `extra_scopes.user`, the
account identifier and label, and what the provider says about the account.
A Slack workspace id and name live in `account_metadata.tenancy_id` and
`.tenancy_name`.

## When a credential dies

A stored token can be dead while its recorded expiry is still in the future:
the user revoked the grant, or the provider rotated it. Nothing Otari can see
says so, so tell it when a call to the app comes back 401 or 403:

```python
result = otari.post(f"/v1/connections/{connection_id}/rejected", params={"user": u},
                    json={"reason": "slack: token_revoked"}).json()
if result["status"] == "active":
    ...  # Otari refreshed it; retry the call
else:
    ...  # "needs_reauth": ask the user to connect again
```

Otari attempts one forced refresh. If that works the connection stays
`active`; if it does not, the connection is marked `needs_reauth` with an
`invalid_reason`, and stays listed that way: a row you can show the user
rather than a hole where a connection used to be. A refresh Otari attempts on
its own and the provider refuses marks it the same way. Reconnecting clears it.

## Deleting a user

When one of your users deletes their account with you, `DELETE
/v1/connections/user?user=` revokes every grant of theirs at the provider,
best effort, and deletes the rows, so "delete my data" does not stop at your
own database. Connections the workspace shares are left alone: they belong to
the workspace, and other users are still acting through them.

## Encryption at rest

Each connection's tokens are encrypted with a Fernet key of that row's own,
and only that key is encrypted with `OTARI_SECRET_KEY`. Rotating the
deployment key therefore rewraps one short value per row instead of
re-encrypting every credential, and a single disclosed row key is worth one
account rather than all of them. Every write mints a fresh row key.

## What comes next

Connections exist so that Otari can act with them. The follow-ups, in order:

- **`credential: "connected"` on `mcp_servers`** and on built-in connectors:
  a completion request that names a `user` gets that user's token injected,
  and the application never sees it.
- **`connection_required`** as a structured outcome (a response field and a
  streaming event) when a request needs an app the user has not connected or
  has connected with too few scopes, carrying the authorization link, so the
  agent loop can pause for consent and resume the way Octonous's does.
- A dashboard page for operators to connect their own accounts, which is the
  same API with the operator as the user.
