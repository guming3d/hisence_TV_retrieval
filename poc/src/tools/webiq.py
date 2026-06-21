# Copyright (c) Microsoft. All rights reserved.
"""WebIQ tool — latest web / news / images for the Hisense TV assistant.

Demonstrates **Microsoft Web IQ (Foundry)** as a live grounding source for
open-domain and freshness-sensitive questions (latest match news, "what
happened last night", player photos). Adapted from the customer's
``webiq_test_client.webiq_client`` REST client, trimmed to the three verticals
this POC needs (web / news / images) with API-key auth.

Exposed to LangGraph as a single ``webiq_search`` tool. When ``WEBIQ_API_KEY``
is absent (offline demo), it returns a clearly-labelled deterministic stub so
the graph stays runnable without network access.
"""

from __future__ import annotations

import json
import random
import time
from typing import Annotated, Any, Literal

import requests
from langchain_core.tools import tool

from config import get_settings

_RETRYABLE = {429, 500, 503, 504}
_MAX_RETRIES = 3
_TIMEOUT = 30.0

# Documented per-vertical result caps (from the Web IQ v3 API reference).
_MAX_RESULTS = {"web": 50, "news": 20, "images": 30}


class WebIQError(RuntimeError):
    """Raised on a non-success Web IQ HTTP response."""


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    url = f"{settings.webiq_base_url.rstrip('/')}{path}"
    headers = {
        "host": "api.microsoft.ai",
        "content-type": "application/json",
        "x-apikey": settings.webiq_api_key or "",
    }
    payload = {k: v for k, v in body.items() if v is not None}

    attempt = 0
    while True:
        attempt += 1
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=_TIMEOUT)
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                return {"_raw": resp.text[:2000]}
        if resp.status_code in _RETRYABLE and attempt <= _MAX_RETRIES:
            time.sleep(min(2 ** (attempt - 1), 10) + random.uniform(0, 0.4))
            continue
        raise WebIQError(f"WebIQ HTTP {resp.status_code}: {resp.text[:300]}")


def _search_live(query: str, vertical: str, max_results: int) -> dict[str, Any]:
    cap = _MAX_RESULTS[vertical]
    max_results = max(1, min(max_results, cap))
    if vertical == "web":
        data = _post("/search/web", {"query": query, "maxResults": max_results,
                                     "contentFormat": "text"})
    elif vertical == "news":
        data = _post("/search/news", {"query": query, "maxResults": max_results,
                                      "contentFormat": "text"})
    else:  # images
        data = _post("/search/images", {"query": query, "maxResults": max_results,
                                        "safeSearch": "strict"})
    return data


def _summarize(data: dict[str, Any], vertical: str, max_results: int) -> dict[str, Any]:
    """Normalize a Web IQ response into a compact, model-friendly shape.

    The v3 API returns a per-vertical envelope: ``webResults`` / ``newsResults`` /
    ``imageResults`` (each item carries ``url`` + ``title``; news/images also carry
    a thumbnail and host page). We flatten them into a uniform list the model can
    cite, enriching image/news rows with ``thumbnail_url`` and ``source``.
    """
    results: list[dict[str, Any]] = []
    raw_items = (
        data.get("results")
        or data.get("value")
        or data.get("webResults")
        or data.get("newsResults")
        or data.get("imageResults")
        or data.get("images")
        or []
    )
    for item in raw_items[:max_results]:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {
            "title": item.get("title") or item.get("name"),
            "url": item.get("url") or item.get("contentUrl") or item.get("link"),
            "snippet": (item.get("snippet") or item.get("description")
                        or item.get("content") or "")[:400],
        }
        thumbnail = item.get("thumbnailUrl") or item.get("thumbnail")
        if thumbnail:
            entry["thumbnail_url"] = thumbnail
        source = item.get("hostPageUrl") or item.get("source")
        if source:
            entry["source"] = source
        results.append(entry)
    return {"vertical": vertical, "count": len(results), "results": results}


def _search_offline(query: str, vertical: str, max_results: int) -> dict[str, Any]:
    stub = {
        "vertical": vertical,
        "count": 1,
        "offline_stub": True,
        "results": [
            {
                "title": f"[OFFLINE STUB] WebIQ {vertical} result for: {query}",
                "url": "https://example.invalid/webiq-offline",
                "snippet": (
                    "WEBIQ_API_KEY 未配置或处于离线模式,返回占位结果。"
                    "配置 WEBIQ_API_KEY 后将返回真实的联网搜索内容。"
                ),
            }
        ],
    }
    return stub


@tool
def webiq_search(
    query: Annotated[str, "自然语言搜索词,例如 '哈兰德 最新进球 新闻'。"],
    vertical: Annotated[
        Literal["web", "news", "images"],
        "搜索类型:web=网页,news=新闻,images=图片。最新动态/赛果用 news,要图片用 images。",
    ] = "news",
    max_results: Annotated[int, "返回结果条数(1-10)。"] = 5,
) -> str:
    """联网搜索最新的网页 / 新闻 / 图片(Microsoft Web IQ)。

    用于回答开放域、强时效性的问题:最新比赛新闻、"昨晚发生了什么"、球员近况、
    需要配图的查询。返回标题、链接与摘要,回答时请引用来源链接。
    """
    settings = get_settings()
    max_results = max(1, min(int(max_results), 10))
    try:
        if settings.webiq_live:
            data = _search_live(query, vertical, max_results)
            summary = _summarize(data, vertical, max_results)
        else:
            summary = _search_offline(query, vertical, max_results)
    except Exception as exc:  # noqa: BLE001 — surface as tool message, don't crash turn
        summary = {"vertical": vertical, "count": 0, "error": str(exc), "results": []}
    return json.dumps(summary, ensure_ascii=False)
