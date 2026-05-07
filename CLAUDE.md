# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Note: a `CLAUDE.md` exists in an ancestor directory (`/home/minggu/projects_code/CLAUDE.md`) describing an unrelated retail-forecasting project. Ignore it — this repo has nothing to do with that system.

## Repository purpose

Design-only repo for the **Hisense TV Sports AI Assistant** customer engagement. Users press the remote's AI key and ask sports questions in natural language (EPG schedule, live scores, player/team knowledge); the system answers in 3–5s via an Azure Foundry Agent orchestrating Postgres + Azure AI Search + sports APIs.

There is **no application code** in this repo yet — only Chinese-language design documents and one sample EPG payload. Do not fabricate build/test/lint commands; there are none.

## Repo contents

- `HLD_海信TV体育AI助手_高阶设计.md` — High-level design. Architecture, data-layer split rationale (Postgres as System of Record + AI Search as derived semantic index), ingest pipeline, Agent tool set, latency/cost/ops targets, review checklist.
- `LLD_海信TV体育AI助手_详细设计.md` — Low-level design. Production-ready Postgres DDL, Azure AI Search index JSON, ingest pipeline pseudocode, entity-linker logic, Agent tool JSON schemas, API contract, Redis key conventions, observability fields, test plan.
- `1.2026-04-14.json` — One-day EPG sample from the upstream feed (DR1 Denmark: 33 programs / 36 listings, multi-language titles DA/EN/SV/NO, no sports items). This is the shape the daily ingest pipeline must consume. Used as the ground-truth schema reference when designing the data layer.

The LLD is the authoritative source for implementation details; the HLD sets direction and trade-offs. Keep them consistent — any LLD change that contradicts an HLD decision should update both.

## Working in this repo

### When editing design docs
- Docs are in **Simplified Chinese**. Match the existing tone and terminology (e.g. 节目 / 排播 / 频道 / 实体链接器). Don't translate existing Chinese sections to English.
- The LLD's Postgres DDL and AI Search JSON are meant to be executable as-is — preserve syntactic validity when editing (no placeholder SQL, no invalid constraint forms).
- When changing data-layer behavior (schema, UPSERT logic, tombstone rules, embedding triggers), update both the HLD data-layer section and the LLD DDL/pipeline section in the same commit.

### Review workflow
The docs have been iterated via `/codex:review` (Codex-based reviewer agent). Two review cycles have already been applied and committed — see `git log` for the "Codex review fixes" commits. When making non-trivial changes, run `/codex:review` before committing.

### Known sharp edges baked into the design
These are subtle decisions a future editor might accidentally regress — preserve them unless explicitly revisiting:

- **Postgres is System of Record; AI Search is derived.** Any field AI Search needs must first exist in Postgres. The index can always be rebuilt from Postgres.
- **AI Search doc granularity = canonical `title_id` with `doc_type` discriminator (`movie` / `series` / `episode`), not `program_id` and not `listing_id`.** Same index, filter by `doc_type`. Repeats of the same title share one embedding. Schedule info lives in Postgres only. (The v0.1 design keyed docs on `program_id`; that was replaced in v0.2 — do not revert.)
- **UPSERT for `listings` splits fields by semantics.** Content fields are gated by `source_updated_at` (skip stale upstream writes); existence fields (`status`, `last_seen_batch`, `tombstoned_at=NULL`) must refresh on every batch regardless, so snapshot reconciliation stays correct. See LLD §2 UPSERT and §4 pipeline.
- **Snapshot reconciliation uses tombstones.** Listings that disappear from the daily batch are soft-deleted (`status='removed'`, `tombstoned_at=now()`). Queries default to `WHERE status='active'`. Partial indexes include this predicate.
- **Entity-linker re-runs are broader than "new rows only"** — they fire on content change, low-confidence previous runs, and upstream correction. The predicate compares `listings.listing_content_hash` (not `programs.content_hash`). The linker must also fire on titles whose *metadata did not change but a listing was added/modified* (pure EPG additions) — see `post_merge_pipeline` in LLD §4.1.
- **Agent tool `query_schedule` accepts sport predicates** (`sport`, `competition`, `team`, `has_match_id`) and returns `match_external_id` so `get_live_scores` can be called directly without a second lookup. Maximum tool-call depth is 3.
- **`titles` is the canonical entity; `source_records` holds per-source raw snapshots; `programs` is a simpleTV-side instance with a `title_id` FK.** Never add `source_id` columns to `programs` — that was explicitly rejected (HLD §10 decision #2) because a single entity can have both IMDB and simpleTV source rows simultaneously (3NF violation). Field-level merges happen via `compute_merged(title_id)` under `titles.merge_version` optimistic CAS. VOD doesn't go through `source_records`; it writes `vod_assets` and only materializes the `titles.vod_playable` flag.
- **Dedupe is staged: `imdb_id` exact match → `pg_trgm` fuzzy on `normalized_title` (threshold 0.85, same `kind`, `release_year ±1`) → new title.** 0.75–0.85 is the manual-review band (pushed to Service Bus, does not auto-merge). Do not change the thresholds without running the 200-pair benchmark in LLD §10.3; they were chosen to catch "Fellowship of the Ring" synonyms without merging "冰雪奇缘 1" with "冰雪奇缘 2".
- **tvSeries RAG docs live in the same AI Search index as movies/episodes, differentiated by `doc_type`.** Single index, not multiple. Series docs are rebuilt only when one of four conditions fires: new season, long-description first appearance, series metadata drift (cast/genres/rating), or periodic fallback (≥10 episodes or ≥90 days since last rebuild). `rag_doc_version` increments on each rebuild for audit/rollback. Never rebuild on every episode arrival — that blows up the embedding bill and tvSeries rebuild has an LLM-summarization step (`gpt-5-mini` to ~500 chars).
- **Pipeline order (v0.2) for IMDB + simpleTV paths: `source_records UPSERT → resolve_title → compute_merged → programs/listings UPSERT (simpleTV only) → reconciliation → sport linker → tvSeries rebuild decision → embed → AI Search push`.** The sport linker now runs on merged `titles.primary_title`/`genres_merged`, not single-source programs, so do not move it earlier. VOD has its own lightweight path: `vod_assets UPSERT → refresh_vod_playable → patch_search_playable` (no text re-embed, just flag update).

## Environment

- Branch strategy: work on `master`; remote is `origin` → `https://github.com/guming3d/hisence_TV_retrieval.git` (private).
- A `.venv/` is present but there is no Python code in the repo yet; it's empty scaffolding from the customer workspace.
