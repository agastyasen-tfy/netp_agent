"""
Minimal MCP server protected by Microsoft Entra ID JWTs.

Every request must carry a bearer token that:
  - is signed by Entra (verified against the tenant's JWKS)
  - has iss == https://login.microsoftonline.com/<TENANT_ID>/v2.0
  - has aud == the `mcp-api` app registration's Application (client) ID
  - carries the delegated scope `Tool.Write` in `scp`

In the on-behalf-of flow, this token is the one the TrueFoundry MCP Gateway
mints by exchanging the connector's `agent-service`-audienced token. The
`whoami` tool exists to prove that the signed-in Copilot Studio user's
identity survived the whole chain.
"""

import os

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

load_dotenv()

TENANT_ID = os.environ["ENTRA_TENANT_ID"]
AUDIENCE = os.environ["MCP_API_APP_ID"]  # bare GUID, not api://... (v2 tokens use the GUID in `aud`)
REQUIRED_SCOPE = os.environ.get("REQUIRED_SCOPE", "Tool.Write")

ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
# FASTMCP_TEST_JWKS is only for test_local.py; never set it in a real deployment.
JWKS_URI = os.environ.get("FASTMCP_TEST_JWKS") or f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"

class EntraJWTVerifier(JWTVerifier):
    """
    JWTVerifier that compares scopes case-insensitively.

    Entra matches requested scopes case-insensitively but returns them in `scp` exactly as the
    scope was spelled when it was created on the app registration. A scope created as
    `Tool.write` therefore satisfies a request for `Tool.Write` at the token endpoint but would
    fail an exact string comparison here. Normalising both sides avoids a per-environment
    REQUIRED_SCOPE override.
    """

    def _extract_scopes(self, claims):  # type: ignore[override]
        return [s.lower() for s in super()._extract_scopes(claims)]


verifier = EntraJWTVerifier(
    jwks_uri=JWKS_URI,
    issuer=ISSUER,
    audience=AUDIENCE,
    required_scopes=[REQUIRED_SCOPE.lower()],  # fastmcp reads both `scope` and Entra's `scp`
)

mcp = FastMCP("obo-demo", auth=verifier)


# --- Well-known routes -------------------------------------------------------
# Point OAuth discovery at Entra so any client that probes the server finds the
# right authorization server. Not used by the gateway in OBO mode, but harmless
# and useful when testing the server directly from an MCP client.
@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def oauth_well_known(_: Request):
    return RedirectResponse(f"{ISSUER}/.well-known/openid-configuration", status_code=307)


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_: Request):
    return JSONResponse({"status": "ok", "audience": AUDIENCE})


# --- Tools -------------------------------------------------------------------
@mcp.tool
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@mcp.tool
def subtract(a: float, b: float) -> float:
    """Subtract b from a."""
    return a - b


@mcp.tool
def echo(text: str) -> str:
    """Return the text you were given. Handy for checking the tool path end to end."""
    return text


@mcp.tool
def whoami() -> dict:
    """
    Show who this call is running as, according to the verified Entra token.

    Use this to confirm the on-behalf-of chain: `user` should be the person
    signed in to Copilot Studio, `actor_app` should be the agent-service app
    the gateway exchanged as, and `aud` should be this server's mcp-api ID.
    """
    token = get_access_token()
    claims = token.claims if token else {}
    return {
        "mode": "delegated" if claims.get("oid") else "application",
        "user": claims.get("preferred_username") or claims.get("upn") or claims.get("email"),
        "user_name": claims.get("name"),
        "user_oid": claims.get("oid"),  # stable per-user key; `sub` changes across exchanges
        "actor_app": claims.get("azp"),
        "aud": claims.get("aud"),
        "scp": claims.get("scp"),
        "iss": claims.get("iss"),
        "tenant": claims.get("tid"),
        "exp": claims.get("exp"),
    }


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        stateless_http=True,
    )
