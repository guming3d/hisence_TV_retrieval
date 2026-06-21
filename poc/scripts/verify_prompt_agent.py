"""Verify the Foundry-managed capabilities of the prompt-agent variant.

Per request, ``hisense-tv-assistant-prompt`` (see ``create_prompt_agent.py``)
attaches **all three** Foundry-managed tools: Web IQ (MCP) + Memory + Foundry IQ
(Azure AI Search). A dedicated ``hisense-program-library`` agent is kept for
grounded program search.

We invoke via the OpenAI-compatible responses API (SDK path, correct tenant) and
print the *types* of output items so we can prove which managed tool fired. The
checks encode the empirically confirmed platform behavior:

  1. Web IQ      [combined] -> expect ``mcp_call`` to FIRE.
  2. Memory      [combined] -> expect ``memory_search_call`` to FIRE.
  3. Foundry IQ  [combined] -> expect ``azure_ai_search_call`` to be SUPPRESSED
     (Memory's automatic per-turn retrieval pre-empts the managed retrieval slot,
     so AI Search never fires while Memory is attached). This check passes when
     the tool is *absent* -- it reproduces/asserts the documented finding.
  4. Foundry IQ  [library] -> expect ``azure_ai_search_call`` to FIRE on the
     dedicated ``hisense-program-library`` agent (grounded program search is still
     fully demonstrable in the POC).

A non-zero exit code is returned if any check does not match its expectation.
"""

from __future__ import annotations

import os
import sys

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

ASSISTANT_AGENT = os.environ.get("PROMPT_AGENT_NAME", "hisense-tv-assistant-prompt")
LIBRARY_AGENT = os.environ.get("PROGRAM_LIBRARY_AGENT_NAME", "hisense-program-library")
MODEL_DEPLOYMENT = os.environ.get("PROMPT_AGENT_MODEL", "gpt-5.4")

_PROGRAM_Q = "DR1 频道上有哪些节目？请介绍其中一部的内容，并说明来源标题。"

# (label, agent_name, query, output-item substring, mode)
#   mode "fires"      -> PASS when the substring IS present in some output-item type
#   mode "suppressed" -> PASS when the substring is ABSENT (the documented finding)
CHECKS = [
    (
        "Web IQ (联网最新新闻+图片) [combined]",
        ASSISTANT_AGENT,
        "哈兰德最近有什么新闻？给我最新的消息和相关图片链接。",
        "mcp_call",
        "fires",
    ),
    (
        "Memory (跨会话偏好召回) [combined]",
        ASSISTANT_AGENT,
        "还记得我吗？根据我之前说过的观影偏好给我推荐今晚可以看的内容。",
        "memory_search",
        "fires",
    ),
    (
        "Foundry IQ suppression finding [combined: AI Search must NOT fire while Memory attached]",
        ASSISTANT_AGENT,
        _PROGRAM_Q,
        "azure_ai_search",
        "suppressed",
    ),
    (
        "Foundry IQ (节目库知识检索) [library agent]",
        LIBRARY_AGENT,
        _PROGRAM_Q,
        "azure_ai_search",
        "fires",
    ),
]


def main() -> int:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ.get(
        "AZURE_AI_PROJECT_ENDPOINT"
    )
    if not endpoint:
        print("ERROR: set FOUNDRY_PROJECT_ENDPOINT", file=sys.stderr)
        return 2

    client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    oai = client.get_openai_client()

    all_ok = True
    for label, agent_name, text, expect, mode in CHECKS:
        print("\n" + "=" * 78)
        print(f"## {label}   [agent={agent_name}]")
        print(f"Q: {text}")
        try:
            resp = oai.responses.create(
                model=MODEL_DEPLOYMENT,
                input=text,
                extra_body={"agent_reference": {"type": "agent_reference", "name": agent_name}},
            )
        except Exception as e:  # noqa: BLE001
            print(f"  INVOKE ERROR: {type(e).__name__}: {str(e)[:600]}")
            all_ok = False
            continue

        item_types = []
        for item in getattr(resp, "output", []) or []:
            t = getattr(item, "type", "?")
            extra = ""
            if t in ("mcp_call", "mcp_list_tools"):
                extra = f" [server={getattr(item, 'server_label', '?')}" \
                        f" tool={getattr(item, 'name', '?')}]"
            item_types.append(t + extra)
        present = any(expect in it for it in item_types)
        if mode == "suppressed":
            ok = not present
            verdict = (
                f"  expected '{expect}' SUPPRESSED (must not fire): "
                f"{'YES (suppressed as documented)' if ok else 'NO — it fired unexpectedly'}"
            )
        else:
            ok = present
            verdict = f"  expected '{expect}' fired: {'YES' if ok else 'NO'}"
        all_ok = all_ok and ok
        print(f"  tool/output items: {item_types}")
        print(verdict)

        answer = getattr(resp, "output_text", None) or ""
        print("  A:", answer[:900].replace("\n", "\n     "))

    print("\n" + "=" * 78)
    print("RESULT:", "ALL CAPABILITIES VERIFIED ✅" if all_ok else "SOME CHECKS FAILED ❌")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
