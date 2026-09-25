"""
Offline test: runs a fake JWKS endpoint + the server with a throwaway RSA key,
then checks (1) no token -> 401, (2) token without Tool.Write -> 401,
(3) good token -> tools list + whoami shows the user.

    pip install -r requirements.txt PyJWT cryptography
    python test_local.py
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import httpx2 as httpx  # fastmcp >= 4 depends on httpx2
except ImportError:
    import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp import Client

TENANT = str(uuid.uuid4())
MCP_API = str(uuid.uuid4())
JWKS_PORT, MCP_PORT = 8790, 8791

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pub = key.public_key()
jwk = jwt.algorithms.RSAAlgorithm.to_jwk(pub, as_dict=True) | {"kid": "test-kid", "use": "sig", "alg": "RS256"}


class JWKS(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"keys": [jwk]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def mint(scp: str | None):
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": MCP_API,
        "iat": now, "nbf": now, "exp": now + 600,
        "oid": "11111111-2222-3333-4444-555555555555",
        "preferred_username": "alex@contoso.com",
        "name": "Alex Example",
        "azp": "agent-service-app-id",
        "tid": TENANT,
    }
    if scp:
        claims["scp"] = scp
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": "test-kid"})


async def main():
    threading.Thread(target=HTTPServer(("127.0.0.1", JWKS_PORT), JWKS).serve_forever, daemon=True).start()

    env = os.environ | {
        "ENTRA_TENANT_ID": TENANT, "MCP_API_APP_ID": MCP_API, "PORT": str(MCP_PORT),
        # override the two URLs that server.py derives from the tenant so they hit our fake JWKS
        "FASTMCP_TEST_JWKS": f"http://127.0.0.1:{JWKS_PORT}/keys",
    }
    proc = subprocess.Popen([sys.executable, "server.py"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{MCP_PORT}/mcp"
    try:
        for _ in range(50):
            try:
                httpx.get(f"http://127.0.0.1:{MCP_PORT}/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)

        r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 401, r
        print("no token          -> 401  OK")

        r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={"Accept": "application/json, text/event-stream",
                                "Authorization": f"Bearer {mint('Other.Scope')}"})
        assert r.status_code == 401, r
        print("wrong scope       -> 401  OK")

        async with Client(url, auth=mint("Tool.Write")) as c:
            tools = sorted(t.name for t in await c.list_tools())
            print("Tool.Write token  -> tools:", tools)
            assert tools == ["add", "echo", "subtract", "whoami"]
            who = await c.call_tool("whoami", {})
            print("whoami            ->", json.dumps(who.data, indent=2))
            assert who.data["user"] == "alex@contoso.com" and who.data["mode"] == "delegated"
            res = await c.call_tool("add", {"a": 2, "b": 3})
            assert res.data == 5.0
            print("add(2,3)          ->", res.data, " OK")
        print("\nALL CHECKS PASSED")
    finally:
        proc.terminate()
        err = proc.stderr.read().decode()
        if "Traceback" in err:
            print(err)


if __name__ == "__main__":
    asyncio.run(main())
