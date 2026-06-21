"""Create / version the **prompt-agent** variant of the Hisense TV assistant.

Why this exists
---------------
The main demo agent ``hisense-tv-assistant`` is a **hosted** LangGraph agent
(container + custom Python). That is the right shape for complex orchestration,
but it is *not* the easiest way to show an enterprise user how Foundry's native,
managed capabilities plug into an agent. A **prompt agent** is just an LLM + a
list of Foundry-managed tools (no container, no code) -- perfect for a "look how
little it takes to attach WebIQ / Foundry IQ / Memory" demo.

Single combined agent (all three Foundry-managed capabilities)
--------------------------------------------------------------
This script creates **one** prompt agent -- ``hisense-tv-assistant-prompt`` --
that carries all three Foundry-managed tools at once:

* **Web IQ**     -> ``MCPTool`` (api.microsoft.ai/v3/mcp) : latest web/news/images.
* **Memory**     -> ``MemorySearchPreviewTool`` : cross-session viewer profile,
  shared with the hosted agent's managed memory store.
* **Foundry IQ** -> ``AzureAISearchTool`` : grounded program-library knowledge
  over the production index ``hisense-programs`` (57 EPG/title docs), SEMANTIC
  ranking (the index is text + semantic, no vectors), with native citations.

Composition caveat (empirical -- re-tested by ``verify_prompt_agent.py``)
-------------------------------------------------------------------------
No Microsoft doc forbids combining these three tools on one prompt agent; the
tool catalog says you can attach multiple tools and the model decides which to
invoke. In *earlier* probing on the preview runtime we observed that Memory and
``AzureAISearchTool`` did not co-surface in the same turn (attaching Memory
silently dropped AI Search). We are intentionally combining all three here (per
request) and letting ``verify_prompt_agent.py`` re-test the three capabilities
against this single agent so the behavior is proven empirically rather than
assumed. If AI Search still does not fire while Memory is attached, the verify
output will show it (the agent is still created as requested).

``create_version`` always creates a new immutable version of the named agent
(older versions remain); this script prints the new name + version.

Run (from ``poc/`` with the venv)::

    $env:FOUNDRY_PROJECT_ENDPOINT = "https://.../api/projects/control-plane-test"
    .venv/Scripts/python.exe scripts/create_prompt_agent.py
"""

from __future__ import annotations

import os
import sys

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects import models as m


# --- Resource identifiers (this project / rg / sub) --------------------------
SUBSCRIPTION_ID = "b9da3a59-1b01-4211-b510-cdbc0790ed2c"
RESOURCE_GROUP = "minggu-2026"
ACCOUNT = "control-plane-test"
PROJECT = "control-plane-test"

_CONN_BASE = (
    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
    f"/providers/Microsoft.CognitiveServices/accounts/{ACCOUNT}"
    f"/projects/{PROJECT}/connections"
)

# AzureAISearch connection (AAD / keyless) -> hisense-poc-search-06211057, index hisense-programs
SEARCH_CONNECTION_ID = f"{_CONN_BASE}/hisense-search"
SEARCH_INDEX_NAME = os.environ.get("FOUNDRY_IQ_INDEX", "hisense-programs")

# Existing WebIQ RemoteTool (MCP) connection -> api.microsoft.ai/v3/mcp
WEBIQ_CONNECTION_ID = f"{_CONN_BASE}/WebIQ"

# Existing managed memory store (shared with the hosted agent)
MEMORY_STORE_NAME = os.environ.get("MEMORY_STORE_NAME", "hisense-viewer-memory")
MEMORY_SCOPE = os.environ.get("MEMORY_DEFAULT_SCOPE", "viewer_demo-viewer")

# One combined prompt agent carrying all three Foundry-managed tools.
ASSISTANT_AGENT_NAME = os.environ.get("PROMPT_AGENT_NAME", "hisense-tv-assistant-prompt")
MODEL_DEPLOYMENT = os.environ.get("PROMPT_AGENT_MODEL", "gpt-5.4")


# --- Instructions ------------------------------------------------------------
# Combined agent: Web IQ + Memory + Foundry IQ (program library), one agent.
ASSISTANT_INSTRUCTIONS = """你是“海信电视体育 AI 助手（Prompt Agent 版）”。用户通过电视遥控器一键唤起你，用自然语言询问节目单/体育新闻/球员球队动态，并希望得到结合个人偏好的个性化回应。请用简洁、口语化、面向电视屏幕的中文回答。

你具备三种 Microsoft Foundry 原生能力，请按问题类型选择合适的工具（可在同一轮里组合使用）：

1. 【Foundry IQ · 节目库知识检索（Azure AI Search 索引 hisense-programs）】当问题涉及“节目、剧集、频道、节目单、EPG、今晚有什么、那部关于…的节目”等与海信生产节目库相关的内容时，调用节目库检索，并**只依据检索返回的结果**回答节目内容与频道，回答时附带来源节目标题/引用。注意：在“同时挂载 Memory 的组合配置”下，节目库检索（Azure AI Search）可能不会触发；若本轮拿不到节目库检索结果，请**如实告知**“当前组合下暂时无法直接检索节目库”，并提供替代路径：①可改用 WebIQ 搜索该节目/频道的公开网络信息；②建议用户切换到配套的“海信节目库知识 Agent（hisense-program-library）”获取精确的节目单/EPG。**绝不可**凭通用知识编造节目名称或频道（例如不要凭空给出并不在节目库里的 CCTV/央视等内容）。

2. 【Web IQ · 联网检索 WebIQ】当问题是“最新/最近/现在/昨晚”的开放域新闻热点，或用户明确需要外部网页/图片（比如某球员的最新消息、比分、转会、相关图片）时，调用 WebIQ 联网检索 web/news/images，并引用真实来源链接。

3. 【Memory · 跨会话记忆与个性化】系统会自动记住并召回该观众的观影偏好（喜欢/支持的球队或运动、偏好的解说语言、想多看或不想看的内容）。当用户表达偏好时自然地记住；当用户问“还记得我吗”“根据我的偏好推荐”“我之前说过我喜欢什么”时，先用一两句话复述你记住的偏好。

个性化推荐时的接地规则：①先用记忆复述用户偏好；②**根据偏好方向去调用节目库检索（Foundry IQ）**，并只推荐检索命中的真实节目/频道，附来源；若本轮节目库检索未触发/无结果，按上面的替代路径如实说明，并可改用 WebIQ 或建议切换到“海信节目库知识 Agent”，不要编造；③如用户想要某球队/球员的最新动态或外部资讯/图片，调用 WebIQ 并引用真实来源链接。回答要言之有据，不要编造来源或节目名；信息不足时如实说明。"""


# --- Tool builders -----------------------------------------------------------
def _webiq_tool() -> m.MCPTool:
    """The literal WebIQ MCP server (api.microsoft.ai/v3/mcp).

    The runtime MCP tool requires ``server_url``; auth (x-apikey, CustomKeys) is
    supplied by the existing project connection, so no key is embedded here.
    ``require_approval="never"`` keeps the demo non-interactive.
    """
    return m.MCPTool(
        server_label="WebIQ",
        server_url="https://api.microsoft.ai/v3/mcp",
        project_connection_id=WEBIQ_CONNECTION_ID,
        require_approval="never",
        server_description=(
            "联网检索最新 web/news/images。用于‘最新/最近/现在/昨晚’的开放域新闻热点，"
            "或需要外部网页/图片链接的问题。"
        ),
    )


def _memory_tool() -> m.MemorySearchPreviewTool:
    """Managed cross-session memory (search + auto-update), shared with the hosted agent.

    ``update_delay`` defers the memory write/extraction pass out of the synchronous
    turn so it doesn't cascade dozens of write ops and blank the answer; recall
    (memory_search) still runs in-turn.
    """
    return m.MemorySearchPreviewTool(
        memory_store_name=MEMORY_STORE_NAME,
        scope=MEMORY_SCOPE,
        update_delay=120,
    )


def _ai_search_tool() -> m.AzureAISearchTool:
    """Foundry IQ: agentic retrieval over the production program-library index.

    The index is text + semantic (no vectors) -> SEMANTIC query type (not vector).
    """
    return m.AzureAISearchTool(
        azure_ai_search=m.AzureAISearchToolResource(
            indexes=[
                m.AISearchIndexResource(
                    project_connection_id=SEARCH_CONNECTION_ID,
                    index_name=SEARCH_INDEX_NAME,
                    query_type=m.AzureAISearchQueryType.SEMANTIC,
                    top_k=5,
                )
            ]
        ),
    )


def build_combined_definition() -> m.PromptAgentDefinition:
    """One agent carrying all three Foundry-managed tools.

    WebIQ (MCP) + Memory are known to coexist; Foundry IQ (AzureAISearchTool) is
    added here too so a single agent demonstrates all three capabilities. See the
    module docstring's composition caveat -- verify_prompt_agent.py re-tests
    whether AI Search still fires while Memory is attached.
    """
    return m.PromptAgentDefinition(
        model=MODEL_DEPLOYMENT,
        instructions=ASSISTANT_INSTRUCTIONS,
        tools=[_webiq_tool(), _memory_tool(), _ai_search_tool()],
    )


def _version(client: AIProjectClient, name: str, definition, description: str) -> None:
    print(f"\nCreating/versioning prompt agent '{name}' (model={MODEL_DEPLOYMENT}) ...")
    result = client.agents.create_version(
        agent_name=name, definition=definition, description=description
    )
    print("  OK created.")
    print(f"    agent name    : {getattr(result, 'name', name)}")
    print(f"    agent version : {getattr(result, 'version', None)}")


def main() -> int:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ.get(
        "AZURE_AI_PROJECT_ENDPOINT"
    )
    if not endpoint:
        print(
            "ERROR: set FOUNDRY_PROJECT_ENDPOINT (e.g. from `azd env get-values -e poc`).",
            file=sys.stderr,
        )
        return 2

    client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())

    _version(
        client,
        ASSISTANT_AGENT_NAME,
        build_combined_definition(),
        "Hisense TV assistant prompt-agent: Web IQ (MCP) + Memory + Foundry IQ "
        "(Azure AI Search, index hisense-programs, SEMANTIC). One agent demonstrating "
        "all three Foundry-managed capabilities; recalls the cross-session viewer "
        "profile, grounds program-library answers with citations, and fetches latest web/news.",
    )

    print("\nDone. Combined prompt agent created/versioned:")
    print(
        f"  * {ASSISTANT_AGENT_NAME}  -> WebIQ(MCP) + Memory({MEMORY_STORE_NAME}/{MEMORY_SCOPE})"
        f" + Foundry IQ AzureAISearch({SEARCH_INDEX_NAME}/SEMANTIC)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
