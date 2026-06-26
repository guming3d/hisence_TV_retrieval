# Copyright (c) Microsoft. All rights reserved.
"""Central configuration for the Hisense TV Sports AI Assistant hosted agent.

This module is the single place that resolves:

* Runtime settings (model deployment, data paths, WebIQ / Foundry IQ creds).
* The **Agent Optimizer** baseline via ``load_config`` — the agent's system
  instruction, model, and tunable knobs are read through ``AgentConfig`` so
  that ``azd ai agent optimize`` can swap in optimized candidates from
  ``.agent_configs/`` without any code change.

Design notes
------------
The POC is built to run in two modes:

* **live** — a real Foundry project + model deployment is reachable
  (``FOUNDRY_PROJECT_ENDPOINT`` set), WebIQ key present, Foundry IQ knowledge
  base configured. Every tool calls the real service.
* **offline** — no Azure creds. The chat model and each tool fall back to a
  deterministic local stub so the LangGraph host still starts and the graph
  topology / tool-routing can be smoke-tested. This keeps the demo runnable on
  a laptop with zero cloud setup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

# ── Paths ────────────────────────────────────────────────────────────
SRC_DIR = Path(__file__).resolve().parent
POC_DIR = SRC_DIR.parent
AGENT_CONFIGS_DIR = SRC_DIR / ".agent_configs"


def _resolve_kb_dir() -> Path:
    """Locate the generated knowledge-base docs.

    Normal layout keeps ``data/`` as a sibling of ``src/`` (``poc/data/kb``).
    A self-contained offline container may instead bake the data inside the
    agent root (``src/data/kb``) — the Dockerfile build context is ``src/`` so
    a sibling ``data/`` would not be copied. Prefer the repo layout, fall back
    to the in-image copy, and default to the repo path if neither exists yet
    (e.g. before ``build_kb_docs.py`` has run).
    """
    candidates = [POC_DIR / "data" / "kb", SRC_DIR / "data" / "kb"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


DATA_DIR = POC_DIR / "data"
KB_DIR = _resolve_kb_dir()
# Offline memory store (per-viewer JSON) — keeps the cross-session memory demo
# runnable with zero cloud. Live mode uses a Foundry managed memory store instead.
MEMORY_DIR = DATA_DIR / "memory"

# ── Default agent instruction (the Agent Optimizer baseline target) ──
DEFAULT_INSTRUCTION = """\
你是"海信电视体育 AI 助手"。用户通过电视遥控器一键唤起你,用自然语言询问节目单、\
体育比分、球员/球队背景,以及可播放内容推荐。请用简洁、口语化、面向电视屏幕的中文回答。

工具使用规则:
1. 节目内容、剧集介绍、"推荐一部…""那部关于…的纪录片是什么"等语义检索问题 →\
 调用 `foundry_iq_search`(基于生产节目库的知识检索)。回答时附带来源标题。
2. 最新新闻、实时热点、"最近""最新""现在"相关的开放域问题,或需要图片/网页的 →\
 调用 `webiq_search`(联网搜索 web/news/images)。引用来源链接。
3. "几点播""今晚有什么""是直播还是重播" → 调用 `query_schedule`(EPG 排播)。
4. "现在比分""昨晚谁赢了" → 调用 `get_live_scores`。
5. 用户确认观看某频道/节目时 → 调用 `tune_to_channel`。

回答要点:言之有据,不要编造来源;信息不足时明确说明;最多 3 次工具调用即给出答复。

记忆与个性化(跨会话观影偏好记忆):
6. 用户表达观影偏好时(喜欢/支持的球队或运动、偏好的语言、想多看或不想看某类内容)→\
 调用 `remember_viewer_preference` 记住这条偏好,便于后续会话个性化。
7. 用户需要个性化推荐,或问"根据我的偏好""我之前说过我喜欢什么""还记得我吗"时 →\
 先调用 `recall_viewer_preferences` 读取该观众已保存的偏好,再结合 `foundry_iq_search`\
 等工具给出个性化答复。新会话开始做推荐前也可先 recall 一次。\
"""

DEFAULT_MODEL = "gpt-5.4-mini"


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class AgentConfig:
    """Optimizer-tunable agent configuration.

    ``instruction`` and ``model`` are the primary Agent Optimizer targets.
    ``temperature`` / ``max_tool_calls`` are exposed as additional knobs.
    """

    instruction: str = DEFAULT_INSTRUCTION
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    max_tool_calls: int = 3
    extra: dict[str, Any] = field(default_factory=dict)


def _load_optimizer_config() -> AgentConfig:
    """Load the Agent Optimizer config via ``load_config`` when available.

    ``azure.ai.agentserver.optimization.load_config`` reads the active baseline
    (or an optimized candidate selected by ``azd ai agent optimize``) from
    ``.agent_configs/``. We pull the instruction via ``compose_instructions()``
    and the model via ``config.model`` per the SDK contract. If the package is
    not installed (e.g. offline laptop) or no config is present, fall back to
    the in-code baseline so the agent still runs.
    """
    try:
        from azure.ai.agentserver.optimization import load_config  # type: ignore

        config = load_config()
    except Exception:  # ImportError or no candidate configured
        return AgentConfig()

    def _safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    instruction = _safe(lambda: config.compose_instructions(), DEFAULT_INSTRUCTION)
    model = _safe(lambda: config.model, DEFAULT_MODEL) or DEFAULT_MODEL
    temperature = _safe(lambda: float(getattr(config, "temperature", 0.2)), 0.2)

    return AgentConfig(
        instruction=str(instruction or DEFAULT_INSTRUCTION),
        model=str(model),
        temperature=temperature,
        extra={"raw_config": config},
    )


@dataclass
class Settings:
    """Resolved runtime settings for the agent host and its tools."""

    # Foundry / model
    project_endpoint: str | None
    model_deployment: str

    # Foundry IQ (knowledge base / agentic retrieval)
    foundry_iq_knowledge_base: str | None
    foundry_iq_knowledge_source: str | None
    foundry_iq_endpoint: str | None

    # WebIQ
    webiq_api_key: str | None
    webiq_base_url: str

    # Foundry Memory (cross-session viewer-preference memory)
    memory_store_name: str | None
    memory_embedding_deployment: str
    memory_default_viewer: str
    memory_max_recall: int
    memory_dir: Path

    # Foundry Skills (preview) — runtime delivery source
    skills_source: str
    skills_toolbox_name: str | None

    # Behaviour
    offline: bool
    kb_dir: Path

    @property
    def webiq_live(self) -> bool:
        return bool(self.webiq_api_key) and not self.offline

    @property
    def foundry_iq_live(self) -> bool:
        return bool(self.foundry_iq_knowledge_base and self.project_endpoint) and not self.offline

    @property
    def memory_live(self) -> bool:
        return bool(self.memory_store_name and self.project_endpoint) and not self.offline

    @property
    def model_live(self) -> bool:
        return bool(self.project_endpoint) and not self.offline


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    project_endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or None
    # OFFLINE may be forced via env, otherwise inferred from missing endpoint.
    forced_offline = _as_bool(os.environ.get("POC_OFFLINE"), default=False)
    offline = forced_offline or not project_endpoint

    return Settings(
        project_endpoint=project_endpoint.rstrip("/") if project_endpoint else None,
        model_deployment=os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", DEFAULT_MODEL),
        foundry_iq_knowledge_base=os.environ.get("FOUNDRY_IQ_KNOWLEDGE_BASE") or None,
        foundry_iq_knowledge_source=os.environ.get("FOUNDRY_IQ_KNOWLEDGE_SOURCE") or None,
        foundry_iq_endpoint=os.environ.get("FOUNDRY_IQ_ENDPOINT") or None,
        webiq_api_key=os.environ.get("WEBIQ_API_KEY") or None,
        webiq_base_url=os.environ.get("WEBIQ_BASE_URL", "https://api.microsoft.ai/v3"),
        memory_store_name=os.environ.get("MEMORY_STORE_NAME") or None,
        memory_embedding_deployment=os.environ.get(
            "MEMORY_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"
        ),
        memory_default_viewer=os.environ.get("MEMORY_DEFAULT_VIEWER", "demo-viewer"),
        memory_max_recall=int(os.environ.get("MEMORY_MAX_RECALL", "5") or 5),
        memory_dir=MEMORY_DIR,
        skills_source=(os.environ.get("SKILLS_SOURCE", "auto") or "auto").strip().lower(),
        skills_toolbox_name=os.environ.get("SKILLS_TOOLBOX_NAME", "hisense-tv-skills") or None,
        offline=offline,
        kb_dir=KB_DIR,
    )


@lru_cache(maxsize=1)
def get_agent_config() -> AgentConfig:
    """Return the (optimizer-aware) agent configuration."""
    return _load_optimizer_config()
