# Agent identity demo on TrueFoundry — MCP server + mock agent

End-to-end demo of a governed agent: an agent with its own Microsoft Entra identity, acting on
behalf of a signed-in user, calling MCP tools and a model **through the TrueFoundry gateway**. The
gateway resolves both identities, enforces who may act for whom, and performs the on-behalf-of
(OBO) exchange so the MCP server receives a token that still names the user. Everything runs in
the customer's own environment; nothing depends on Copilot Studio or any Microsoft-hosted runtime.

Two deployables, both in this repo:

| Folder           | What it is                                                                                          | Port |
| ---------------- | --------------------------------------------------------------------------------------------------- | ---- |
| `mcp-obo-demo/`  | FastMCP server that validates Entra JWTs (`aud` = mcp-api, scope `Tool.Write`) and serves `add`, `subtract`, `echo`, `whoami` | 8000 |
| `mock-agent/`    | FastAPI chat agent: device-code user sign-in, client-credentials agent token, LLM tool-calling loop, token-flow panel | 8080 |

```
user ──device code──▶ Entra ──user token (aud agent-service)──┐
                                                              ▼
mock-agent ──client creds──▶ Entra ──agent token (aud agent-service)
     │  Authorization: <user>   x-tfy-agent-authorization: <agent>
     ▼
TrueFoundry gateway ── resolve agent (azp) + user (preferred_username)
     │  may agent act for user?  is agent an MCP Server User?
     │  OBO exchange as agent-service  →  token aud mcp-api, same user
     ▼
mcp-obo-demo (whoami shows user, actor_app = agent-service)
```

---

## 0. What you need before starting

**In Microsoft Entra** (Cloud Application Administrator or higher on the customer's tenant):

| App registration | Role in the flow                                                            | Holds a secret?                     |
| ---------------- | --------------------------------------------------------------------------- | ----------------------------------- |
| `mcp-api`        | Audience of the MCP server. Exposes delegated scope `Tool.Write`.           | No                                  |
| `agent-service`  | Audience of every token sent to the gateway; the app the gateway exchanges as. Exposes `access_as_user`. | Yes — **TrueFoundry** holds it |
| `mock-agent`     | The agent's own identity. Public client flows on (device code) + a secret (client credentials). | Yes — the **agent Service** holds it |

Keep `agent-service` separate from `mock-agent` even with one agent: if they were the same app, the
agent would hold the credential that can mint mcp-api tokens and could bypass the gateway.

**In TrueFoundry:** a control plane the customer's users can log into, a workspace on a cluster
inside their network, and rights to add an Identity Provider (Settings → Security & Access),
Secrets, an Agent Registry entry and an MCP server.

**Network:** the cluster must reach `login.microsoftonline.com` (token validation and the OBO
exchange) and the control plane must reach the MCP server's hostname.

**On your laptop:** Python ≥ 3.10, `pip install -U truefoundry`.

---

## 1. Entra app registrations

Entra admin center → Identity → Applications → App registrations. Copy each Application (client)
ID as you go; you will need all three plus the Tenant ID (Overview page).

### 1a. `mcp-api`
1. **+ New registration** → name `mcp-api`, single tenant, no redirect URI → Register.
2. **Expose an API** → Application ID URI → Add → accept `api://<id>` → Save.
3. **+ Add a scope** → name `Tool.Write`, Admins and users, any display text, Enabled → Add scope.
   Note the exact casing you type; Entra returns it as stored.
4. **Manifest** → `requestedAccessTokenVersion` → `2` → Save. (Without this the server rejects the issuer.)

### 1b. `agent-service`
1. **+ New registration** → `agent-service`, single tenant → Register.
2. **Expose an API** → Application ID URI → Add → Save. **+ Add a scope** → `access_as_user`, Admins and users, Enabled.
3. **API permissions → + Add a permission → APIs my organization uses → mcp-api → Delegated → `Tool.Write`**; and **Microsoft Graph → Delegated → `offline_access`**. **Grant admin consent**.
4. **Manifest** → `requestedAccessTokenVersion` → `2` → Save.
5. **Certificates & secrets → + New client secret** → copy the value. This goes into TrueFoundry (step 3b).

### 1c. `mock-agent`
1. **+ New registration** → `mock-agent`, single tenant, no redirect URI → Register.
2. **Authentication → Advanced settings → Allow public client flows: Yes** → Save.
3. **API permissions → + Add a permission → APIs my organization uses → agent-service → Delegated → `access_as_user`**; and **Microsoft Graph → Delegated → `offline_access`**. **Grant admin consent**.
4. **Certificates & secrets → + New client secret** → copy the value. This goes into TrueFoundry (step 3b) for the agent Service.

(Optional, not required once admin consent is granted: agent-service → Expose an API → Authorized client applications → add the mock-agent ID.)

---

## 2. Deploy the MCP server

From your laptop, in `mcp-obo-demo/`:

```bash
tfy login --host https://<control-plane>          # once; opens a device-code page
export WORKSPACE_FQN=<cluster>:<workspace>       # Workspaces list in the UI shows this
export HOST=mcp-obo-demo.<cluster-base-domain>   # see "Finding the base domain" below
export ENTRA_TENANT_ID=<tenant-id>
export MCP_API_APP_ID=<mcp-api client id>
python deploy.py
```

`deploy.py` uploads the folder, builds the Dockerfile on the cluster and rolls out a Service with
those env vars. Watch **Deployments** in the UI; the first build takes a few minutes.

Verify:

```bash
curl https://$HOST/health          # {"status":"ok","audience":"<mcp-api id>"}
```

A POST to `https://$HOST/mcp` without a token returns **401** — correct.

Scope casing does not matter: Entra returns the scope exactly as it was spelled when created (a
scope typed as `Tool.write` comes back as `Tool.write` even when `Tool.Write` was requested), and the
server compares case-insensitively to match Entra's own behaviour. `REQUIRED_SCOPE` only needs
setting if you named the scope something other than `Tool.Write` entirely.

**Finding the base domain.** Every exposed Service gets a hostname on one of the cluster's base
domains. Find them under Platform → Clusters → the cluster, or open any existing Service → Ports and
look at its host. `HOST` must be `<anything>.<one of those domains>`.

---

## 3. Configure the gateway

All in the TrueFoundry UI on the customer's control plane.

### 3a. Identity Provider
**Settings → Security & Access → Identity Providers → +**

| Field             | Value                                                                        |
| ----------------- | ---------------------------------------------------------------------------- |
| Provider Name     | `entra`                                                                      |
| Issuer URL        | `https://login.microsoftonline.com/<tenant-id>/v2.0`                         |
| Allowed Audiences | `api://<agent-service id>` **and** `<agent-service id>` (both)               |
| JWKS URI          | `https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys`          |
| Resolve Token To  | TrueFoundry user; Email Claim `preferred_username`; Team Claim `groups`      |

The signing-in users' Entra UPNs must match existing TrueFoundry users' emails. If the UI offers
an agent resolution option, you can additionally resolve the agent from claim `azp`; keep user
resolution so the gateway can still enforce whom the agent acts for.

### 3b. Secrets
**Secrets → a group (e.g. `agent-demo`) → + Add Secret**, twice:
- `AGENT_SERVICE_CLIENT_SECRET` = agent-service secret (1b step 5)
- `AGENT_CLIENT_SECRET` = mock-agent secret (1c step 4)

Copy both FQNs (`tfy-secret://<tenant>:<group>:<name>`).

### 3c. MCP server
**MCP Gateway → Add Server → Connect any Remote MCP Server**

| Field          | Value                                                                  |
| -------------- | ---------------------------------------------------------------------- |
| Name           | `obo-demo`                                                             |
| URL            | `https://mcp-obo-demo.<base-domain>/mcp` — the Service, not the gateway |
| Auth Data      | **OAuth2**                                                             |
| OAuth Provider | **Microsoft Entra**                                                    |
| Grant Type     | **JWT Bearer** (on-behalf-of) — not Token Exchange                     |
| Token URL      | `https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token`      |
| Client ID      | `<agent-service id>`                                                   |
| Client Secret  | FQN of `AGENT_SERVICE_CLIENT_SECRET`                                   |
| Scopes         | `api://<mcp-api id>/Tool.Write`                                        |

Save. If the UI reports `AADSTS5002726 … unsupported token header` on save, ignore it: the UI tried
to exchange your TrueFoundry session token, which is not an Entra token. The entry is saved and the
real path works. Open the server → **How To Use** and copy the gateway URL; it looks like
`https://<control-plane>/api/llm/<tenant>/mcp/obo-demo/server`.

### 3d. Agent Registry
**Agent Registry → Create New Agent → Integrate a Remote Agent**
- Metadata: name `mock-agent`, any description.
- Agent Identity: **Identity provider-backed**, provider `entra`, **Subject value = the mock-agent client ID**.
- Config: **Embedded**.
- Access Control: the users who will sign in, as **Agent Access**. This grant is also "whom the agent may act for".

Then **MCP Gateway → obo-demo → Collaborators → + Add → Agents tab → `mock-agent`** as **MCP Server User**.

### 3e. Model access
Pick a model provider account (e.g. `openai-main`). Either add the agent and the users as
collaborators with a use role on that account, or create a TrueFoundry API key and store it as a
secret (`LLM_GATEWAY_API_KEY`) to pass in step 4. Note the model name in `provider/model` form.

---

## 4. Deploy the mock agent

From your laptop, in `mock-agent/`:

```bash
export WORKSPACE_FQN=<cluster>:<workspace>
export HOST=mock-agent.<cluster-base-domain>
export ENTRA_TENANT_ID=<tenant-id>
export AGENT_CLIENT_ID=<mock-agent client id>
export GATEWAY_AUDIENCE_APP_ID=<agent-service client id>
export AGENT_CLIENT_SECRET_FQN=tfy-secret://<tenant>:<group>:AGENT_CLIENT_SECRET
export MCP_GATEWAY_URL=<gateway URL from 3c>
export LLM_GATEWAY_BASE_URL=https://<control-plane>/api/llm
export LLM_MODEL=<provider>/<model>
# optional, if using a key for the model instead of collaborator grants:
# export LLM_GATEWAY_API_KEY_FQN=tfy-secret://<tenant>:<group>:LLM_GATEWAY_API_KEY
python deploy.py
```

Open `https://$HOST`. The right-hand panel shows the agent's client ID, the gateway URL and the model.

Re-running `python deploy.py` after any change redeploys in place. Env vars can also be edited in
the UI under the Service → Environment Variables.

---

## 5. Demo script (about five minutes)

1. **Sign in with Microsoft** → open the link, enter the code, sign in as a user with Agent Access.
2. **Show token flow**. Three cards: the user token and agent token the agent sends (both `aud` =
   agent-service; the agent's has `azp` = mock-agent and no user), and the token the MCP server
   received after the exchange (`aud` = mcp-api, `azp` = agent-service, same user and `oid`).
   The agent never holds that third token. Tick *show raw JWTs* to display the real header and
   payload with the signature hidden.
3. Ask *“Who am I according to your tools?”* → the trace shows `whoami` through the gateway; the
   result names the signed-in user with `mode: delegated`.
4. TrueFoundry → the MCP server's traces: every call shows **agent = mock-agent and the user**.
5. Revoke: remove the user's **Agent Access** on the agent → next tool call fails with a gateway
   **403**. Restore. Remove the agent's **MCP Server User** role → 403 again. Neither the agent nor
   the MCP server changed; the gateway is the control point.
6. Bypass test: change the MCP server's Auth Data to **Token Passthrough** → the server rejects the
   call (`audience mismatch` in its logs). The agent's token is useless at the MCP server; only the
   gateway's exchange gets in. Switch back to JWT Bearer and re-save.
7. Optional: sign in as a second user without Agent Access → 403 on the first tool call.

---

## 6. Running locally instead

Both services also run on a laptop for development:

```bash
# MCP server
cd mcp-obo-demo && cp .env.example .env    # ENTRA_TENANT_ID, MCP_API_APP_ID, REQUIRED_SCOPE
pip install -r requirements.txt && python server.py            # :8000
python test_local.py                                           # offline auth test, no Entra needed

# expose it so the gateway can reach it (URL changes each run; update the MCP server entry)
cloudflared tunnel --url http://localhost:8000

# mock agent
cd mock-agent && cp .env.example .env      # fill AGENT_CLIENT_ID, AGENT_CLIENT_SECRET, gateway URLs, LLM_MODEL
pip install -r requirements.txt && uvicorn app:app --port 8080  # :8080
```

Set `LOG_TOKENS=1` in the agent's `.env` to print the masked JWTs to the terminal on every call.

`.env` is read only for local runs; the Docker image does not include it. Deployed Services get the
same variables from `deploy.py`, with secrets as `tfy-secret://` references.

---

## 7. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `deploy.py` rejects `HOST` | Not on one of the cluster's base domains. Platform → Clusters → base domains. |
| Build fails in Deployments | Open the build logs; usually a network policy blocking PyPI from the build pod. |
| `/health` OK but every gateway call fails at the MCP server with 401 | Server log says why: `issuer mismatch` → `requestedAccessTokenVersion` not 2 on mcp-api; `audience mismatch` → `MCP_API_APP_ID` wrong or gateway not exchanging (Auth Data not JWT Bearer); `missing required scopes` → the gateway's Scopes field names a different scope than the server expects (casing is ignored). |
| Device-code start fails `AADSTS7000218` | *Allow public client flows* is off on mock-agent. |
| Sign-in errors `AADSTS65001` | mock-agent lacks delegated `access_as_user` on agent-service, or admin consent not granted. |
| `Agent token failed: invalid_client` | Wrong secret or the FQN isn't resolving; check the Service's env in the UI. |
| Gateway 401 on tools (token valid) | mock-agent ID has no Agent Registry mapping, Allowed Audiences missing a spelling, or the user's UPN matches no TrueFoundry user. |
| Gateway 403 on tools | Agent isn't MCP Server User on the server, or the user lacks Agent Access on the agent. |
| Model call 401/403 | Agent or user not a collaborator on the provider account; or set `LLM_GATEWAY_API_KEY`. |
| Saving the MCP server shows `AADSTS5002726` | Expected from the UI; it used your TrueFoundry session token. Entry is saved. |
| Worked, then broke after rotating a secret | Re-save the MCP server entry; credentials bind on save. |
| Everything fails at token validation on the cluster | No egress to `login.microsoftonline.com` from the cluster; fix the network policy. |

---

## Files

```
mcp-obo-demo/
  server.py          FastMCP server with Entra JWT verification; tools add/subtract/echo/whoami
  deploy.py          TrueFoundry Service deploy (upload + remote Docker build)
  Dockerfile, requirements.txt, .env.example
  test_local.py      offline test with a fake JWKS: 401 / wrong scope / good token
  check_server.py    call a running instance with a real Entra token (direct or via gateway)
mock-agent/
  app.py             FastAPI agent: device-code login, agent token, two-header gateway calls, LLM loop, /api/tokenflow
  static/index.html  chat UI, identity panel, token-flow cards, per-message tool trace
  deploy.py          TrueFoundry Service deploy
  Dockerfile, requirements.txt, .env.example
```
