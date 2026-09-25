"""
One-command deploy to TrueFoundry. No Git repo, no local Docker: the folder is
uploaded and built on the platform.

    pip install -U truefoundry
    tfy login --host https://<your-control-plane>          # once
    export WORKSPACE_FQN=<cluster>:<workspace>            # e.g. my-cluster:agastya-dev
    export HOST=mcp-obo-demo.<your-cluster-base-domain>   # see note below
    export ENTRA_TENANT_ID=...  MCP_API_APP_ID=...
    python deploy.py

HOST: the public hostname for the service. Your cluster has one or more base
domains (shown in the UI when you add a port to any service, or under
Platform → Clusters → <cluster> → Base Domains). Pick one and prefix it, e.g.
mcp-obo-demo.apps.acme.truefoundry.cloud.

Re-running the script redeploys in place.
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


service = Service(
    name="mcp-obo-demo",
    image=Build(
        build_source=LocalSource(project_root_path="./", local_build=False),  # upload + remote build
        build_spec=DockerFileBuild(dockerfile_path="./Dockerfile", build_context_path="./"),
    ),
    ports=[Port(port=8000, protocol="TCP", expose=True, app_protocol="http", host=need("HOST"))],
    env={
        "ENTRA_TENANT_ID": need("ENTRA_TENANT_ID"),
        "MCP_API_APP_ID": need("MCP_API_APP_ID"),
        "REQUIRED_SCOPE": "Tool.Write",
    },
    resources=Resources(cpu_request=0.2, cpu_limit=0.5, memory_request=256, memory_limit=512),
    replicas=1,
)

service.deploy(workspace_fqn=need("WORKSPACE_FQN"))
