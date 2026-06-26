"""Copy centrally stored skill text into the **prompt agent** instructions.

This is a demo bridge, **not** native Foundry Skills attachment for prompt
agents. The official preview docs describe two supported delivery modes:

1. Attach skills to a toolbox so an MCP client can discover them as resources.
2. Download ``SKILL.md`` into a hosted/local agent project for direct injection.

``PromptAgentDefinition`` has no ``skills`` or toolbox field, and a prompt agent
has no client code to call ``resources/list`` / ``resources/read`` on a toolbox.
So the only safe way to reuse centrally stored skill guidance on a prompt agent
today is to **copy the skill bodies into the system prompt** and publish a new
agent version. This script performs that prompt-composition step, pulling the
skill bodies from the central store (``project.beta.skills.download``) so the
prompt agent follows the same authored content as the hosted agent/toolbox demo.

It re-publishes a new immutable version of ``hisense-tv-assistant-prompt`` that
**preserves the live model and tools** and only swaps in copied, skill-derived
instructions. Re-runnable: it strips any previously injected block before
re-composing, so versions don't stack.

Run (from ``poc/`` with the venv)::

    $env:FOUNDRY_PROJECT_ENDPOINT = "https://.../api/projects/control-plane-test"
    .venv/Scripts/python.exe scripts/update_prompt_agent_skills.py
"""

from __future__ import annotations

import io
import os
import sys
import zipfile

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects import models as m

AGENT_NAME = os.environ.get("PROMPT_AGENT_NAME", "hisense-tv-assistant-prompt")
# Order in which skills are folded into the prompt (style first, policy second).
SKILL_NAMES = ["tv-screen-style", "spoiler-safe-scores"]

SKILLS_MARKER = "# 已同步的技能说明"
LEGACY_SKILLS_MARKERS = ("# 已加载的 Foundry 技能",)
_HEADER = (
    f"{SKILLS_MARKER}（内容复制自 Foundry Skills 中央技能库 / preview）\n"
    "说明：以下行为规范是在发布 prompt agent 版本时从 Foundry Skills 复制进系统提示词的，"
    "并非 prompt agent 对 Skills / toolbox 资源的原生运行时加载；"
    "在任何工具调用与回答中都请严格遵守。"
)


def _strip_front_matter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text.strip()


def _download_skill_body(client: AIProjectClient, name: str) -> str:
    """Download the skill from the central store and return its SKILL.md body.

    ``skills.download`` streams a ZIP archive of the skill folder. Extract
    ``SKILL.md`` from it (falling back to raw-text decode for non-zip payloads).
    """
    data = b"".join(client.beta.skills.download(name))
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            md_name = next(
                (n for n in zf.namelist() if n.lower().endswith("skill.md")),
                None,
            )
            if md_name is None:
                raise ValueError(f"SKILL.md not found in downloaded archive for '{name}'")
            text = zf.read(md_name).decode("utf-8")
    else:
        text = data.decode("utf-8")
    return _strip_front_matter(text)


def _base_instructions(definition: m.PromptAgentDefinition) -> str:
    """Return the agent instructions with any prior injected skill block removed."""
    instr = definition.instructions or ""
    markers = (SKILLS_MARKER, *LEGACY_SKILLS_MARKERS)
    matches = [instr.find(marker) for marker in markers]
    idx = min((pos for pos in matches if pos != -1), default=-1)
    return (instr[:idx] if idx != -1 else instr).rstrip()


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

    # 1) Read the live latest version so we preserve model + tools verbatim.
    versions = list(client.agents.list_versions(AGENT_NAME))
    latest = max(versions, key=lambda v: int(getattr(v, "version", 0)))
    definition: m.PromptAgentDefinition = latest.definition
    print(
        f"Live latest: {AGENT_NAME} v{latest.version} "
        f"(model={definition.model}, {len(definition.tools or [])} tools)."
    )
    print(
        "NOTE: prompt agents do not natively load Foundry Skills or toolbox resources; "
        "this script copies the selected skill text into the agent instructions."
    )

    # 2) Pull the skill bodies from the central store and compose the block.
    blocks = [_HEADER]
    for name in SKILL_NAMES:
        details = client.beta.skills.get(name)
        ver = getattr(details, "default_version", "?")
        body = _download_skill_body(client, name)
        blocks.append(f"== Foundry Skill: {name} (v{ver}) ==\n{body}")
        print(f"  + copied skill text '{name}' (store v{ver}, {len(body)} chars)")

    base = _base_instructions(definition)
    new_instructions = base + "\n\n" + "\n\n".join(blocks) + "\n"

    # 3) New immutable version: same model + same tools, skill-augmented prompt.
    new_def = m.PromptAgentDefinition(
        model=definition.model,
        instructions=new_instructions,
        tools=definition.tools,
        temperature=getattr(definition, "temperature", None),
        top_p=getattr(definition, "top_p", None),
    )
    result = client.agents.create_version(
        agent_name=AGENT_NAME,
        definition=new_def,
        description=(
            "Hisense TV prompt agent with skill-derived text copied from the central "
            "Foundry Skills store: tv-screen-style and spoiler-safe-scores; model "
            "and WebIQ/Memory/Foundry IQ tools preserved. This is prompt composition, "
            "not native prompt-agent Skills attachment."
        ),
    )
    print(
        f"\nOK -> {getattr(result, 'name', AGENT_NAME)} v{getattr(result, 'version', '?')} "
        f"(instructions {len(base)} -> {len(new_instructions)} chars)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
