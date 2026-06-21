# Copyright (c) Microsoft. All rights reserved.
"""Offline router chat model — keeps the LangGraph host runnable with no LLM.

In **live** mode the agent is driven by a real Foundry model deployment
(``ChatOpenAI`` against the project endpoint). In **offline** mode there is no
model, so this deterministic stand-in implements the minimal
``BaseChatModel`` surface (``bind_tools`` + ``_generate``) that
``langchain.agents.create_agent`` needs to run its tool-calling loop.

It does keyword routing over the latest user turn to pick exactly one tool,
emits a tool call, then — after the ``ToolMessage`` comes back — produces a
short final answer that quotes the tool output. This lets ``smoke_local.py``
assert end-to-end graph wiring and tool routing without any cloud calls.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

# Ordered routing rules: (keywords, tool_name, default_args). Evaluated top to
# bottom; content/recommendation intent is checked before the news keyword so
# "推荐一部关于…新闻…的纪录片" routes to retrieval, not Web IQ.
_ROUTING: list[tuple[tuple[str, ...], str, dict[str, Any]]] = [
    # Memory intent is the most specific — check it before content/news/schedule.
    (("根据我的", "我的偏好", "个性化", "还记得我", "我之前说", "我喜欢什么", "recall", "记得我吗"),
     "recall_viewer_preferences", {}),
    (("记住", "记一下", "我支持", "设为我的偏好", "以后多", "remember", "帮我记住"),
     "remember_viewer_preference", {}),
    (("推荐", "纪录片", "剧情", "介绍一下", "哪部", "那部", "讲的是", "recommend"),
     "foundry_iq_search", {}),
    (("比分", "进球", "赢了", "score", "比赛结果"), "get_live_scores", {}),
    (("换台", "切换", "切到", "tune", "看这个", "播这个"), "tune_to_channel", {}),
    (("几点", "今晚", "排播", "播出", "直播", "重播", "节目单", "schedule"),
     "query_schedule", {}),
    (("新闻", "最新", "最近", "现在", "图片", "照片", "latest", "news", "热点"),
     "webiq_search", {"vertical": "news"}),
]
_DEFAULT_TOOL = "foundry_iq_search"


def _latest_human_text(messages: Sequence[BaseMessage]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, list):
                return " ".join(
                    str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content
                )
            return str(content)
    return ""


def _route(query: str) -> tuple[str, dict[str, Any]]:
    low = query.lower()
    for keywords, tool_name, base_args in _ROUTING:
        if any(k.lower() in low for k in keywords):
            args = dict(base_args)
            if tool_name in ("webiq_search", "foundry_iq_search", "recall_viewer_preferences"):
                args["query"] = query
            elif tool_name == "remember_viewer_preference":
                args["note"] = query
            elif tool_name == "tune_to_channel":
                args["channel"] = "DR1 Denmark"
            return tool_name, args
    return _DEFAULT_TOOL, {"query": query}


class FakeRouterChatModel(BaseChatModel):
    """Deterministic, no-network chat model for offline demos and smoke tests."""

    @property
    def _llm_type(self) -> str:  # noqa: D401
        return "fake-router-offline"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeRouterChatModel":
        # Routing is keyword-based against known tool names, so we don't need to
        # inspect the bound schemas — just satisfy the create_agent contract.
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        last = messages[-1] if messages else None

        # Second pass: a tool already ran → produce the final natural answer.
        if isinstance(last, ToolMessage):
            tool_name = getattr(last, "name", "tool")
            try:
                payload = json.loads(last.content) if isinstance(last.content, str) else last.content
            except json.JSONDecodeError:
                payload = {"raw": str(last.content)[:200]}
            answer = (
                f"[离线演示] 已调用工具 `{tool_name}` 并获得结果。"
                f"工具返回(节选):{json.dumps(payload, ensure_ascii=False)[:300]}"
            )
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=answer))])

        # First pass: route the user query to exactly one tool.
        query = _latest_human_text(messages)
        tool_name, args = _route(query)
        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": tool_name, "args": args, "id": f"call_{uuid.uuid4().hex[:8]}",
                 "type": "tool_call"}
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=ai)])
