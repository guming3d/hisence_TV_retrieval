# Copyright (c) Microsoft. All rights reserved.
"""Foundry IQ tool — knowledge retrieval over the production program library.

Demonstrates **Foundry IQ** (Microsoft Foundry's unified knowledge layer /
agentic retrieval) as the RAG source for the Hisense assistant: semantic
program search, content recommendation, "那部关于…的纪录片是什么" style
questions. The knowledge base is built from this repo's real EPG/titles
sample (``1.2026-04-14.json`` shaped into ``movie`` / ``series`` / ``episode``
docs by ``scripts/build_kb_docs.py``), matching the LLD ``doc_type`` design.

Two execution paths:

* **live** — calls a Foundry IQ knowledge base (agentic retrieval) via the
  Azure AI Search *knowledge agent retrieve* API that powers Foundry IQ. Gated
  by ``FOUNDRY_IQ_ENDPOINT`` + ``FOUNDRY_IQ_KNOWLEDGE_BASE``.
* **offline** — a dependency-free local retriever scores the generated KB docs
  by keyword overlap so the tool returns real content from the sample data with
  zero cloud setup. This is what makes the POC runnable on a laptop.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Annotated, Any

from langchain_core.tools import tool

from config import get_settings

_TOKEN_RE = re.compile(r"[\w\u00c0-\u024f\u4e00-\u9fff]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


@lru_cache(maxsize=1)
def _load_kb_docs() -> list[dict[str, Any]]:
    """Load the generated knowledge-base docs from ``data/kb/``.

    Reads ``kb_docs.jsonl`` (one JSON doc per line) produced by
    ``scripts/build_kb_docs.py``. Returns an empty list if the file is missing,
    which the caller turns into a friendly "knowledge base not built yet"
    message.
    """
    kb_file = get_settings().kb_dir / "kb_docs.jsonl"
    if not kb_file.exists():
        return []
    docs: list[dict[str, Any]] = []
    for line in kb_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        doc["_tokens"] = _tokenize(
            " ".join(
                str(doc.get(f, ""))
                for f in ("title", "original_title", "content", "genres", "keywords", "channel")
            )
        )
        docs.append(doc)
    return docs


def _score(query_tokens: list[str], doc: dict[str, Any]) -> float:
    if not query_tokens:
        return 0.0
    doc_tokens = doc.get("_tokens", [])
    if not doc_tokens:
        return 0.0
    doc_set = set(doc_tokens)
    # Overlap + light frequency weighting; title hits count double.
    title_tokens = set(_tokenize(str(doc.get("title", "")) + " " + str(doc.get("original_title", ""))))
    score = 0.0
    for qt in query_tokens:
        if qt in title_tokens:
            score += 2.0
        elif qt in doc_set:
            score += 1.0
    return score


def _retrieve_offline(query: str, doc_type: str | None, top: int) -> dict[str, Any]:
    docs = _load_kb_docs()
    if not docs:
        return {
            "source": "foundry_iq_offline",
            "count": 0,
            "note": "知识库尚未构建。请先运行 scripts/build_kb_docs.py 生成 data/kb/kb_docs.jsonl。",
            "results": [],
        }
    qt = _tokenize(query)
    candidates = docs
    if doc_type:
        candidates = [d for d in docs if d.get("doc_type") == doc_type] or docs
    scored = sorted(((_score(qt, d), d) for d in candidates), key=lambda x: x[0], reverse=True)
    results = []
    for score, doc in scored[:top]:
        if score <= 0:
            continue
        results.append(
            {
                "title_id": doc.get("title_id"),
                "doc_type": doc.get("doc_type"),
                "title": doc.get("title"),
                "original_title": doc.get("original_title"),
                "genres": doc.get("genres"),
                "channel": doc.get("channel"),
                "release_year": doc.get("release_year"),
                "imdb_rating": doc.get("imdb_rating"),
                "snippet": (doc.get("content") or "")[:400],
                "score": round(score, 2),
            }
        )
    low_confidence = False
    if not results:
        # No keyword hit (e.g. cross-language gap the offline retriever can't
        # bridge). Return a few series/movies anyway so the demo still grounds
        # an answer; live Foundry IQ would rank these by multilingual embeddings.
        low_confidence = True
        fallback = [d for d in candidates if d.get("doc_type") in ("series", "movie")][:top]
        for doc in fallback or candidates[:top]:
            results.append(
                {
                    "title_id": doc.get("title_id"),
                    "doc_type": doc.get("doc_type"),
                    "title": doc.get("title"),
                    "original_title": doc.get("original_title"),
                    "genres": doc.get("genres"),
                    "channel": doc.get("channel"),
                    "release_year": doc.get("release_year"),
                    "imdb_rating": doc.get("imdb_rating"),
                    "snippet": (doc.get("content") or "")[:400],
                    "score": 0.0,
                }
            )
    out: dict[str, Any] = {
        "source": "foundry_iq_offline",
        "count": len(results),
        "results": results,
    }
    if low_confidence:
        out["low_confidence"] = True
        out["note"] = "离线关键词检索未命中,返回示例候选;线上 Foundry IQ 使用多语向量检索。"
    return out


def _retrieve_live(query: str, doc_type: str | None, top: int) -> dict[str, Any]:
    """Call the Foundry IQ knowledge base via the AI Search agentic-retrieval API.

    Foundry IQ exposes agentic retrieval over an Azure AI Search *knowledge base*
    (GA REST ``2026-04-01``). We POST the query to ``/knowledgebases/{kb}/retrieve``
    with an Entra bearer token (the deployed agent's managed identity, granted
    *Search Index Data Reader* on the search service). Endpoint + knowledge-base
    name come from env (``FOUNDRY_IQ_ENDPOINT`` / ``FOUNDRY_IQ_KNOWLEDGE_BASE``).

    The GA response carries grounding docs as a JSON array inside
    ``response[].content[].text``; ``references`` (when requested) holds the
    per-source documents that contributed. We parse both into structured results
    the model can cite. ``doc_type`` is applied as a post-filter hint (GA retrieve
    doesn't expose a query-time index filter).
    """
    import requests
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    settings = get_settings()
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://search.azure.com/.default"
    )
    endpoint = settings.foundry_iq_endpoint or settings.project_endpoint
    kb = settings.foundry_iq_knowledge_base
    ks = settings.foundry_iq_knowledge_source
    url = (
        f"{endpoint.rstrip('/')}/knowledgebases/{kb}/retrieve"
        f"?api-version=2026-04-01"
    )
    body: dict[str, Any] = {
        "intents": [{"type": "semantic", "search": query}],
    }
    if ks:
        body["knowledgeSourceParams"] = [
            {
                "knowledgeSourceName": ks,
                "kind": "searchIndex",
                "includeReferences": True,
                "includeReferenceSourceData": True,
            }
        ]
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token_provider()}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body),
        timeout=30.0,
    )
    resp.raise_for_status()
    return _parse_live_response(resp.json(), doc_type, top)


def _parse_live_response(data: dict[str, Any], doc_type: str | None, top: int) -> dict[str, Any]:
    """Shape a GA ``2026-04-01`` retrieve response into the tool's result format.

    Grounding docs arrive as a JSON array embedded in ``response[].content[].text``
    (``{ref_id, title, terms, content}``). When ``references`` are requested they
    carry the index source-data fields (``title_id``, ``doc_type`` …) which we
    merge onto the grounding rows by ``ref_id`` for richer citations.
    """
    grounding: list[dict[str, Any]] = []
    for msg in data.get("response") or []:
        for chunk in (msg.get("content") if isinstance(msg, dict) else None) or []:
            text = chunk.get("text") if isinstance(chunk, dict) else None
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                grounding.append({"content": text})
                continue
            if isinstance(parsed, list):
                grounding.extend(d for d in parsed if isinstance(d, dict))
            elif isinstance(parsed, dict):
                grounding.append(parsed)

    ref_by_id: dict[Any, dict[str, Any]] = {}
    for ref in data.get("references") or []:
        if not isinstance(ref, dict):
            continue
        rid = ref.get("id", ref.get("refId"))
        src = ref.get("sourceData") if isinstance(ref.get("sourceData"), dict) else ref
        if rid is not None:
            ref_by_id[rid] = src

    results: list[dict[str, Any]] = []
    for item in grounding:
        merged = dict(item)
        ref = ref_by_id.get(item.get("ref_id"))
        if ref:
            for key in (
                "title_id", "doc_type", "original_title", "genres", "channel",
                "release_year", "imdb_rating",
            ):
                if key in ref and key not in merged:
                    merged[key] = ref[key]
        results.append(merged)

    if not results and ref_by_id:
        results = list(ref_by_id.values())

    if doc_type:
        filtered = [r for r in results if r.get("doc_type") == doc_type]
        if filtered:
            results = filtered

    results = results[:top]
    return {
        "source": "foundry_iq_live",
        "count": len(results),
        "results": results,
    }


@tool
def foundry_iq_search(
    query: Annotated[str, "节目内容/推荐类自然语言查询,例如 '关于丹麦收养的纪录片'。"],
    doc_type: Annotated[
        str | None,
        "可选过滤:'movie' / 'series' / 'episode'。不确定时留空。",
    ] = None,
    top: Annotated[int, "返回条数(1-8)。"] = 5,
) -> str:
    """检索生产节目库进行内容推荐 / 语义搜索(Foundry IQ 知识库)。

    用于"推荐一部…""那部讲…的节目是什么""有没有关于…的纪录片"等内容类问题。
    返回节目标题、类型、简介与来源,回答时请引用节目标题作为依据。
    """
    settings = get_settings()
    top = max(1, min(int(top), 8))
    norm_type = doc_type if doc_type in {"movie", "series", "episode"} else None
    try:
        if settings.foundry_iq_live:
            result = _retrieve_live(query, norm_type, top)
        else:
            result = _retrieve_offline(query, norm_type, top)
    except Exception as exc:  # noqa: BLE001 — fall back to offline on any live error
        result = _retrieve_offline(query, norm_type, top)
        result["live_error"] = str(exc)
    return json.dumps(result, ensure_ascii=False)
