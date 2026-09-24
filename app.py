"""
Mock agent with an Entra agent identity, acting on behalf of a signed-in user,
calling MCP tools and an LLM through the TrueFoundry gateway.

Identity model (two bearer tokens per call, per the TrueFoundry agent-identity docs):
  Authorization:               the USER's Entra token   (aud = agent-service, obtained by device-code sign-in)
  x-tfy-agent-authorization:   the AGENT's Entra token  (aud = agent-service, obtained by client credentials)

The gateway resolves both, checks the agent may act for the user and may reach the
MCP server / model, then performs the OBO exchange as agent-service so the MCP
server receives a token audienced to mcp-api that still names the user.

Run locally:  cp .env.example .env && uvicorn app:app --port 8080
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import msal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from openai import AsyncOpenAI

load_dotenv()

# --- Config ------------------------------------------------------------------
TENANT_ID = os.environ["ENTRA_TENANT_ID"]
AGENT_CLIENT_ID = os.environ["AGENT_CLIENT_ID"]            # the mock agent's own Entra app
AGENT_CLIENT_SECRET = os.environ["AGENT_CLIENT_SECRET"]    # its client secret (tfy-secret:// on TF)
GATEWAY_AUDIENCE_APP_ID = os.environ["GATEWAY_AUDIENCE_APP_ID"]  # agent-service app id
MCP_GATEWAY_URL = os.environ["MCP_GATEWAY_URL"]            # https://<cp>/api/llm/<tenant>/mcp/<server>/server
LLM_GATEWAY_BASE_URL = os.environ["LLM_GATEWAY_BASE_URL"]  # https://<cp>/api/llm
LLM_MODEL = os.environ["LLM_MODEL"]                        # e.g. openai-main/gpt-4o-mini
LLM_GATEWAY_API_KEY = os.environ.get("LLM_GATEWAY_API_KEY")  # optional: use a TF key for the model instead of the user token
AGENT_NAME = os.environ.get("AGENT_NAME", "mock-agent")
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "6"))

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
USER_SCOPES = [f"api://{GATEWAY_AUDIENCE_APP_ID}/access_as_user"]
AGENT_SCOPES = [f"api://{GATEWAY_AUDIENCE_APP_ID}/.default"]

SYSTEM_PROMPT = (
    f"You are {AGENT_NAME}, a demo agent. You have tools reached through the TrueFoundry MCP Gateway. "
    "Use them whenever a user asks to add, subtract, echo text, or asks who they are. "
    "When asked about identity, always call whoami and report exactly what it returns; never guess."
)

app = FastAPI(title=AGENT_NAME)
STATIC = Path(__file__).parent / "static"


# --- Agent identity (client credentials, cached by MSAL) ----------------------
_agent_app = msal.ConfidentialClientApplication(
    AGENT_CLIENT_ID, authority=AUTHORITY, client_credential=AGENT_CLIENT_SECRET
)


def agent_token() -> str:
    result = _agent_app.acquire_token_for_client(scopes=AGENT_SCOPES)
    if "access_token" not in result:
        raise HTTPException(502, f"Agent token failed: {result.get('error')}: {result.get('error_description')}")
    return result["access_token"]


# --- User sessions (device-code sign-in, one MSAL cache per browser session) ---
@dataclass
class Session:
    pca: msal.PublicClientApplication = field(
        default_factory=lambda: msal.PublicClientApplication(AGENT_CLIENT_ID, authority=AUTHORITY)
    )
    flow: dict | None = None
    login_state: str = "signed_out"  # signed_out | pending | signed_in | error
    login_error: str | None = None
    account: dict | None = None
    claims: dict = field(default_factory=dict)
    created: float = field(default_factory=time.time)


SESSIONS: dict[str, Session] = {}


def get_session(request: Request, response: Response | None = None) -> Session:
    sid = request.cookies.get("sid")
    if not sid or sid not in SESSIONS:
        sid = secrets.token_urlsafe(24)
        SESSIONS[sid] = Session()
        if response is not None:
            response.set_cookie("sid", sid, httponly=True, samesite="lax")
    return SESSIONS[sid]


def user_token(s: Session) -> str:
    if s.login_state != "signed_in" or not s.account:
        raise HTTPException(401, "Sign in first")
    result = s.pca.acquire_token_silent(USER_SCOPES, account=s.account)
    if not result or "access_token" not in result:
        s.login_state = "signed_out"
        raise HTTPException(401, "Session expired; sign in again")
    return result["access_token"]


def _decode_claims(token: str) -> dict:
    try:
        import base64
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


# --- Gateway helpers -------------------------------------------------------------
def identity_headers(s: Session) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {user_token(s)}",
        "x-tfy-agent-authorization": f"Bearer {agent_token()}",
    }


def mcp_client(s: Session) -> Client:
    return Client(StreamableHttpTransport(MCP_GATEWAY_URL, headers=identity_headers(s)))


def llm_client(s: Session) -> AsyncOpenAI:
    if LLM_GATEWAY_API_KEY:
        return AsyncOpenAI(base_url=LLM_GATEWAY_BASE_URL, api_key=LLM_GATEWAY_API_KEY)
    h = identity_headers(s)
    return AsyncOpenAI(
        base_url=LLM_GATEWAY_BASE_URL,
        api_key=h["Authorization"].removeprefix("Bearer "),
        default_headers={"x-tfy-agent-authorization": h["x-tfy-agent-authorization"]},
    )


def mcp_tools_to_openai(tools) -> list[dict]:
    out = []
    for t in tools:
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
        out.append({
            "type": "function",
            "function": {"name": t.name, "description": t.description or "", "parameters": schema},
        })
    return out


def _result_text(res) -> str:
    if getattr(res, "data", None) is not None:
        return json.dumps(res.data) if not isinstance(res.data, str) else res.data
    parts = [getattr(c, "text", "") for c in (res.content or [])]
    return "\n".join(p for p in parts if p) or "(empty result)"


# --- Routes ----------------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok", "agent": AGENT_NAME}


@app.get("/api/status")
async def status(request: Request, response: Response):
    s = get_session(request, response)
    return {
        "agent_name": AGENT_NAME,
        "agent_client_id": AGENT_CLIENT_ID,
        "gateway_audience": GATEWAY_AUDIENCE_APP_ID,
        "mcp_gateway_url": MCP_GATEWAY_URL,
        "llm_model": LLM_MODEL,
        "login_state": s.login_state,
        "login_error": s.login_error,
        "user": {k: s.claims.get(k) for k in ("preferred_username", "name", "oid")} if s.claims else None,
        "device_flow": {"user_code": s.flow["user_code"], "verification_uri": s.flow["verification_uri"]}
        if s.flow and s.login_state == "pending" else None,
    }


@app.post("/api/login/start")
async def login_start(request: Request, response: Response):
    s = get_session(request, response)
    if s.login_state == "pending" and s.flow:
        return {"user_code": s.flow["user_code"], "verification_uri": s.flow["verification_uri"]}
    flow = s.pca.initiate_device_flow(scopes=USER_SCOPES)
    if "user_code" not in flow:
        raise HTTPException(502, f"Device flow failed: {flow.get('error_description') or flow}")
    s.flow, s.login_state, s.login_error = flow, "pending", None

    async def wait_for_user():
        result = await asyncio.to_thread(s.pca.acquire_token_by_device_flow, flow)
        if "access_token" in result:
            s.account = s.pca.get_accounts()[0] if s.pca.get_accounts() else None
            s.claims = _decode_claims(result["access_token"])
            s.login_state = "signed_in"
        else:
            s.login_state, s.login_error = "error", f"{result.get('error')}: {result.get('error_description')}"
        s.flow = None

    asyncio.create_task(wait_for_user())
    return {"user_code": flow["user_code"], "verification_uri": flow["verification_uri"]}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    sid = request.cookies.get("sid")
    if sid in SESSIONS:
        SESSIONS[sid] = Session()
    return {"ok": True}


@app.get("/api/identity")
async def identity(request: Request, response: Response):
    """What the gateway will see on the next call: both tokens' key claims (no secrets)."""
    s = get_session(request, response)
    u = _decode_claims(user_token(s))
    a = _decode_claims(agent_token())
    pick = lambda c, keys: {k: c.get(k) for k in keys}
    return {
        "user_token": pick(u, ("aud", "azp", "scp", "preferred_username", "oid", "iss")),
        "agent_token": pick(a, ("aud", "azp", "roles", "iss")),
    }


@app.get("/api/tools")
async def tools(request: Request, response: Response):
    s = get_session(request, response)
    try:
        async with mcp_client(s) as c:
            ts = await c.list_tools()
        return {"tools": [{"name": t.name, "description": t.description} for t in ts]}
    except Exception as e:
        raise HTTPException(502, f"MCP gateway: {e}")


@app.post("/api/tool/{name}")
async def call_tool(name: str, request: Request, response: Response):
    s = get_session(request, response)
    args = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    try:
        async with mcp_client(s) as c:
            res = await c.call_tool(name, args or {})
        return {"tool": name, "result": res.data if res.data is not None else _result_text(res)}
    except Exception as e:
        raise HTTPException(502, f"MCP gateway: {e}")


@app.post("/api/chat")
async def chat(request: Request, response: Response):
    s = get_session(request, response)
    body = await request.json()
    history: list[dict[str, Any]] = body.get("messages", [])
    if not history or history[-1].get("role") != "user":
        raise HTTPException(400, "messages must end with a user message")

    trace: list[dict] = []
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]

    try:
        async with mcp_client(s) as mcp:
            tool_defs = mcp_tools_to_openai(await mcp.list_tools())
            llm = llm_client(s)
            for _ in range(MAX_TOOL_ROUNDS):
                completion = await llm.chat.completions.create(
                    model=LLM_MODEL, messages=messages, tools=tool_defs or None, tool_choice="auto" if tool_defs else None
                )
                msg = completion.choices[0].message
                if not msg.tool_calls:
                    return {"reply": msg.content or "", "trace": trace}
                messages.append({
                    "role": "assistant", "content": msg.content or None,
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                })
                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments or "{}")
                    t0 = time.time()
                    try:
                        res = await mcp.call_tool(tc.function.name, args)
                        text = _result_text(res)
                        ok = True
                    except Exception as e:
                        text, ok = f"error: {e}", False
                    trace.append({"tool": tc.function.name, "args": args, "result": text, "ok": ok, "ms": int((time.time() - t0) * 1000)})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})
            return {"reply": "(stopped: too many tool rounds)", "trace": trace}
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e), "trace": trace})
