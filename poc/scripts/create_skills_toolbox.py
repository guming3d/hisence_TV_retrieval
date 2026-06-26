"""Create a **Toolbox** that exposes the Hisense TV skills as MCP resources.

The second Foundry Skills delivery path (besides hosted-agent direct injection)
is the **toolbox / MCP resources** path: attach skills to a toolbox version, and
every MCP client that connects to the toolbox endpoint can list them as MCP
*resources* (``resources/list`` / ``resources/read``). This is the path an
MCP-aware client uses to consume a centrally-governed skill; prompt agents
themselves do not auto-load toolbox resources.

This toolbox carries **only skills** (no tools) — its whole job is to publish the
two demo skills over an MCP endpoint so you can show, in the Foundry portal and
via a raw MCP call, that the skills are discoverable as governed resources.

Endpoint (after creation)::

    {project_endpoint}/toolboxes/hisense-tv-skills/mcp?api-version=v1
    # required header: Foundry-Features: Toolboxes=V1Preview

Run (from ``poc/`` with the venv)::

    $env:FOUNDRY_PROJECT_ENDPOINT = "https://.../api/projects/control-plane-test"
    .venv/Scripts/python.exe scripts/create_skills_toolbox.py
"""

from __future__ import annotations

import os
import sys

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects import models as m

TOOLBOX_NAME = os.environ.get("SKILLS_TOOLBOX_NAME", "hisense-tv-skills")
SKILL_NAMES = ["tv-screen-style", "spoiler-safe-scores"]


def main() -> int:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ.get(
        "AZURE_AI_PROJECT_ENDPOINT"
    )
    if not endpoint:
        print("ERROR: set FOUNDRY_PROJECT_ENDPOINT.", file=sys.stderr)
        return 2

    client = AIProjectClient(
        endpoint=endpoint,
        credential=DefaultAzureCredential(),
        allow_preview=True,
    )

    skills = [m.ToolboxSkillReference(name=n) for n in SKILL_NAMES]
    print(f"Creating toolbox version '{TOOLBOX_NAME}' referencing skills: {SKILL_NAMES} ...")
    ver = client.beta.toolboxes.create_version(
        name=TOOLBOX_NAME,
        description=(
            "Hisense TV demo skills exposed as MCP resources: tv-screen-style "
            "(answer style) + spoiler-safe-scores (no-spoiler policy). Skills-only "
            "toolbox — demonstrates the toolbox/MCP-resources Skills delivery path."
        ),
        tools=[],
        skills=skills,
    )
    version = getattr(ver, "version", "?")
    print(f"  OK -> version {version} (default).")

    base = endpoint.rstrip("/")
    print("\nToolbox MCP endpoint (skills surface as MCP resources):")
    print(f"  latest : {base}/toolboxes/{TOOLBOX_NAME}/mcp?api-version=v1")
    print(f"  pinned : {base}/toolboxes/{TOOLBOX_NAME}/versions/{version}/mcp?api-version=v1")
    print("  header : Foundry-Features: Toolboxes=V1Preview")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
