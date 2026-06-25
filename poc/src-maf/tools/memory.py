# Copyright (c) Microsoft. All rights reserved.
"""Foundry Memory tools — cross-session viewer-preference personalization.

Demonstrates **Microsoft Foundry managed Memory** as the persistence layer that
turns the Hisense remote-key assistant from stateless Q&A into a personalized
companion. A viewer's profile — favorite teams / sports, preferred language,
liked or disliked genres — is remembered across the many short sessions a TV
user has over weeks, so "根据我的偏好,今晚推荐什么" is answered without
re-asking.

Two tools, two execution paths each:

* ``remember_viewer_preference`` — persist a stated preference.
    * **live** — ``beta.memory_stores.begin_update_memories(update_delay=0)`` lets
      the Foundry memory store's chat + embedding models *extract* a structured
      ``USER_PROFILE`` memory from the note (the managed-memory value-add), keyed
      by a per-viewer ``scope``.
    * **offline** — append the note to a local per-viewer JSON file.
* ``recall_viewer_preferences`` — retrieve what we know about this viewer.
    * **live** — ``beta.memory_stores.search_memories`` does semantic recall over
      the viewer's ``scope``.
    * **offline** — read the local JSON (optional keyword filter).

Memory is partitioned per viewer via ``scope = "viewer_{viewer_id}"`` (the update
endpoint only accepts ``[A-Za-z0-9_-]``). The POC defaults to a single demo viewer
(``MEMORY_DEFAULT_VIEWER``); a real TV client would pass the signed-in
household/profile id. Gated by ``MEMORY_STORE_NAME`` +
``FOUNDRY_PROJECT_ENDPOINT`` — absent either, the tools run fully offline so the
demo still works on a laptop.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Annotated, Any

from agent_framework import tool

from config import get_settings

_TOKEN_RE = re.compile(r"[\w\u00c0-\u024f\u4e00-\u9fff]+", re.UNICODE)


def _scope(viewer_id: str | None) -> str:
    # The memory update endpoint allows only [A-Za-z0-9_-] in scope (stricter
    # than search), so use '_' as the namespace separator and sanitize the id.
    settings = get_settings()
    vid = (viewer_id or settings.memory_default_viewer or "demo-viewer").strip()
    vid = re.sub(r"[^A-Za-z0-9_-]", "-", vid) or "demo-viewer"
    return f"viewer_{vid}"


# ── Offline store (per-viewer JSON) ──────────────────────────────────


def _viewer_file(viewer_id: str | None):
    settings = get_settings()
    vid = (viewer_id or settings.memory_default_viewer or "demo-viewer").strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", vid) or "demo-viewer"
    return settings.memory_dir / f"{safe}.json"


def _load_offline(viewer_id: str | None) -> list[dict[str, Any]]:
    path = _viewer_file(viewer_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save_offline(viewer_id: str | None, items: list[dict[str, Any]]) -> None:
    path = _viewer_file(viewer_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _remember_offline(note: str, viewer_id: str | None) -> dict[str, Any]:
    items = _load_offline(viewer_id)
    entry = {
        "memory_id": f"local_{uuid.uuid4().hex[:8]}",
        "content": note.strip(),
        "kind": "user_profile",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    items.append(entry)
    _save_offline(viewer_id, items)
    return {
        "source": "memory_offline",
        "stored": True,
        "scope": _scope(viewer_id),
        "memory": entry,
        "note": "离线模式:偏好已写入本地观众档案;线上由 Foundry 托管记忆自动抽取并去重。",
    }


def _recall_offline(query: str | None, viewer_id: str | None, top: int) -> dict[str, Any]:
    items = _load_offline(viewer_id)
    if query:
        qt = {t.lower() for t in _TOKEN_RE.findall(query)}
        scored = [
            (len(qt & {t.lower() for t in _TOKEN_RE.findall(str(i.get("content", "")))}), i)
            for i in items
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        ranked = [i for s, i in scored if s > 0] or items
    else:
        ranked = items
    results = [
        {"content": i.get("content"), "kind": i.get("kind"), "updated_at": i.get("updated_at")}
        for i in ranked[:top]
    ]
    out: dict[str, Any] = {
        "source": "memory_offline",
        "scope": _scope(viewer_id),
        "count": len(results),
        "memories": results,
    }
    if not results:
        out["note"] = "暂无该观众的偏好记忆。可先用 remember_viewer_preference 记录。"
    return out


# ── Live store (Foundry managed memory) ──────────────────────────────


@lru_cache(maxsize=1)
def _project_client():
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    settings = get_settings()
    return AIProjectClient(
        endpoint=settings.project_endpoint, credential=DefaultAzureCredential()
    )


def _remember_live(note: str, viewer_id: str | None) -> dict[str, Any]:
    settings = get_settings()
    client = _project_client()
    poller = client.beta.memory_stores.begin_update_memories(
        settings.memory_store_name,
        scope=_scope(viewer_id),
        items=[{"role": "user", "type": "message", "content": note.strip()}],
        update_delay=0,  # process immediately instead of the 300s debounce default
    )
    result = poller.result()
    update_id = getattr(result, "update_id", None) or getattr(result, "id", None)
    return {
        "source": "memory_live",
        "stored": True,
        "scope": _scope(viewer_id),
        "update_id": update_id,
        "note": "已交由 Foundry 托管记忆抽取并保存为用户画像。",
    }


def _recall_live(query: str | None, viewer_id: str | None, top: int) -> dict[str, Any]:
    from azure.ai.projects.models import MemorySearchOptions

    settings = get_settings()
    client = _project_client()
    search_text = query or "该观众的观影偏好:喜欢的球队/运动、偏好语言、喜欢或不喜欢的节目类型"
    result = client.beta.memory_stores.search_memories(
        settings.memory_store_name,
        scope=_scope(viewer_id),
        items=search_text,
        options=MemorySearchOptions(max_memories=top),
    )
    memories: list[dict[str, Any]] = []
    for item in getattr(result, "memories", None) or []:
        mem = getattr(item, "memory_item", None)
        if mem is None:
            continue
        memories.append(
            {
                "content": getattr(mem, "content", None),
                "kind": str(getattr(mem, "kind", "")),
                "updated_at": str(getattr(mem, "updated_at", "")),
            }
        )
    out: dict[str, Any] = {
        "source": "memory_live",
        "scope": _scope(viewer_id),
        "count": len(memories),
        "memories": memories,
        "search_id": getattr(result, "search_id", None),
    }
    if not memories:
        out["note"] = "该观众暂无可召回的偏好记忆。"
    return out


# ── Tools ────────────────────────────────────────────────────────────


@tool(approval_mode="never_require")
def remember_viewer_preference(
    note: Annotated[
        str,
        "要记住的观众偏好,自然语言。例如 '喜欢皇家马德里和西甲' 或 '偏好中文解说、不爱看拳击'。",
    ],
    viewer_id: Annotated[
        str | None, "观众/家庭档案 ID,可选;不传则使用默认演示观众。"
    ] = None,
) -> str:
    """记住观众的观影偏好(喜欢的球队/运动、语言、节目类型),用于跨会话个性化。

    当用户表达喜好时调用,例如"我是皇马球迷""以后多给我推荐网球""我喜欢中文解说"。
    偏好会持久化(线上为 Foundry 托管记忆),下次会话可用 `recall_viewer_preferences` 召回。
    """
    settings = get_settings()
    try:
        if settings.memory_live:
            result = _remember_live(note, viewer_id)
        else:
            result = _remember_offline(note, viewer_id)
    except Exception as exc:  # noqa: BLE001 — fall back to offline on any live error
        result = _remember_offline(note, viewer_id)
        result["live_error"] = str(exc)
    return json.dumps(result, ensure_ascii=False)


@tool(approval_mode="never_require")
def recall_viewer_preferences(
    query: Annotated[
        str | None,
        "可选的检索意图,例如 '今晚体育推荐' 或 '喜欢的语言'。不传则返回全部已知偏好。",
    ] = None,
    viewer_id: Annotated[
        str | None, "观众/家庭档案 ID,可选;不传则使用默认演示观众。"
    ] = None,
) -> str:
    """召回该观众已保存的观影偏好,用于个性化推荐。

    做个性化推荐、或用户问"根据我的偏好""我之前说过喜欢什么""还记得我吗"时先调用本工具,
    再结合 `foundry_iq_search` / `query_schedule` 等给出贴合偏好的答复。
    """
    settings = get_settings()
    top = max(1, min(int(settings.memory_max_recall or 5), 10))
    try:
        if settings.memory_live:
            result = _recall_live(query, viewer_id, top)
        else:
            result = _recall_offline(query, viewer_id, top)
    except Exception as exc:  # noqa: BLE001 — fall back to offline on any live error
        result = _recall_offline(query, viewer_id, top)
        result["live_error"] = str(exc)
    return json.dumps(result, ensure_ascii=False)
