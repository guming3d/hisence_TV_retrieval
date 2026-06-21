# Copyright (c) Microsoft. All rights reserved.
"""Offline smoke test — proves graph assembly + tool routing with no cloud.

Forces ``POC_OFFLINE=1`` so :func:`graph.build_graph` uses the deterministic
:class:`offline_model.FakeRouterChatModel`, then invokes the compiled LangGraph
agent with representative Hisense queries and asserts each routes to the
expected tool. Exits non-zero on any mismatch so CI can gate on it.

Run::

    python poc/scripts/smoke_local.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["POC_OFFLINE"] = "1"

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from langchain_core.messages import HumanMessage, ToolMessage  # noqa: E402

from graph import build_graph  # noqa: E402

# (query, expected tool name)
CASES: list[tuple[str, str]] = [
    ("推荐一部关于丹麦新闻的纪录片", "foundry_iq_search"),
    ("哈兰德昨晚的最新新闻和图片", "webiq_search"),
    ("DR1 今晚几点有节目?是不是直播", "query_schedule"),
    ("现在比分多少,昨晚谁赢了", "get_live_scores"),
    ("帮我换台到 DR1", "tune_to_channel"),
    ("记住我是皇家马德里球迷,以后多推荐西甲", "remember_viewer_preference"),
    ("根据我的偏好,有什么个性化推荐", "recall_viewer_preferences"),
]


def _tools_invoked(result: dict) -> list[str]:
    names: list[str] = []
    for msg in result.get("messages", []):
        if isinstance(msg, ToolMessage):
            names.append(getattr(msg, "name", "") or "")
    return names


def main() -> int:
    graph = build_graph()
    failures = 0
    for query, expected in CASES:
        result = graph.invoke({"messages": [HumanMessage(content=query)]})
        invoked = _tools_invoked(result)
        ok = expected in invoked
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {query!r} -> tools={invoked} (expected {expected})")
        if not ok:
            failures += 1

    final = result["messages"][-1].content if result.get("messages") else ""
    print(f"\nFinal answer sample: {str(final)[:160]}")

    if failures:
        print(f"\n{failures}/{len(CASES)} cases FAILED")
        return 1
    print(f"\nAll {len(CASES)} routing cases passed ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
