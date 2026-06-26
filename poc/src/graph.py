# Copyright (c) Microsoft. All rights reserved.
"""LangGraph agent assembly for the Hisense TV Sports AI Assistant.

``build_graph()`` returns the compiled LangGraph agent the hosted runtime
serves over the ``responses`` protocol. The model is resolved from
:mod:`config`:

* **live** — a Foundry model deployment reached through ``ChatOpenAI`` with an
  Entra bearer token (the official hosted-agent pattern).
* **offline** — :class:`offline_model.FakeRouterChatModel`, so the graph runs
  with zero cloud setup for local smoke tests / demos.

The system instruction and model name come from :func:`config.get_agent_config`,
which is **Agent Optimizer-aware** — an optimized candidate from
``.agent_configs/`` is picked up here without any code change.
"""

from __future__ import annotations

from typing import Any

from config import get_agent_config, get_settings
from tools import ALL_TOOLS


def build_model() -> Any:
    """Construct the chat model for the current mode (live Foundry or offline)."""
    settings = get_settings()
    cfg = get_agent_config()

    if not settings.model_live:
        from offline_model import FakeRouterChatModel

        return FakeRouterChatModel()

    # ── Live: Foundry model deployment via the project's OpenAI endpoint ──
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from langchain_openai import ChatOpenAI

    credential = DefaultAzureCredential()
    project = AIProjectClient(endpoint=settings.project_endpoint, credential=credential)
    openai_client = project.get_openai_client()
    token_provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")

    return ChatOpenAI(
        model=settings.model_deployment,
        base_url=str(openai_client.base_url),
        api_key=token_provider,  # bearer-token provider; refreshed per call
        use_responses_api=True,
        output_version="responses/v1",
        temperature=cfg.temperature,
    )


def build_graph() -> Any:
    """Assemble the tool-calling LangGraph agent (model + bound scenario tools).

    When an Agent Optimizer config is active (baseline or a selected candidate),
    its optimized **tool definitions** are applied to the live ``@tool`` objects
    via ``OptimizationConfig.apply_tool_descriptions`` — this patches each tool's
    ``.description`` (and parameter descriptions) in place by name, so a candidate
    that rewrites tool docs takes effect without any code change. The optimized
    **instruction** is supplied through ``cfg.instruction``.
    """
    from langchain.agents import create_agent

    from skills_loader import compose_system_prompt

    cfg = get_agent_config()
    model = build_model()

    tools = list(ALL_TOOLS)
    opt_config = cfg.extra.get("raw_config")
    if opt_config is not None:
        try:
            opt_config.apply_tool_descriptions(tools)
        except Exception as exc:  # noqa: BLE001 — tool patching is best-effort
            print(f"[optimizer] apply_tool_descriptions skipped: {exc}")

    # Foundry Skills (preview) — direct injection: append each bundled SKILL.md
    # body (published to the central store via scripts/manage_skills.py) to the
    # system prompt. See src/skills_loader.py.
    system_prompt = compose_system_prompt(cfg.instruction)

    return create_agent(model, tools=tools, system_prompt=system_prompt)
