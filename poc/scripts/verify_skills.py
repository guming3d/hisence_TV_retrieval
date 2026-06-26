"""Demo / verify prompt-composed skill behavior on the prompt agent.

This validates behavior copied from centrally stored Foundry skill content into
the prompt agent instructions. It does **not** prove native prompt-agent
support for Foundry Skills or toolbox resources.

Two probes exercise the two demo skills end-to-end:

1. ``spoiler-safe-scores`` — a spoiler-bait question that does NOT explicitly ask
   for the score. A skill-compliant answer must *not* blurt the final score and
   should offer a choice (score vs. highlights).
2. ``tv-screen-style`` — a recommendation request. A skill-compliant answer is
   short spoken Chinese, no markdown/URLs, <=3 picks, ending with one next step.

Prints each answer so skill compliance can be eyeballed during the demo.

Run (from ``poc/`` with the venv)::

    $env:FOUNDRY_PROJECT_ENDPOINT = "https://.../api/projects/control-plane-test"
    .venv/Scripts/python.exe scripts/verify_skills.py
    # optional: pin a version ->  $env:PROMPT_AGENT_VERSION = "15"
"""

from __future__ import annotations

import os
import sys

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

AGENT_NAME = os.environ.get("PROMPT_AGENT_NAME", "hisense-tv-assistant-prompt")
AGENT_VERSION = os.environ.get("PROMPT_AGENT_VERSION")  # optional pin

PROBES = [
    (
        "spoiler-safe-scores (剧透防护)",
        "昨晚的欧冠决赛精彩吗？值得看回放吗？",
    ),
    (
        "tv-screen-style (电视大屏作答风格)",
        "给我推荐今晚可以看的体育节目。",
    ),
]


def _agent_reference() -> dict:
    ref = {"type": "agent_reference", "name": AGENT_NAME}
    if AGENT_VERSION:
        ref["version"] = AGENT_VERSION
    return ref


def main() -> int:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ.get(
        "AZURE_AI_PROJECT_ENDPOINT"
    )
    if not endpoint:
        print("ERROR: set FOUNDRY_PROJECT_ENDPOINT", file=sys.stderr)
        return 2

    client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    oai = client.get_openai_client()
    ref = _agent_reference()
    pin = f" v{AGENT_VERSION}" if AGENT_VERSION else " (default)"
    print(f"Invoking agent '{AGENT_NAME}'{pin}\n")
    print(
        "NOTE: this prompt-agent check validates copied skill text in instructions, "
        "not native Foundry Skills attachment.\n"
    )

    for label, text in PROBES:
        print("=" * 78)
        print(f"## {label}")
        print(f"Q: {text}")
        try:
            resp = oai.responses.create(input=text, extra_body={"agent_reference": ref})
        except Exception as e:  # noqa: BLE001
            print(f"  INVOKE ERROR: {type(e).__name__}: {str(e)[:600]}")
            continue
        answer = getattr(resp, "output_text", None) or ""
        print("A:", answer.replace("\n", "\n   "))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
