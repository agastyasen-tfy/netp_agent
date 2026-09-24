# Mock agent — Entra agent identity through the TrueFoundry gateway

A small chat agent you deploy **inside the customer's environment** with TrueFoundry. It signs
the user in with Microsoft Entra (device code), authenticates itself with its own Entra app
(client credentials), and calls MCP tools and a model through the TrueFoundry gateway carrying
both identities. The gateway resolves the agent and the user, enforces who may act for whom,
and performs the on-behalf-of exchange so the MCP server receives a token that still names the
user. Nothing depends on Copilot Studio or any Microsoft-hosted runtime, so it works where
those cannot reach.

It reuses the `mcp-obo-demo` server and the gateway setup from the Copilot Studio runbook
(mcp-api, agent-service, Identity Provider, MCP server registration). Only the client side
changes: the Power Apps connector and Copilot Studio agent are replaced by this service.

```
user ──device code──▶ Entra ──user token (aud agent-service)──┐
                                                              ▼
mock-agent ──client creds──▶ Entra ──agent token (aud agent-service)
     │  Authorization: <user>   x-tfy-agent-authorization: <agent>
     ▼
TrueFoundry MCP Gateway ── resolve agent (azp) + user (preferred_username)
     │  may agent act for user?  is agent an MCP Server User?
     │  OBO exchange as agent-service  →  token aud mcp-api, same user
     ▼
mcp-obo-demo (whoami shows user, actor_app = agent-service)
```

## What you need beyond the Copilot Studio runbook

### Entra: one more app registration, `mock-agent`

1. App registrations → **+ New registration** → name `mock-agent`, single tenant, no redirect URI → Register.
   Copy the **Application (client) ID**.
2. **Authentication** → *Advanced settings* → **Allow public client flows: Yes** → Save.
   (Device-code sign-in needs this.)
3. **API permissions → + Add a permission → APIs my organization uses → `agent-service` →
   Delegated → `access_as_user`** → Add. Also add Microsoft Graph → Delegated → `offline_access`.
   Then **Grant admin consent**.
4. On **`agent-service` → Expose an API → Authorized client applications → + Add**: the
   `mock-agent` client ID, tick `access_as_user`. (Lets device-code sign-in get the user token
   silently after consent.)
5. **Certificates & secrets → + New client secret** → copy the value. This is the agent's own
   credential; it goes into a TrueFoundry Secret, never into the repo.

That is all. `mcp-api` and `agent-service` are unchanged; the gateway keeps exchanging as
agent-service exactly as before.

### TrueFoundry

1. **Secrets** → add `AGENT_CLIENT_SECRET` with the value from step 5. Copy its `tfy-secret://` FQN.
2. **Agent Registry → Create New Agent → Integrate a Remote Agent**: name `netapp-mock-agent`;
   Identity **Identity provider-backed**, provider `entra-copilot` (the one from the runbook),
   **Subject value = the `mock-agent` client ID**; Config Embedded; Access Control: add the users
   who will sign in as **Agent Access**. (If you registered a Copilot Studio agent earlier you can
   instead add this ID as a second Identity Provider Mapping on it — one registry entry, two client
   apps.)
3. **MCP Gateway → `aga-obo-demo` → Collaborators**: add `agent:netapp-mock-agent` as **MCP Server User**.
4. **Model provider account** you want to use (e.g. `openai-main`) → Collaborators: add the agent
   with a role that grants use, and make sure the signing-in users have access too. Or set
   `LLM_GATEWAY_API_KEY` to a TrueFoundry API key and skip this.
5. The Identity Provider needs both spellings of agent-service in Allowed Audiences and user
   resolution by `preferred_username` (already done in the runbook).

### Deploy

```bash
pip install -U truefoundry
tfy login --host https://<control-plane>
export WORKSPACE_FQN=<cluster>:<workspace>
export HOST=mock-agent.<cluster-base-domain>
export ENTRA_TENANT_ID=<tenant> AGENT_CLIENT_ID=<mock-agent id> GATEWAY_AUDIENCE_APP_ID=<agent-service id>
export AGENT_CLIENT_SECRET_FQN=tfy-secret://<tenant>:<group>:AGENT_CLIENT_SECRET
export MCP_GATEWAY_URL=https://<control-plane>/api/llm/<tenant>/mcp/<server>/server
export LLM_GATEWAY_BASE_URL=https://<control-plane>/api/llm
export LLM_MODEL=openai-main/gpt-4o-mini
python deploy.py
```

Open `https://$HOST`. Or locally: `cp .env.example .env`, fill it, `uvicorn app:app --port 8080`.

## Demo script (5 minutes)

1. Open the agent. The right panel shows the agent's client ID and the gateway it talks to.
2. **Sign in with Microsoft** → open the link, enter the code, sign in as a user with Agent Access.
3. Click **Show token claims**: two tokens, both `aud` = agent-service; the user's has
   `preferred_username`, the agent's has `azp` = mock-agent and no user. This is what the gateway sees.
4. Ask: *“Who am I according to your tools?”* → the trace shows `whoami` called through the
   gateway; the result names the signed-in user, `mode: delegated`, `actor_app` = agent-service,
   `aud` = mcp-api. Identity travelled through the exchange intact.
5. In TrueFoundry → the MCP server's traces: every call shows **agent = netapp-mock-agent, user =
   the signed-in person**.
6. Revoke: remove the user's Agent Access on the agent → the next tool call fails with a gateway
   **403**. Restore it. Then remove the agent's MCP Server User role → 403 again. Nothing changed
   in the agent or the MCP server; the gateway is the control point.
7. Optional: sign in as a second user without Agent Access → 403 on the first tool call. Same
   agent, different person, different answer.

## Files

- `app.py` — FastAPI service: device-code sign-in, agent token, MCP + LLM calls through the gateway with both headers, tool-calling loop.
- `static/index.html` — chat UI with an identity panel and per-message tool trace.
- `deploy.py`, `Dockerfile`, `requirements.txt`, `.env.example`.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Device-code start fails with `AADSTS7000218` / public client error | *Allow public client flows* is off on `mock-agent`. |
| Sign-in prompts for consent every time or fails `AADSTS65001` | `mock-agent` lacks delegated `access_as_user` on agent-service, or admin consent not granted. |
| `Agent token failed: invalid_client` | Wrong client secret, or the secret FQN isn't resolving; check the Service's env in TrueFoundry. |
| Gateway 401 on tools | Agent token's `azp` (mock-agent id) has no Agent Registry mapping, or the user's UPN doesn't match a TrueFoundry user. |
| Gateway 403 on tools | Agent isn't MCP Server User on the server, or the user lacks Agent Access on the agent. |
| Model call 401/403 | Agent or user not a collaborator on the model provider; or set `LLM_GATEWAY_API_KEY`. |
| MCP server 401 after the gateway accepted | Same as the runbook: scope casing or `requestedAccessTokenVersion` on mcp-api. |
