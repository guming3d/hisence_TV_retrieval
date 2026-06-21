# Copyright (c) Microsoft. All rights reserved.
"""Build Foundry IQ knowledge-base docs + EPG schedule from the sample feed.

Transforms the upstream EPG payload (``1.2026-04-14.json`` at the repo root)
into the two local data files the POC tools consume:

* ``poc/data/kb/kb_docs.jsonl`` — one knowledge doc per line, keyed by canonical
  ``title_id`` with a ``doc_type`` discriminator (``movie`` / ``series`` /
  ``episode``), exactly as the LLD specifies for the AI Search index. Docs are
  **schedule-free**: they carry semantic content (titles, descriptions, genres,
  ratings) for retrieval/recommendation, not airing times.
* ``poc/data/kb/schedule.json`` — listings-derived EPG (channel, start/end,
  live/rerun, match id) that the ``query_schedule`` scenario tool reads. In
  production this is a Postgres query; here a local JSON stands in for it.

Run::

    python poc/scripts/build_kb_docs.py

Idempotent — safe to re-run; it overwrites both output files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "1.2026-04-14.json"
KB_DIR = REPO_ROOT / "poc" / "data" / "kb"


def _pick_title(titles: list[dict[str, Any]], prefer: tuple[str, ...]) -> str | None:
    for kind in prefer:
        for t in titles:
            if t.get("kind") == kind and t.get("text"):
                return t["text"].strip()
    for t in titles:
        if t.get("text"):
            return t["text"].strip()
    return None


def _descriptions(program: dict[str, Any], kinds: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    descs = program.get("descriptions", [])
    # Prefer the requested kinds, longest first, de-duplicated.
    for kind in kinds:
        for length in ("long", "short"):
            for d in descs:
                if d.get("kind") == kind and d.get("length") == length and d.get("text"):
                    txt = d["text"].strip()
                    if txt and txt not in out:
                        out.append(txt)
    return out


def _genre_names(program: dict[str, Any]) -> list[str]:
    return [g["name"] for g in program.get("genres", []) if g.get("name")]


# EN genre/category → Chinese synonyms, so Chinese user queries match the
# Danish/English production data offline (live Foundry IQ does this via
# multilingual embeddings; offline we bridge it with explicit synonyms).
_ZH_SYNONYMS: dict[str, list[str]] = {
    "News": ["新闻", "资讯", "时事"],
    "Documentary": ["纪录片", "纪实"],
    "Drama": ["剧情", "电视剧", "剧集"],
    "Crime": ["犯罪"],
    "Crime-Thriller": ["犯罪", "惊悚"],
    "Murder Mystery": ["悬疑", "谋杀", "推理"],
    "Entertainment": ["娱乐", "综艺"],
    "History": ["历史"],
    "Politics": ["政治", "时政"],
    "Reality": ["真人秀", "真人实境"],
    "Docusoap": ["纪实真人秀"],
    "Talk-Show": ["脱口秀", "访谈"],
    "Game-Show": ["游戏节目", "竞猜"],
    "Travel": ["旅行", "旅游"],
    "Weather": ["天气", "气象"],
    "Lifestyle": ["生活", "生活方式"],
    "Leisure": ["休闲"],
    "Gardening": ["园艺"],
    "Fishing": ["钓鱼"],
    "Antiques": ["古董", "收藏"],
    "House": ["家居"],
    "Sport": ["体育", "运动"],
    "Series": ["剧集", "连续剧"],
    "Miscellaneous": ["综合"],
}


def _zh_terms(genres: list[str], category: str | None) -> list[str]:
    terms: list[str] = []
    for name in genres + ([category] if category else []):
        for zh in _ZH_SYNONYMS.get(name, []):
            if zh not in terms:
                terms.append(zh)
    return terms


def _build_content(title: str | None, descs: list[str], genres: list[str],
                   category: str | None, attrs: dict[str, Any]) -> str:
    parts: list[str] = []
    if title:
        parts.append(title)
    if category:
        parts.append(f"类别:{category}")
    if genres:
        parts.append("类型:" + "、".join(genres))
    zh_terms = _zh_terms(genres, category)
    if zh_terms:
        parts.append("中文类型:" + "、".join(zh_terms))
    countries = [c.get("name") for c in attrs.get("production_countries", []) if c.get("name")]
    if countries:
        parts.append("制作国家/地区:" + "、".join(countries))
    if attrs.get("production_company"):
        parts.append("制作方:" + str(attrs["production_company"]))
    parts.extend(descs)
    return "\n".join(parts)


def build() -> None:
    if not SOURCE.exists():
        raise SystemExit(f"Source feed not found: {SOURCE}")

    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    programs: list[dict[str, Any]] = data.get("programs", [])
    listings: list[dict[str, Any]] = data.get("listings", [])
    channels = {c["id"]: c for c in data.get("channels", [])}

    programs_by_id = {p["id"]: p for p in programs}

    # Channel name a program airs on (from listings), for recommendation context.
    program_channel: dict[int, str] = {}
    for l in listings:
        pid = l.get("program_id")
        ch = channels.get(l.get("channel_id"), {})
        if pid is not None and ch.get("name"):
            program_channel.setdefault(pid, ch["name"])

    docs: dict[str, dict[str, Any]] = {}
    series_episode_count: dict[str, int] = {}

    for p in programs:
        pid = p["id"]
        kind = p.get("kind")
        attrs = p.get("attributes", {}) or {}
        genres = _genre_names(p)
        category = p.get("category")
        channel = program_channel.get(pid)
        imdb_rating = (p.get("ratings") or {}).get("imdb")
        release_year = attrs.get("release_year")
        zh = _zh_terms(genres, category)
        kw = list(dict.fromkeys(genres + ([category] if category else []) + zh))

        if kind == "movie":
            title = _pick_title(p.get("titles", []), ("original", "title"))
            descs = _descriptions(p, ("show", "episode"))
            doc_id = f"movie-{pid}"
            docs[doc_id] = {
                "title_id": doc_id,
                "doc_type": "movie",
                "title": title,
                "original_title": _pick_title(p.get("titles", []), ("original",)),
                "genres": genres,
                "keywords": kw,
                "channel": channel,
                "release_year": release_year,
                "imdb_rating": imdb_rating,
                "content": _build_content(title, descs, genres, category, attrs),
            }
        else:  # episode → one episode doc + a deduped series doc
            sid = p.get("series_id") or pid
            series_doc_id = f"series-{sid}"
            ep_title = _pick_title(p.get("titles", []), ("episode_local", "original"))
            series_title = _pick_title(
                p.get("titles", []), ("original", "season_local", "episode_local")
            )
            ep_descs = _descriptions(p, ("episode", "show"))
            series_descs = _descriptions(p, ("show", "episode"))

            ep_doc_id = f"episode-{pid}"
            ep_num = (attrs.get("episode") or {}).get("number")
            season_num = (attrs.get("episode") or {}).get("season")
            docs[ep_doc_id] = {
                "title_id": ep_doc_id,
                "doc_type": "episode",
                "series_id": series_doc_id,
                "title": ep_title,
                "original_title": series_title,
                "season": season_num,
                "episode_number": ep_num,
                "genres": genres,
                "keywords": kw,
                "channel": channel,
                "release_year": release_year,
                "imdb_rating": imdb_rating,
                "content": _build_content(ep_title, ep_descs, genres, category, attrs),
            }

            series_episode_count[series_doc_id] = series_episode_count.get(series_doc_id, 0) + 1
            # First episode seeds the series doc; later episodes only bump count.
            if series_doc_id not in docs:
                docs[series_doc_id] = {
                    "title_id": series_doc_id,
                    "doc_type": "series",
                    "title": series_title,
                    "original_title": series_title,
                    "genres": genres,
                    "keywords": kw,
                    "channel": channel,
                    "release_year": release_year,
                    "imdb_rating": imdb_rating,
                    "content": _build_content(series_title, series_descs, genres, category, attrs),
                }

    for series_doc_id, count in series_episode_count.items():
        if series_doc_id in docs:
            docs[series_doc_id]["episode_count"] = count

    # ── Schedule (listings-derived, schedule lives outside the KB) ──────
    schedule: list[dict[str, Any]] = []
    for l in listings:
        p = programs_by_id.get(l.get("program_id"), {})
        ch = channels.get(l.get("channel_id"), {})
        sched = l.get("schedule", {}) or {}
        qual = l.get("qualifiers", {}) or {}
        bids = l.get("broadcast_ids", {}) or {}
        title = _pick_title(p.get("titles", []), ("original", "episode_local")) if p else None
        bt = l.get("broadcast_titles", [])
        broadcast_title = bt[0]["text"] if bt and bt[0].get("text") else None
        schedule.append(
            {
                "listing_id": l.get("id"),
                "program_id": l.get("program_id"),
                "title": title or broadcast_title,
                "original_title": title,
                "channel": ch.get("name"),
                "start_time": sched.get("start_time"),
                "end_time": sched.get("end_time"),
                "duration": sched.get("duration"),
                "live": bool(qual.get("live")),
                "rerun": bool(qual.get("rerun")),
                "catchup": bool(l.get("catchup")),
                # No sport-match feed in this DR1 sample → null; sport listings
                # would carry a real external id here for get_live_scores.
                "match_external_id": bids.get("event") if p.get("category") == "Sport" else None,
            }
        )

    KB_DIR.mkdir(parents=True, exist_ok=True)
    kb_file = KB_DIR / "kb_docs.jsonl"
    with kb_file.open("w", encoding="utf-8") as f:
        for doc in docs.values():
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    sched_file = KB_DIR / "schedule.json"
    sched_file.write_text(json.dumps(schedule, ensure_ascii=False, indent=2), encoding="utf-8")

    by_type: dict[str, int] = {}
    for doc in docs.values():
        by_type[doc["doc_type"]] = by_type.get(doc["doc_type"], 0) + 1
    print(f"Wrote {len(docs)} KB docs to {kb_file}")
    print(f"  by doc_type: {by_type}")
    print(f"Wrote {len(schedule)} schedule entries to {sched_file}")


if __name__ == "__main__":
    build()
