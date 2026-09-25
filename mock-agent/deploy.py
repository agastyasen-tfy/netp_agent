"""
Deploy the mock agent to TrueFoundry from this folder (uploads + remote build; no Git, no local Docker).

    pip install -U truefoundry
    tfy login --host https://<control-plane>
    export WORKSPACE_FQN=<cluster>:<workspace>
    export HOST=mock-agent.<cluster-base-domain>
    export ENTRA_TENANT_ID=... AGENT_CLIENT_ID=... GATEWAY_AUDIENCE_APP_ID=...
    export AGENT_CLIENT_SECRET_FQN=tfy-secret://<tenant>:<group>:AGENT_CLIENT_SECRET
    export MCP_GATEWAY_URL=... LLM_GATEWAY_BASE_URL=... LLM_MODEL=...
    python deploy.py
"""

import logging
import os

from truefoundry.deploy import Build, DockerFileBuild, LocalSource, Port, Resources, Service

logging.basicConfig(level=logging.INFO)


def need(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Set {name} first (see docstring).")
    return v


env = {
    "ENTRA_TENANT_ID": need("ENTRA_TENANT_ID"),
    "AGENT_CLIENT_ID": need("AGENT_CLIENT_ID"),
    "AGENT_CLIENT_SECRET": need("AGENT_CLIENT_SECRET_FQN"),  # secret reference, never the value
    "GATEWAY_AUDIENCE_APP_ID": need("GATEWAY_AUDIENCE_APP_ID"),
    "MCP_GATEWAY_URL": need("MCP_GATEWAY_URL"),
    "LLM_GATEWAY_BASE_URL": need("LLM_GATEWAY_BASE_URL"),
    "LLM_MODEL": need("LLM_MODEL"),
    "AGENT_NAME": os.environ.get("AGENT_NAME", "netapp-mock-agent"),
    "PORT": "8080",
}
if os.environ.get("LLM_GATEWAY_API_KEY_FQN"):
    env["LLM_GATEWAY_API_KEY"] = os.environ["LLM_GATEWAY_API_KEY_FQN"]

service = Service(
    name=os.environ.get("SERVICE_NAME", "mock-agent"),
    image=Build(
        build_source=LocalSource(project_root_path="./", local_build=False),
        build_spec=DockerFileBuild(dockerfile_path="./Dockerfile", build_context_path="./"),
    ),
    ports=[Port(port=8080, protocol="TCP", expose=True, app_protocol="http", host=need("HOST"))],
    env=env,
    resources=Resources(cpu_request=0.2, cpu_limit=0.5, memory_request=256, memory_limit=512),
    replicas=1,
)

service.deploy(workspace_fqn=need("WORKSPACE_FQN"))
