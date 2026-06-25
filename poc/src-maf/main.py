# Copyright (c) Microsoft. All rights reserved.
"""Hosted-agent entrypoint for the Hisense TV Sports AI Assistant (MAF harness).

This is the **Microsoft Agent Framework** twin of the LangGraph agent in
``poc/src/main.py``. Instead of a hand-built LangGraph, the agent is created with
the framework's **Agent Harness** (:func:`agent_framework.create_harness_agent`)
— the "latest harness feature" that ships built-in agentic planning (todo list),
plan/execute modes, automatic context compaction, skills, and GenAI OpenTelemetry
instrumentation on top of the same seven tools.

It is served over the Foundry ``responses`` protocol via
``agent_framework_foundry_hosting.ResponsesHostServer`` — the canonical hosting
surface used by the framework's ``04-hosting`` samples. Foundry Hosted Agents have
**built-in observability**: the platform injects
``APPLICATIONINSIGHTS_CONNECTION_STRING`` and manages the OTel exporters, so no
exporter wiring is needed in code (set ``ENABLE_SENSITIVE_DATA=true`` /
``AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED=true`` to also capture payloads).

Run locally::

    python poc/src-maf/main.py        # needs the MAF hosting deps installed

The host always starts (offline-safe): every tool falls back to a deterministic
local stub when Azure creds are absent (see ``config.Settings``).
"""

from __future__ import annotations

import os


def build_agent():
    """Construct the MAF harness agent wired to the seven Hisense TV tools.

    Kept as a standalone factory (mirrors ``src/graph.build_graph``) so it can be
    imported by smoke tests without starting the HTTP server.
    """
    from agent_framework import InMemoryHistoryProvider, create_harness_agent
    from agent_framework.foundry import FoundryChatClient
    from azure.identity import DefaultAzureCredential

    from config import DEFAULT_INSTRUCTION, get_settings
    from skills_loader import compose_system_prompt
    from tools import ALL_TOOLS

    settings = get_settings()

    # FoundryChatClient construction is lazy (no network call); the project
    # endpoint + model deployment are injected by the platform when hosted.
    client = FoundryChatClient(
        project_endpoint=os.environ.get("FOUNDRY_PROJECT_ENDPOINT", ""),
        model=settings.model_deployment,
        credential=DefaultAzureCredential(),
    )

    # Behavioural parity with the LangGraph agent: the same Chinese system prompt,
    # with the bundled / toolbox Foundry Skills injected as agent instructions.
    agent_instructions = compose_system_prompt(DEFAULT_INSTRUCTION)

    agent = create_harness_agent(
        client,
        name="hisense-tv-assistant-maf",
        description=(
            "海信电视体育 AI 助手 (Microsoft Agent Framework + Agent Harness)。"
            "与 LangGraph 版本相同的七个工具(Foundry IQ 知识检索、Web IQ 联网搜索、"
            "EPG 排播 / 实时比分 / 调台、跨会话观影偏好记忆),"
            "但由 MAF Harness 提供内置的任务规划、计划/执行模式、上下文压缩与可观测性。"
        ),
        agent_instructions=agent_instructions,
        tools=ALL_TOOLS,
        # ── Harness knobs (this is the "enable latest harness feature" ask) ──
        # Context window + output budget enable the harness's automatic
        # compaction once a conversation grows large.
        max_context_window_tokens=128_000,
        max_output_tokens=16_384,
        # HOSTING REQUIREMENT: ResponsesHostServer rejects an agent whose
        # HistoryProvider has load_messages=True (history is owned by the hosting
        # infra). The harness defaults to InMemoryHistoryProvider() (load=True),
        # so override with load_messages=False here.
        history_provider=InMemoryHistoryProvider(load_messages=False),
        # Toolset parity: the seven Hisense tools already cover retrieval and the
        # open web (webiq_search), so the harness's built-in web_search is off to
        # keep "same tools as the LangGraph agent".
        disable_web_search=True,
        # The harness's built-in (experimental) MemoryStore needs a writable
        # workspace + tool auto-approval that don't fit an unattended hosted
        # container; cross-session memory in this POC is provided by the Foundry
        # Memory tools (remember/recall_viewer_preferences) instead.
        disable_memory=True,
        # The agent's FoundryChatClient drives the model over the Foundry
        # **Responses API**, which threads multi-turn tool calls via server-side
        # state: turn 1 returns a ``function_call``; after the tool runs, the
        # continuation turn sends the ``function_call_output`` and the server
        # must resolve the matching ``function_call`` from the stored prior
        # response. ``store: false`` discards that prior response, so every
        # tool-calling turn fails with HTTP 400 "No tool call found for function
        # call output with call_id ..." (the user then sees an empty answer).
        # Keep ``store: true`` so tool results thread back correctly. This is the
        # model-call store flag and is independent of how the hosting infra
        # persists the inbound end-user conversation.
        default_options={"store": True},
        # HOSTING REQUIREMENT: the harness wires a ToolApprovalMiddleware by default
        # (human-in-the-loop "approve this tool call" gating) that *requires the caller
        # to pass an AgentSession to Agent.run*. ResponsesHostServer does not supply one,
        # so every tool-calling turn fails with "ToolApprovalMiddleware requires an
        # AgentSession." A hosted, unattended agent has no human to approve calls — tools
        # must auto-execute — so disable the middleware here.
        disable_tool_auto_approval=True,
        # LEFT ON intentionally (the harness features being showcased):
        #   • todo planning      (disable_todo)
        #   • plan/execute mode  (disable_mode)
        #   • context compaction (disable_compaction)
        #   • shell executor stays unset (no shell/file access in this host)
        #   • GenAI OTel tracing (auto-wired; collected by Foundry observability)
    )
    return agent


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # noqa: BLE001 — dotenv is a convenience for local runs only
        pass

    from agent_framework_foundry_hosting import ResponsesHostServer

    agent = build_agent()

    # The host binds the framework-default port (8088, matching the Dockerfile
    # EXPOSE) and is managed by the Foundry hosting infra when deployed.
    print("[host] starting MAF ResponsesHostServer (responses protocol)")
    ResponsesHostServer(agent).run()


if __name__ == "__main__":
    main()
