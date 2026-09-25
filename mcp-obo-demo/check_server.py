"""
Call the server with a real Entra token, either directly or through the TrueFoundry gateway.

    python check_server.py <url> "$TOKEN"

Which token depends on what you're calling. Get one as yourself with Azure CLI
(Azure CLI's app id 04b07795-8ddb-461a-bbee-02f9e1bf7b46 must be pre-authorised
on the scope you request):

  Direct to the server (aud = mcp-api):
    TOKEN=$(az account get-access-token --scope "api://<mcp-api-app-id>/Tool.Write" --query accessToken -o tsv)
    python check_server.py http://localhost:8000/mcp "$TOKEN"

  Through the gateway (aud = agent-service; the gateway does the OBO exchange):
    TOKEN=$(az account get-access-token --scope "api://<agent-service-app-id>/access_as_user" --query accessToken -o tsv)
    python check_server.py https://<gateway-host>/<gateway-path> "$TOKEN"
    (the Agent Registry entry needs a mapping for 04b07795-... for this to resolve)
"""

import asyncio
import json
import sys

from fastmcp import Client


async def main(url: str, token: str):
    async with Client(url, auth=token) as c:
        tools = await c.list_tools()
        print("tools:", [t.name for t in tools])
        who = await c.call_tool("whoami", {})
        print("whoami:", json.dumps(who.data, indent=2))
        res = await c.call_tool("add", {"a": 2, "b": 3})
        print("add(2,3):", res.data)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
