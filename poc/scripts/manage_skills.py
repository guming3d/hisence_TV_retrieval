"""Register / list / verify **Foundry Skills (preview)** for the Hisense TV demo.

What a "skill" is
-----------------
A Foundry Skill is a small, reusable *behavioral guideline* authored as a
``SKILL.md`` file (agentskills.io spec): YAML front matter (``name`` +
``description``) followed by a Markdown body of instructions. Skills live in a
**central, versioned store** on the Foundry project and can then be delivered to
agents two ways:

* **Direct injection** (hosted agents): download ``SKILL.md`` into the agent
  project before deploy, then the hosted agent reads the bundled file at startup
  and appends the body to its system prompt. See ``src/skills_loader.py``.
* **Toolbox / MCP resources** (any MCP-aware client): attach the skill to a
  toolbox; it surfaces over the toolbox MCP endpoint as an MCP *resource*.
  Prompt agents themselves do not automatically read these resources. See
  ``scripts/create_skills_toolbox.py``.

This script manages the **central store** half: it reads the local
``src/skills/<name>/SKILL.md`` files (the source of truth) and publishes them to
``project.beta.skills`` so both agents can consume the same versioned skill.

Run (from ``poc/`` with the venv)::

    $env:FOUNDRY_PROJECT_ENDPOINT = "https://.../api/projects/control-plane-test"
    .venv/Scripts/python.exe scripts/manage_skills.py            # register if missing
    .venv/Scripts/python.exe scripts/manage_skills.py --force    # publish a new version
    .venv/Scripts/python.exe scripts/manage_skills.py --list     # show stored skills
    .venv/Scripts/python.exe scripts/manage_skills.py --verify   # download + print bodies
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects import models as m

# src/skills/<name>/SKILL.md -- the local source of truth for every skill.
SKILLS_DIR = Path(__file__).resolve().parents[1] / "src" / "skills"


def _endpoint() -> str:
    ep = os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ.get(
        "AZURE_AI_PROJECT_ENDPOINT"
    )
    if not ep:
        print(
            "ERROR: set FOUNDRY_PROJECT_ENDPOINT (e.g. from `azd env get-values -e poc`).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return ep


def _client() -> AIProjectClient:
    # allow_preview=True is required for the project.beta.* (Skills) surface.
    return AIProjectClient(
        endpoint=_endpoint(),
        credential=DefaultAzureCredential(),
        allow_preview=True,
    )


def parse_skill_md(path: Path) -> tuple[str, str, str]:
    """Return (name, description, body) parsed from a SKILL.md file.

    Front matter is the block between the first two ``---`` lines. ``name`` and
    ``description`` are read from it; the body is everything after the closing
    ``---`` (this becomes the skill's ``instructions``).
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{path}: missing YAML front matter")
    _, fm, body = text.split("---", 2)
    name = description = None
    for line in fm.splitlines():
        line = line.strip()
        if line.startswith("name:"):
            name = line[len("name:"):].strip()
        elif line.startswith("description:"):
            description = line[len("description:"):].strip()
    if not name or not description:
        raise ValueError(f"{path}: front matter must define name and description")
    return name, description, body.strip()


def discover_local_skills() -> list[tuple[str, str, str, Path]]:
    out: list[tuple[str, str, str, Path]] = []
    for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        name, desc, body = parse_skill_md(skill_md)
        out.append((name, desc, body, skill_md))
    if not out:
        print(f"ERROR: no SKILL.md found under {SKILLS_DIR}", file=sys.stderr)
        raise SystemExit(2)
    return out


def _existing_names(client: AIProjectClient) -> set[str]:
    try:
        return {s.name for s in client.beta.skills.list()}
    except Exception:  # noqa: BLE001 -- empty store / first run
        return set()


def cmd_register(client: AIProjectClient, force: bool) -> int:
    existing = _existing_names(client)
    for name, desc, body, path in discover_local_skills():
        if name in existing and not force:
            details = client.beta.skills.get(name)
            print(
                f"= skill '{name}' already exists "
                f"(default v{getattr(details, 'default_version', '?')}); skip "
                f"(use --force to publish a new version)."
            )
            continue
        verb = "Publishing new version of" if name in existing else "Creating"
        print(f"+ {verb} skill '{name}' from {path.relative_to(SKILLS_DIR.parents[1])} ...")
        ver = client.beta.skills.create(
            name=name,
            inline_content=m.SkillInlineContent(description=desc, instructions=body),
            default=True,  # newest version becomes the default the agents resolve
        )
        print(f"    OK -> version {getattr(ver, 'version', '?')} (set as default)")
    return 0


def cmd_list(client: AIProjectClient) -> int:
    skills = list(client.beta.skills.list())
    if not skills:
        print("(no skills registered)")
        return 0
    print(f"Registered skills ({len(skills)}):")
    for s in skills:
        print(
            f"  - {s.name}  [default v{getattr(s, 'default_version', '?')}, "
            f"latest v{getattr(s, 'latest_version', '?')}]"
        )
        print(f"      {getattr(s, 'description', '')}")
    return 0


def cmd_verify(client: AIProjectClient) -> int:
    for name, *_ in discover_local_skills():
        print(f"==== central store: skill '{name}' ====")
        chunks = client.beta.skills.download(name)
        data = b"".join(chunks)
        try:
            print(data.decode("utf-8"))
        except UnicodeDecodeError:
            print(f"(binary, {len(data)} bytes)")
        print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Manage Foundry Skills for the Hisense TV demo.")
    ap.add_argument("--list", action="store_true", help="list skills in the central store")
    ap.add_argument("--verify", action="store_true", help="download + print each stored skill")
    ap.add_argument("--force", action="store_true", help="publish a new version even if it exists")
    args = ap.parse_args()

    client = _client()
    if args.list:
        return cmd_list(client)
    if args.verify:
        return cmd_verify(client)
    rc = cmd_register(client, force=args.force)
    print()
    cmd_list(client)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
