# Copyright (c) Microsoft. All rights reserved.
"""Scenario tools — EPG schedule, live scores, tune-to-channel.

These round out the TV-assistant story so the demo covers the full remote-key
experience, not just retrieval. They are intentionally lightweight:

* ``query_schedule`` reads the listings-derived ``data/kb/schedule.json`` (built
  from the real ``1.2026-04-14.json`` feed by ``scripts/build_kb_docs.py``).
  Schedule lives outside the Foundry IQ knowledge base on purpose — per the LLD,
  Postgres is the system of record for airing times and the RAG index is
  schedule-free. Here the local JSON stands in for that Postgres query.
* ``get_live_scores`` returns a deterministic mock scoreboard (no public sports
  API wired in for the POC); in production this calls the sports score service.
* ``tune_to_channel`` emits the device command the TV client would execute.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any

from langchain_core.tools import tool

from config import get_settings


@lru_cache(maxsize=1)
def _load_schedule() -> list[dict[str, Any]]:
    sched_file = get_settings().kb_dir / "schedule.json"
    if not sched_file.exists():
        return []
    try:
        data = json.loads(sched_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else data.get("listings", [])


@tool
def query_schedule(
    title: Annotated[str | None, "节目名关键字,可选。例如 '新闻' 或具体剧名。"] = None,
    channel: Annotated[str | None, "频道名关键字,可选。"] = None,
    live_only: Annotated[bool, "仅返回直播节目。"] = False,
    limit: Annotated[int, "返回条数(1-10)。"] = 6,
) -> str:
    """查询电视节目单 EPG(几点播、今晚有什么、直播还是重播)。

    返回节目的频道、开始/结束时间、是否直播/重播,以及关联的赛事外部 ID
    (若有,可直接用于 `get_live_scores`)。
    """
    limit = max(1, min(int(limit), 10))
    listings = _load_schedule()
    if not listings:
        return json.dumps(
            {"count": 0, "note": "排播数据未生成,请先运行 scripts/build_kb_docs.py。", "results": []},
            ensure_ascii=False,
        )

    def _match(item: dict[str, Any]) -> bool:
        if live_only and not item.get("live"):
            return False
        if title:
            hay = (str(item.get("title", "")) + " " + str(item.get("original_title", ""))).lower()
            if title.lower() not in hay:
                return False
        if channel and channel.lower() not in str(item.get("channel", "")).lower():
            return False
        return True

    matched = [i for i in listings if _match(i)]
    matched.sort(key=lambda x: str(x.get("start_time", "")))
    results = [
        {
            "title": i.get("title"),
            "channel": i.get("channel"),
            "start_time": i.get("start_time"),
            "end_time": i.get("end_time"),
            "live": i.get("live", False),
            "rerun": i.get("rerun", False),
            "match_external_id": i.get("match_external_id"),
        }
        for i in matched[:limit]
    ]
    return json.dumps({"count": len(results), "results": results}, ensure_ascii=False)


_MOCK_SCORES = {
    "default": {
        "competition": "示例联赛",
        "status": "FT",
        "home": {"team": "主队", "score": 2},
        "away": {"team": "客队", "score": 1},
        "note": "POC 示例比分(未接入真实赛事数据源)。",
    }
}


@tool
def get_live_scores(
    match_external_id: Annotated[
        str | None, "赛事外部 ID(来自 query_schedule),可选。"
    ] = None,
    team: Annotated[str | None, "球队名关键字,可选。"] = None,
) -> str:
    """获取实时/最新比分(现在比分、昨晚谁赢了)。

    POC 返回示例比分数据;生产环境对接体育赛事比分服务。需要最新新闻或图片时改用
    `webiq_search`。
    """
    score = dict(_MOCK_SCORES["default"])
    if match_external_id:
        score["match_external_id"] = match_external_id
    if team:
        score["queried_team"] = team
    return json.dumps({"match": score, "mock": True}, ensure_ascii=False)


@tool
def tune_to_channel(
    channel: Annotated[str, "要切换到的频道名,例如 'DR1'。"],
) -> str:
    """切换电视到指定频道(用户确认观看后调用)。

    返回设备指令,电视客户端据此执行换台。
    """
    command = {"action": "tune", "channel": channel, "status": "ok"}
    return json.dumps({"device_command": command}, ensure_ascii=False)
