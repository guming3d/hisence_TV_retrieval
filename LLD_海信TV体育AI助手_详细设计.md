# 海信 TV 体育 AI 助手 — 详细设计 (LLD)

> 版本: v0.2 (多源数据融合 — 供开发实施)
> 日期: 2026-05-07
> 上游文档: `HLD_海信TV体育AI助手_高阶设计.md` (v0.2)
> 读者: 后端 / 数据工程 / ML 工程开发者
> 产出目标: 本文档内容可直接转化为代码、schema 变更、索引配置与 CI 任务
>
> **v0.2 主要变更**:
> - §2 新增 `titles` / `source_records` / `vod_assets` DDL,`programs` 补 `title_id` FK
> - §3 索引粒度由 `program_id` 迁移至 `title_id`,新增 `doc_type`、`vod_playable`、`rag_doc_version` 字段,索引改名 `content-rag-v1`
> - §4 摄入管道由单源扩展为"三源 → Title Resolver → compute_merged → 现有 link/embed"
> - §4.2 新增去重算法,§4.3 新增 tvSeries rebuild 决策,§4.6 嵌入模板按 doc_type 分支
> - §6 Agent 工具 Schema 补 `require_playable` / `doc_type` / `title_id`,新增 `query_vod` 工具
> - §8 Redis 键新增去重候选缓存;§9 指标新增源维度;§10 新增去重/merge/rebuild 测试;§12 任务拆解补里程碑 M2.5 / M3.5

---

## 1. 文档说明

### 1.1 范围
本 LLD 细化 HLD 中 §3 数据层、§4 摄入管道、§5 Agent 编排的实现细节,提供:
- PostgreSQL DDL (可直接执行)
- Azure AI Search 索引 JSON (可直接提交 REST API)
- Agent 工具 JSON Schema (供 Foundry Agent 注册)
- 摄入管道伪代码与关键算法
- 客户端 ↔ 后端 API 契约
- 缓存键规范、可观测性埋点字段

### 1.2 命名约定
| 对象 | 规范 | 示例 |
|---|---|---|
| 表名 | 小写蛇形复数 | `listings`, `sport_events` |
| 列名 | 小写蛇形 | `channel_id`, `accurate_start` |
| 索引 | `ix_<表>_<列组>[_<过滤>]` | `ix_listings_airtime_active` |
| 主键 | `pk_<表>` | `pk_listings` |
| 外键 | `fk_<子表>_<父表>` | `fk_listings_channels` |
| 迁移文件 | `V{YYYYMMDDHHmm}__<desc>.sql` | `V202604250900__init.sql` |
| AI Search 索引 | `content-rag-v{n}` (别名 `content-rag-current`) | `content-rag-v1` |

### 1.3 技术栈锁定
- PostgreSQL **16**, Azure Flexible Server, 扩展: `btree_gist`, `pg_trgm`, `unaccent`, `pgcrypto`
- Azure AI Search (2024-07-01 GA API),Standard S1 (10GB) 起步
- **Azure Event Hubs** (Kafka-compatible endpoint) — simpleTV / VOD 周度消费通道
- Azure Functions (Python 3.11, isolated) — 同时承载 blob-triggered (IMDB) 与 Event Hubs-triggered (simpleTV / VOD) Worker
- Embeddings: `text-embedding-3-large` via Azure OpenAI
- Agent: Azure AI Foundry Agent Service,`gpt-5-mini` 主模型,`gpt-4o` 用于 tvSeries 系列摘要兜底

---

## 2. PostgreSQL 详细设计

### 2.0 数据源与字段归属

| 源 | `source_id` | 交付通道 | 频率 | 主要字段 | 落入的表 |
|---|---|---|---|---|---|
| IMDB | `'imdb'` | ADLS blob trigger `/raw/imdb/{batch_id}.json` | 手动,月/季 | primary_title、description_long、imdb_rating、cast、genres、release_year、kind、series_id | `source_records` (source='imdb') → `compute_merged` → `titles` |
| simpleTV | `'simpletv'` | Event Hubs `simpletv.epg.weekly` | 每周 | 频道、排播、多语言 titles/descriptions、live/rerun/catchup、broadcast_ids | `source_records` (source='simpletv') + `channels` + `programs` + `listings` |
| VOD | `'vod'` | Event Hubs `vod.catalog.weekly` | 每周 | asset_url、drm、resolution、available_from/to、vod_external_id | `vod_assets` (不落 source_records;VOD 只影响播放维度,不参与规范实体合并) |

**为什么 VOD 不进 source_records**: VOD 不贡献规范实体的元数据字段 (title/description/rating 等),仅承载"能否播放"维度。将其独立建表 `vod_assets`,然后物化到 `titles.vod_playable` 标志,避免污染 merge 逻辑。

### 2.1 DDL (生产可执行)

```sql
-- =============================================================
-- V202604250900__init.sql  (v0.1: channels/programs/listings/sport_events)
-- V202605070900__multi_source.sql  (v0.2: titles/source_records/vod_assets + programs.title_id)
-- 以下 DDL 为合并后的完整建表语句,便于评审
-- =============================================================
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- ★ v0.2: compute_merged 内 digest() 需要

-- 枚举类型 -----------------------------------------------------
CREATE TYPE listing_status AS ENUM ('active', 'removed');
CREATE TYPE program_kind   AS ENUM ('episode', 'movie', 'show', 'clip', 'series', 'other');
    -- ★ v0.2 新增 'series':titles 表规范实体 (kind='series') 承载系列级 RAG 文档;
    --   programs (单源节目实例) 仍不会写入 'series',保持原语义
CREATE TYPE source_id      AS ENUM ('imdb', 'simpletv');   -- VOD 另行, 见 vod_assets
CREATE TYPE rag_doc_type   AS ENUM ('movie', 'series', 'episode');

-- 规范实体 (Canonical Title) ---------------------------------
CREATE TABLE titles (
    id                   BIGSERIAL      PRIMARY KEY,
    imdb_id              TEXT,                              -- 首选规范 id, 可空 (孤儿 simpleTV)
    primary_title        TEXT           NOT NULL,           -- merged, IMDB > simpleTV
    normalized_title     TEXT           NOT NULL,           -- lower + unaccent + 去标点, 用于模糊查重
    kind                 program_kind   NOT NULL DEFAULT 'other',
    release_year         SMALLINT,
    series_id            BIGINT,                            -- tvSeries 聚合键, 对应 series_doc 主键
    genres_merged        JSONB          NOT NULL DEFAULT '[]'::jsonb,
    cast_merged          JSONB          NOT NULL DEFAULT '[]'::jsonb,
    imdb_rating          NUMERIC(3,1),
    vod_playable         BOOLEAN        NOT NULL DEFAULT FALSE,
    rag_doc_version      INTEGER        NOT NULL DEFAULT 0, -- series rebuild 计数器
    content_hash         CHAR(64)       NOT NULL,           -- merged-fields hash -> 驱动 AI Search 重嵌入
    merge_version        INTEGER        NOT NULL DEFAULT 0, -- 乐观锁 CAS
    updated_at           TIMESTAMPTZ    NOT NULL DEFAULT now()
);

-- 去重 Stage 1: imdb_id 精确匹配
CREATE UNIQUE INDEX ix_titles_imdb_unique
    ON titles (imdb_id) WHERE imdb_id IS NOT NULL;

-- 去重 Stage 2: pg_trgm 模糊匹配 (normalized_title)
CREATE INDEX ix_titles_normalized_trgm
    ON titles USING GIN (normalized_title gin_trgm_ops);

-- tvSeries 聚合
CREATE INDEX ix_titles_series
    ON titles (series_id) WHERE series_id IS NOT NULL;

-- 可播放过滤热路径
CREATE INDEX ix_titles_playable
    ON titles (vod_playable) WHERE vod_playable = TRUE;

-- 每源原样快照 (字段级 merge 的原料) ---------------------------
CREATE TABLE source_records (
    source               source_id      NOT NULL,
    source_external_id   TEXT           NOT NULL,           -- IMDB tt-id 或 simpleTV program_id
    title_id             BIGINT         REFERENCES titles(id) ON DELETE CASCADE,
    raw_payload          JSONB          NOT NULL,
    source_updated_at    TIMESTAMPTZ    NOT NULL,
    linker_metadata      JSONB,                             -- 本源带来的附加信息 (如 simpleTV 的 broadcast_ids)
    ingested_at          TIMESTAMPTZ    NOT NULL DEFAULT now(),
    status               TEXT           NOT NULL DEFAULT 'active',  -- 'active' | 'superseded'
    PRIMARY KEY (source, source_external_id)
);

CREATE INDEX ix_source_records_title  ON source_records(title_id);
CREATE INDEX ix_source_records_source ON source_records(source, source_updated_at DESC);

-- 频道 ---------------------------------------------------------
CREATE TABLE channels (
    id              INTEGER       PRIMARY KEY,
    name            TEXT          NOT NULL,
    kind            TEXT,                               -- regular / radio / ...
    country         CHAR(2)       NOT NULL,             -- ISO-3166 alpha-2
    language        CHAR(2)       NOT NULL,             -- ISO-639-1
    logo_url        TEXT,
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX ix_channels_country ON channels(country);

-- 节目 (Program) -----------------------------------------------
CREATE TABLE programs (
    id                   BIGINT        PRIMARY KEY,
    title_id             BIGINT        REFERENCES titles(id),  -- ★ v0.2 新增:指向规范实体
    series_id            BIGINT,
    season_id            BIGINT,
    imdb_id              TEXT,
    kind                 program_kind  NOT NULL DEFAULT 'other',
    category             TEXT,                           -- News / Sports / ...
    release_year         SMALLINT,
    duration_sec         INTEGER,
    imdb_rating          NUMERIC(3,1),
    production_company   TEXT,
    production_countries JSONB         NOT NULL DEFAULT '[]'::jsonb,
    titles               JSONB         NOT NULL DEFAULT '[]'::jsonb,
        -- [{kind, text, language}] 完整存档,供 LLD 之后检索/降级使用
    descriptions         JSONB         NOT NULL DEFAULT '[]'::jsonb,
        -- [{length, kind, language, text}]
    images               JSONB         NOT NULL DEFAULT '[]'::jsonb,
    attributes           JSONB         NOT NULL DEFAULT '{}'::jsonb,
        -- 保留原始 attributes,供字段演进
    content_hash         CHAR(64)      NOT NULL,
        -- sha256(title_original || description_long_show || description_long_episode)
        -- 变化检测器用它判断是否需要重新嵌入
    source_updated_at    TIMESTAMPTZ,                     -- 源 JSON 的 updated_at
    updated_at           TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX ix_programs_series_season ON programs(series_id, season_id);
CREATE INDEX ix_programs_imdb          ON programs(imdb_id) WHERE imdb_id IS NOT NULL;
CREATE INDEX ix_programs_category      ON programs(category);
CREATE INDEX ix_programs_updated_at    ON programs(updated_at);

-- 类别多对多 ---------------------------------------------------
CREATE TABLE program_genres (
    program_id  BIGINT NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
    genre_id    INTEGER NOT NULL,
    genre_name  TEXT    NOT NULL,
    PRIMARY KEY (program_id, genre_id)
);
CREATE INDEX ix_program_genres_name ON program_genres(genre_name);

-- 排播 (Listing) ------------------------------------------------
CREATE TABLE listings (
    id                  BIGINT           PRIMARY KEY,
    program_id          BIGINT           NOT NULL REFERENCES programs(id),
    channel_id          INTEGER          NOT NULL REFERENCES channels(id),
    start_time          TIMESTAMPTZ      NOT NULL,
    end_time            TIMESTAMPTZ      NOT NULL,
    accurate_start      TIMESTAMPTZ,
    accurate_end        TIMESTAMPTZ,
    airtime             TSTZRANGE        GENERATED ALWAYS AS (
                            tstzrange(
                                COALESCE(accurate_start, start_time),
                                COALESCE(accurate_end,   end_time),
                                '[)'
                            )
                        ) STORED,
    is_live             BOOLEAN          NOT NULL DEFAULT FALSE,
    is_rerun            BOOLEAN          NOT NULL DEFAULT FALSE,
    catchup             BOOLEAN          NOT NULL DEFAULT FALSE,
    broadcast_event_id  TEXT,
    broadcast_ids       JSONB            NOT NULL DEFAULT '{}'::jsonb,
    listing_content_hash CHAR(64)        NOT NULL,
        -- sha256(program_id || start_time || end_time || accurate_* ||
        --        broadcast_event_id || program.title_original ||
        --        program.description_long_episode)
        -- 用于体育实体链接器判断"内容是否变化,需重链"
    status              listing_status   NOT NULL DEFAULT 'active',
    tombstoned_at       TIMESTAMPTZ,
    source_updated_at   TIMESTAMPTZ,
    last_seen_batch     TIMESTAMPTZ      NOT NULL,   -- 本 listing 最后一次出现在摄入批次的时间
    updated_at          TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CONSTRAINT ck_listings_airtime_valid CHECK (end_time > start_time)
);

-- 时间范围索引,限定 active 行,热路径查询走此索引
CREATE INDEX ix_listings_airtime_active
    ON listings USING GIST (airtime)
    WHERE status = 'active';

-- 单频道时间线
CREATE INDEX ix_listings_channel_start_active
    ON listings (channel_id, start_time)
    WHERE status = 'active';

CREATE INDEX ix_listings_program
    ON listings (program_id);

CREATE INDEX ix_listings_is_live_active
    ON listings (is_live)
    WHERE is_live = TRUE AND status = 'active';

CREATE INDEX ix_listings_tombstoned
    ON listings (tombstoned_at)
    WHERE status = 'removed';

CREATE INDEX ix_listings_last_seen
    ON listings (channel_id, last_seen_batch);

-- 体育赛事元数据 (1:1 附加到 listing) --------------------------
CREATE TABLE sport_events (
    listing_id          BIGINT        PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,
    title_id            BIGINT        REFERENCES titles(id),   -- ★ v0.2 新增:冗余列, 便于 title 粒度聚合
    sport               TEXT          NOT NULL,      -- football, basketball, ...
    competition         TEXT,                         -- Superliga, UEFA Champions League
    home_team           TEXT,
    away_team           TEXT,
    teams               JSONB         NOT NULL DEFAULT '[]'::jsonb,
        -- 规范化: [{"name": "...", "external_id": "...", "role": "home|away|neutral"}]
    match_external_id   TEXT,                         -- Sportradar/Opta match id
    linker_version      TEXT          NOT NULL,       -- 'rule-v1.2', 'llm-v0.3'
    linker_confidence   NUMERIC(3,2)  NOT NULL CHECK (linker_confidence BETWEEN 0 AND 1),
    linked_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    source_content_hash CHAR(64)      NOT NULL
        -- 链接时写入的 listings.listing_content_hash 快照。
        -- 下一批次若 listings.listing_content_hash 与此值不同,需要重链。
);

CREATE INDEX ix_sport_events_match_id   ON sport_events(match_external_id)
    WHERE match_external_id IS NOT NULL;
CREATE INDEX ix_sport_events_team_trgm  ON sport_events USING GIN (
    (home_team || ' ' || COALESCE(away_team,'')) gin_trgm_ops
);
CREATE INDEX ix_sport_events_competition ON sport_events(competition);
CREATE INDEX ix_sport_events_sport       ON sport_events(sport);
CREATE INDEX ix_sport_events_title       ON sport_events(title_id) WHERE title_id IS NOT NULL;

-- VOD 可播放副本 (many-per-title) --------------------------------
CREATE TABLE vod_assets (
    id                   BIGSERIAL      PRIMARY KEY,
    title_id             BIGINT         NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    vod_external_id      TEXT           NOT NULL,
    asset_url            TEXT,
    drm                  TEXT,                              -- 'widevine' | 'fairplay' | 'none' | ...
    resolution           TEXT,                              -- '480p' | '720p' | '1080p' | '4k'
    quality              TEXT,                              -- 'SD' | 'HD' | 'UHD'
    available_from       TIMESTAMPTZ,
    available_to         TIMESTAMPTZ,
    geo_restriction      JSONB          NOT NULL DEFAULT '[]'::jsonb,   -- ISO 国家码白名单
    source_updated_at    TIMESTAMPTZ    NOT NULL,
    updated_at           TIMESTAMPTZ    NOT NULL DEFAULT now(),
    UNIQUE (vod_external_id)
);

CREATE INDEX ix_vod_assets_title        ON vod_assets(title_id);
CREATE INDEX ix_vod_assets_available
    ON vod_assets(title_id, available_from, available_to);

-- 摄入批次审计 -------------------------------------------------
CREATE TABLE ingest_batches (
    id                  BIGSERIAL     PRIMARY KEY,
    source              TEXT          NOT NULL,       -- 'imdb' | 'simpletv' | 'vod'
    source_path         TEXT,                          -- ADLS raw path (IMDB) or 空 (Kafka 批次)
    topic_partition     TEXT,                          -- e.g. 'simpletv.epg.weekly:0' (Kafka 来源)
    country             CHAR(2),                       -- simpleTV/EPG 场景 (IMDB/VOD 为全局批次时可为空)
    channel_id          INTEGER,
    coverage_start      TIMESTAMPTZ,
    coverage_end        TIMESTAMPTZ,
    batch_time          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    source_records_upserted INTEGER   NOT NULL DEFAULT 0,   -- ★ v0.2: 源快照写入行数
    titles_merged       INTEGER       NOT NULL DEFAULT 0,   -- ★ v0.2: compute_merged 产生的 title 更新数
    titles_new          INTEGER       NOT NULL DEFAULT 0,   -- ★ v0.2: 去重后的 canonical title 新增数
    dedupe_hits_imdb    INTEGER       NOT NULL DEFAULT 0,   -- ★ v0.2: Stage 1 命中
    dedupe_hits_fuzzy   INTEGER       NOT NULL DEFAULT 0,   -- ★ v0.2: Stage 2 命中
    programs_upserted   INTEGER       NOT NULL DEFAULT 0,
    listings_upserted   INTEGER       NOT NULL DEFAULT 0,
    listings_tombstoned INTEGER       NOT NULL DEFAULT 0,
    vod_assets_upserted INTEGER       NOT NULL DEFAULT 0,   -- ★ v0.2
    titles_reembedded   INTEGER       NOT NULL DEFAULT 0,   -- ★ v0.2 (取代 programs_reembedded)
    series_rebuilt      INTEGER       NOT NULL DEFAULT 0,   -- ★ v0.2: series_doc rebuild 次数
    sport_events_relinked INTEGER     NOT NULL DEFAULT 0,
    merge_conflicts     INTEGER       NOT NULL DEFAULT 0,   -- ★ v0.2: CAS 冲突重试次数
    status              TEXT          NOT NULL DEFAULT 'success', -- success|partial|failed
    error_detail        TEXT
);
CREATE INDEX ix_ingest_batches_source_time ON ingest_batches(source, batch_time DESC);
CREATE INDEX ix_ingest_batches_channel_time ON ingest_batches(channel_id, batch_time DESC) WHERE channel_id IS NOT NULL;
```

### 2.2 热路径 SQL (供开发直接复用)

**Q1 — "今晚 8 点在 DR1 的是谁"**
```sql
SELECT l.id                  AS listing_id,
       l.program_id,
       c.name                AS channel,
       COALESCE(l.accurate_start, l.start_time) AS start_at,
       COALESCE(l.accurate_end,   l.end_time)   AS end_at,
       l.is_live, l.is_rerun,
       (SELECT text FROM jsonb_to_recordset(p.titles)
               AS t(kind TEXT, text TEXT, language TEXT)
         WHERE kind IN ('original','episode_local') LIMIT 1) AS title,
       se.sport, se.competition, se.teams, se.match_external_id
  FROM listings l
  JOIN channels c        ON c.id = l.channel_id
  JOIN programs p        ON p.id = l.program_id
  LEFT JOIN sport_events se ON se.listing_id = l.id
 WHERE l.status = 'active'
   AND l.channel_id = :channel_id
   AND l.airtime && tstzrange(:t_from, :t_to, '[)')
 ORDER BY start_at
 LIMIT 20;
```

**Q2 — "今晚所有体育比赛"**
```sql
SELECT l.id AS listing_id, l.program_id, c.name AS channel,
       COALESCE(l.accurate_start, l.start_time) AS start_at,
       se.sport, se.competition, se.teams, se.match_external_id,
       l.is_live
  FROM listings l
  JOIN channels c        ON c.id = l.channel_id
  JOIN sport_events se   ON se.listing_id = l.id
 WHERE l.status = 'active'
   AND c.country = :country
   AND l.airtime && tstzrange(:t_from, :t_to, '[)')
 ORDER BY l.is_live DESC, start_at;
```

**Q3 — "皇马的比赛 (模糊)"**
```sql
SELECT l.id, l.program_id, c.name AS channel,
       COALESCE(l.accurate_start, l.start_time) AS start_at,
       se.competition, se.home_team, se.away_team, se.match_external_id
  FROM listings l
  JOIN sport_events se ON se.listing_id = l.id
  JOIN channels c      ON c.id = l.channel_id
 WHERE l.status = 'active'
   AND l.start_time >= now()
   AND (se.home_team ILIKE '%' || :team || '%'
        OR se.away_team ILIKE '%' || :team || '%'
        OR se.teams @> jsonb_build_array(jsonb_build_object('name', :team)))
 ORDER BY start_at
 LIMIT 10;
```

**Q4 — 快照对账 (每批次结束调用一次)**
```sql
-- :batch_time 为本次摄入开始时间
-- :coverage_start/:coverage_end 为本批次覆盖时间窗
UPDATE listings
   SET status        = 'removed',
       tombstoned_at = now(),
       updated_at    = now()
 WHERE channel_id      = :channel_id
   AND status          = 'active'
   AND last_seen_batch < :batch_time
   AND airtime && tstzrange(:coverage_start, :coverage_end, '[)');

-- 硬删除 7 天前的 tombstone
DELETE FROM listings
 WHERE status = 'removed' AND tombstoned_at < now() - INTERVAL '7 days';
```

**Q5 — 去重 Stage 1: imdb_id 精确匹配** (Title Resolver 热路径)
```sql
SELECT id FROM titles WHERE imdb_id = :imdb_id;
-- 索引 ix_titles_imdb_unique,等值 < 1ms
```

**Q6 — 去重 Stage 2: 归一化 title 模糊匹配** (pg_trgm)
```sql
-- :norm = lower(unaccent(strip_punct(title_original)))
-- :kind + 年份约束 (NULL-safe):
--   incoming 有年份 → 候选 release_year IS NULL 或 diff <= 1
--   incoming 无年份 → 不限制候选 release_year
SELECT id,
       similarity(normalized_title, :norm) AS sim
  FROM titles
 WHERE kind = :kind
   AND (:year::INT IS NULL
        OR release_year IS NULL
        OR abs(release_year - :year::INT) <= 1)
   AND normalized_title % :norm              -- pg_trgm operator, 走 ix_titles_normalized_trgm
 ORDER BY sim DESC
 LIMIT 5;

-- 应用侧阈值: sim >= 0.85 直接合并; 0.75 <= sim < 0.85 入人工审核队列;
--            sim < 0.75 视为无匹配, 新建 title。
```

**Q7 — 过滤可播放 + 体育类当晚直播** (内容推荐复合路径)
```sql
SELECT t.id AS title_id, t.primary_title, l.id AS listing_id,
       c.name AS channel,
       COALESCE(l.accurate_start, l.start_time) AS start_at,
       se.competition, se.home_team, se.away_team, se.match_external_id
  FROM titles t
  JOIN programs p          ON p.title_id = t.id
  JOIN listings l          ON l.program_id = p.id
  JOIN channels c          ON c.id = l.channel_id
  LEFT JOIN sport_events se ON se.listing_id = l.id
 WHERE t.vod_playable = TRUE
   AND l.status = 'active'
   AND l.airtime && tstzrange(:t_from, :t_to, '[)')
   AND (se.sport IS NOT NULL OR p.category = 'Sports')
 ORDER BY l.is_live DESC, start_at
 LIMIT 20;
```

### 2.3 UPSERT 范式

所有摄入 UPSERT 统一使用 `ON CONFLICT ... DO UPDATE`,保证幂等 + 乱序安全。

**关键要点**:
- **`last_seen_batch` 与 `status='active'` 永远更新**,不受 `source_updated_at` 守卫限制 —— 否则源端未变的 listing 会在下一次批次中被对账步骤误判为"消失"而错误 tombstone。
- **业务内容字段** (program_id/时间/标志位/broadcast_ids 等) 仅在 `EXCLUDED.source_updated_at` 更新时才覆盖,防止乱序回滚。
- 使用 `CASE WHEN` 在同一条 SQL 内区分两类字段,避免两次往返。

```sql
INSERT INTO listings (id, program_id, channel_id, start_time, end_time,
                      accurate_start, accurate_end, is_live, is_rerun,
                      catchup, broadcast_event_id, broadcast_ids,
                      listing_content_hash,
                      source_updated_at, last_seen_batch, updated_at)
VALUES (...)
ON CONFLICT (id) DO UPDATE SET
       -- 业务内容字段:仅在源更新时覆盖
       program_id        = CASE WHEN EXCLUDED.source_updated_at IS NOT NULL
                                 AND (listings.source_updated_at IS NULL
                                      OR listings.source_updated_at < EXCLUDED.source_updated_at)
                                THEN EXCLUDED.program_id      ELSE listings.program_id      END,
       channel_id        = CASE WHEN EXCLUDED.source_updated_at IS NOT NULL
                                 AND (listings.source_updated_at IS NULL
                                      OR listings.source_updated_at < EXCLUDED.source_updated_at)
                                THEN EXCLUDED.channel_id      ELSE listings.channel_id      END,
       start_time        = CASE WHEN listings.source_updated_at IS NULL
                                 OR listings.source_updated_at < EXCLUDED.source_updated_at
                                THEN EXCLUDED.start_time      ELSE listings.start_time      END,
       end_time          = CASE WHEN listings.source_updated_at IS NULL
                                 OR listings.source_updated_at < EXCLUDED.source_updated_at
                                THEN EXCLUDED.end_time        ELSE listings.end_time        END,
       accurate_start    = CASE WHEN listings.source_updated_at IS NULL
                                 OR listings.source_updated_at < EXCLUDED.source_updated_at
                                THEN EXCLUDED.accurate_start  ELSE listings.accurate_start  END,
       accurate_end      = CASE WHEN listings.source_updated_at IS NULL
                                 OR listings.source_updated_at < EXCLUDED.source_updated_at
                                THEN EXCLUDED.accurate_end    ELSE listings.accurate_end    END,
       is_live           = CASE WHEN listings.source_updated_at IS NULL
                                 OR listings.source_updated_at < EXCLUDED.source_updated_at
                                THEN EXCLUDED.is_live         ELSE listings.is_live         END,
       is_rerun          = CASE WHEN listings.source_updated_at IS NULL
                                 OR listings.source_updated_at < EXCLUDED.source_updated_at
                                THEN EXCLUDED.is_rerun        ELSE listings.is_rerun        END,
       catchup           = CASE WHEN listings.source_updated_at IS NULL
                                 OR listings.source_updated_at < EXCLUDED.source_updated_at
                                THEN EXCLUDED.catchup         ELSE listings.catchup         END,
       broadcast_event_id= CASE WHEN listings.source_updated_at IS NULL
                                 OR listings.source_updated_at < EXCLUDED.source_updated_at
                                THEN EXCLUDED.broadcast_event_id ELSE listings.broadcast_event_id END,
       broadcast_ids     = CASE WHEN listings.source_updated_at IS NULL
                                 OR listings.source_updated_at < EXCLUDED.source_updated_at
                                THEN EXCLUDED.broadcast_ids   ELSE listings.broadcast_ids   END,
       listing_content_hash = CASE WHEN listings.source_updated_at IS NULL
                                    OR listings.source_updated_at < EXCLUDED.source_updated_at
                                   THEN EXCLUDED.listing_content_hash
                                   ELSE listings.listing_content_hash END,
       source_updated_at = GREATEST(listings.source_updated_at, EXCLUDED.source_updated_at),

       -- 存在性字段:每次见到都必须刷新,不受 source_updated_at 守卫
       status            = 'active',       -- 若此前被 tombstone,重新出现即复活
       tombstoned_at     = NULL,
       last_seen_batch   = EXCLUDED.last_seen_batch,
       updated_at        = now();
```

> 说明: programs / channels 的 UPSERT 仅有"业务内容 + 乱序守卫"一类,沿用 `ON CONFLICT ... DO UPDATE WHERE listings.source_updated_at IS NULL OR listings.source_updated_at < EXCLUDED.source_updated_at` 即可。这个"存在性字段每批刷新"的模式只用于 listings。

### 2.3a `source_records` UPSERT

源管道**先**写 `source_records` (无锁,仅按 PK UPSERT),**再**触发 `compute_merged(title_id)` 更新规范实体。这样做有两个好处:
- 三源并发写入互不阻塞 (PK 不同)
- `source_records.raw_payload` 完整留存,支持后期 schema 演进时回放 merge

```sql
INSERT INTO source_records (source, source_external_id, title_id,
                             raw_payload, source_updated_at, linker_metadata)
VALUES (:source, :ext_id, :title_id, :payload, :src_upd, :meta)
ON CONFLICT (source, source_external_id) DO UPDATE SET
       raw_payload       = CASE WHEN source_records.source_updated_at IS NULL
                                 OR source_records.source_updated_at < EXCLUDED.source_updated_at
                                THEN EXCLUDED.raw_payload
                                ELSE source_records.raw_payload END,
       title_id          = COALESCE(EXCLUDED.title_id, source_records.title_id),
       source_updated_at = GREATEST(source_records.source_updated_at, EXCLUDED.source_updated_at),
       linker_metadata   = COALESCE(EXCLUDED.linker_metadata, source_records.linker_metadata),
       status            = 'active',
       ingested_at       = now();
```

### 2.3b `compute_merged(title_id)` 函数 (字段级优先级聚合)

在 advisory lock 保护下按 §3.1a 优先级合并。伪代码 (建议用 `LANGUAGE plpgsql` 实现):

```sql
CREATE OR REPLACE FUNCTION compute_merged(p_title_id BIGINT)
RETURNS TABLE (updated BOOLEAN, new_content_hash CHAR(64)) AS $$
DECLARE
    v_imdb_rec     source_records%ROWTYPE;
    v_tv_rec       source_records%ROWTYPE;
    v_merged       jsonb;
    v_old_hash     CHAR(64);
    v_new_hash     CHAR(64);
    v_merge_ver    INTEGER;
BEGIN
    -- 1. 拉取规范实体当前版本 (CAS 基础)
    SELECT content_hash, merge_version INTO v_old_hash, v_merge_ver
      FROM titles WHERE id = p_title_id FOR UPDATE;

    -- 2. 按 source 拉最新 source_record (每源至多 1 条)
    SELECT * INTO v_imdb_rec FROM source_records
     WHERE title_id = p_title_id AND source = 'imdb' AND status = 'active'
     ORDER BY source_updated_at DESC LIMIT 1;

    SELECT * INTO v_tv_rec FROM source_records
     WHERE title_id = p_title_id AND source = 'simpletv' AND status = 'active'
     ORDER BY source_updated_at DESC LIMIT 1;

    -- 3. 字段级合并 (IMDB > simpleTV, VOD 通过 vod_assets 物化)
    --    详见 §3.1a 字段优先级表
    v_merged := build_merged_jsonb(v_imdb_rec, v_tv_rec);
    v_new_hash := encode(digest(v_merged::text, 'sha256'), 'hex');

    IF v_new_hash = v_old_hash THEN
        RETURN QUERY SELECT FALSE::BOOLEAN, v_old_hash;
        RETURN;
    END IF;

    -- 4. 乐观 CAS 写入
    UPDATE titles
       SET primary_title      = v_merged->>'primary_title',
           normalized_title   = normalize_title(v_merged->>'primary_title'),
           kind               = (v_merged->>'kind')::program_kind,
           release_year       = (v_merged->>'release_year')::SMALLINT,
           series_id          = NULLIF((v_merged->>'series_id'),'')::BIGINT,
           genres_merged      = v_merged->'genres_merged',
           cast_merged        = v_merged->'cast_merged',
           imdb_rating        = NULLIF((v_merged->>'imdb_rating'),'')::NUMERIC(3,1),
           content_hash       = v_new_hash,
           merge_version      = merge_version + 1,
           updated_at         = now()
     WHERE id = p_title_id
       AND merge_version = v_merge_ver;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'merge_version CAS conflict on title_id=%', p_title_id
              USING ERRCODE = '40001';  -- serialization_failure, 应用侧重试
    END IF;

    RETURN QUERY SELECT TRUE::BOOLEAN, v_new_hash;
END;
$$ LANGUAGE plpgsql;
```

调用契约:
```python
# Title Resolver 伪代码
pg.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", imdb_id)
updated, new_hash = pg.fetchone("SELECT * FROM compute_merged(%s)", title_id)
if updated:
    schedule_embed_and_index(title_id, new_hash)
```

`build_merged_jsonb` 与 `normalize_title` 函数见 §4.2 与 §4.6。

### 2.3c VOD 物化到 `titles.vod_playable`

**触发式** (VOD Kafka 批次结束时,仅处理本批次 touched titles):
```sql
UPDATE titles t
   SET vod_playable = EXISTS (
           SELECT 1 FROM vod_assets va
            WHERE va.title_id = t.id
              AND now() BETWEEN COALESCE(va.available_from, '-infinity'::timestamptz)
                            AND COALESCE(va.available_to,   'infinity'::timestamptz)
       ),
       updated_at = now()
 WHERE t.id = ANY(:touched_title_ids);
```

**定时扫描式** (必须有, 否则 available_to 在两次 Kafka 批次之间过期会导致
`vod_playable` 长时间停留在 TRUE, 推荐结果出现"点击即失效"):

```sql
-- 每小时运行一次 (Azure Function TimerTrigger); 仅处理"状态需翻转"的行
WITH stale AS (
    SELECT t.id,
           EXISTS (
               SELECT 1 FROM vod_assets va
                WHERE va.title_id = t.id
                  AND now() BETWEEN COALESCE(va.available_from, '-infinity'::timestamptz)
                                AND COALESCE(va.available_to,   'infinity'::timestamptz)
           ) AS is_playable_now
      FROM titles t
     WHERE t.id IN (
           SELECT title_id FROM vod_assets
            WHERE available_to IS NOT NULL
              AND available_to BETWEEN now() - INTERVAL '2 hours' AND now()
           UNION
           SELECT title_id FROM vod_assets
            WHERE available_from IS NOT NULL
              AND available_from BETWEEN now() - INTERVAL '2 hours' AND now()
     )
)
UPDATE titles t
   SET vod_playable = s.is_playable_now,
       updated_at = now()
  FROM stale s
 WHERE t.id = s.id
   AND t.vod_playable <> s.is_playable_now
 RETURNING t.id;   -- 返回翻转的 title_ids → 同步 AI Search vod_playable 字段
```

扫描窗口 `2 小时` 应略大于扫描间隔 `1 小时`,保证过期边界不会因抖动错过一次。同步逻辑 (patch `vod_playable` 到 AI Search) 复用 `patch_search_playable`。

### 2.4 连接池与并发
- 使用 **PgBouncer** (transaction pooling),应用侧 `psycopg[pool]` 或 `asyncpg.Pool`
- 推荐连接数: `min=2, max=10` per worker pod
- 批量写入使用 `COPY FROM STDIN` 进入临时表,然后 `INSERT ... ON CONFLICT` 合并
- 事务隔离: 默认 `READ COMMITTED`;对账步骤用单事务 + `FOR UPDATE` 防并发批次冲突

---

## 3. Azure AI Search 索引详细设计

### 3.1 索引定义 (`content-rag-v1`)

> **v0.2 变更**: 索引改名 `content-rag-v1` (别名 `content-rag-current`),粒度从 `program_id` 升级为 `title_id`,通过 `doc_type` 字段区分 movie / series / episode 三类文档,共享向量与语义排序配置。

```json
{
  "name": "content-rag-v1",
  "fields": [
    { "name": "doc_id",              "type": "Edm.String", "key": true, "filterable": true },
    { "name": "doc_type",            "type": "Edm.String", "filterable": true, "facetable": true },
    { "name": "title_id",            "type": "Edm.Int64",  "filterable": true },
    { "name": "series_id",           "type": "Edm.Int64",  "filterable": true },
    { "name": "imdb_id",             "type": "Edm.String", "filterable": true },

    { "name": "title_original",      "type": "Edm.String", "searchable": true, "analyzer": "standard.lucene" },
    { "name": "title_local_da",      "type": "Edm.String", "searchable": true, "analyzer": "da.microsoft" },
    { "name": "title_local_en",      "type": "Edm.String", "searchable": true, "analyzer": "en.microsoft" },
    { "name": "title_local_sv",      "type": "Edm.String", "searchable": true, "analyzer": "sv.microsoft" },
    { "name": "title_local_no",      "type": "Edm.String", "searchable": true, "analyzer": "no.microsoft" },

    { "name": "description_short_da","type": "Edm.String", "searchable": true, "analyzer": "da.microsoft" },
    { "name": "description_short_en","type": "Edm.String", "searchable": true, "analyzer": "en.microsoft" },
    { "name": "description_short_sv","type": "Edm.String", "searchable": true, "analyzer": "sv.microsoft" },
    { "name": "description_short_no","type": "Edm.String", "searchable": true, "analyzer": "no.microsoft" },

    { "name": "description_long_da", "type": "Edm.String", "searchable": true, "analyzer": "da.microsoft" },
    { "name": "description_long_en", "type": "Edm.String", "searchable": true, "analyzer": "en.microsoft" },
    { "name": "description_long_sv", "type": "Edm.String", "searchable": true, "analyzer": "sv.microsoft" },
    { "name": "description_long_no", "type": "Edm.String", "searchable": true, "analyzer": "no.microsoft" },

    { "name": "category",            "type": "Edm.String", "filterable": true, "facetable": true },
    { "name": "kind",                "type": "Edm.String", "filterable": true },
    { "name": "genres",              "type": "Collection(Edm.String)", "filterable": true, "facetable": true },
    { "name": "is_sports",           "type": "Edm.Boolean","filterable": true, "facetable": true },
    { "name": "vod_playable",        "type": "Edm.Boolean","filterable": true, "facetable": true },

    { "name": "production_countries","type": "Collection(Edm.String)", "filterable": true, "facetable": true },
    { "name": "release_year",        "type": "Edm.Int32",  "filterable": true, "sortable": true },
    { "name": "imdb_rating",         "type": "Edm.Double", "filterable": true, "sortable": true },

    { "name": "sport_meta",          "type": "Edm.ComplexType",
      "fields": [
        { "name": "sport",           "type": "Edm.String", "filterable": true, "facetable": true },
        { "name": "competition",     "type": "Edm.String", "filterable": true, "facetable": true },
        { "name": "teams",           "type": "Collection(Edm.String)", "searchable": true, "filterable": true },
        { "name": "match_external_id","type":"Edm.String", "filterable": true }
      ]
    },

    { "name": "content_vector",      "type": "Collection(Edm.Single)",
      "searchable": true, "dimensions": 3072,
      "vectorSearchProfile": "hnsw-cosine" },

    { "name": "content_hash",        "type": "Edm.String", "filterable": true },
    { "name": "rag_doc_version",     "type": "Edm.Int32",  "filterable": true, "sortable": true },
    { "name": "updated_at",          "type": "Edm.DateTimeOffset", "filterable": true, "sortable": true }
  ],

  "vectorSearch": {
    "algorithms": [
      { "name": "hnsw-default",
        "kind": "hnsw",
        "hnswParameters": { "m": 4, "efConstruction": 400, "efSearch": 500, "metric": "cosine" } }
    ],
    "profiles": [
      { "name": "hnsw-cosine", "algorithm": "hnsw-default" }
    ]
  },

  "semantic": {
    "configurations": [
      {
        "name": "epg-semantic",
        "prioritizedFields": {
          "titleField":     { "fieldName": "title_original" },
          "prioritizedContentFields": [
            { "fieldName": "description_long_en" },
            { "fieldName": "description_long_da" }
          ],
          "prioritizedKeywordsFields": [
            { "fieldName": "genres" },
            { "fieldName": "category" }
          ]
        }
      }
    ]
  },

  "corsOptions": { "allowedOrigins": ["*"], "maxAgeInSeconds": 300 }
}
```

**字段语义说明** (JSON 不支持注释, 以下为配套解释, 开发需按此实现写入逻辑):

| 字段 | 说明 |
|---|---|
| `doc_id` | 格式: `title:{title_id}` (movie), `series:{series_id}` (series), `episode:{title_id}` (episode) |
| `doc_type` | `'movie' \| 'series' \| 'episode'` — 检索时用 filter 下推 |
| `description_long_*` (series 文档) | LLM 聚合该系列所有集的剧情,约 500 字符;重建由 §4.3 决策 |
| `description_long_*` (movie/episode 文档) | 单节目长描述 (来自 merged titles / simpleTV 单集) |
| `rag_doc_version` | 系列级重建计数器;用于审计/灰度回滚 (Agent 可按此 > X 过滤新版本) |

### 3.1a 字段级 merge 优先级 (AI Search 文档写入前,Title Resolver 使用)

| 字段 | IMDB | simpleTV | VOD | 合并规则 |
|---|---|---|---|---|
| `primary_title` / `title_original` | **W** | R | — | IMDB 权威;IMDB 缺失时回退 simpleTV |
| `description_long` | **W** | W (IMDB 缺失时) | — | IMDB 优先;无则 simpleTV |
| `imdb_rating` | **W** | — | — | IMDB only |
| `genres` / `genres_merged` | **W (主)** | append 补充 | — | 集合 union |
| `release_year` | **W** | R | — | IMDB 权威 |
| `cast` / `cast_merged` | **W** | — | — | IMDB only |
| `kind` | **W** | R | — | IMDB 权威 (movie/series/episode) |
| `series_id` | **W** | R (兜底) | — | IMDB 权威;IMDB 无则用 simpleTV 自带 series_id |
| `airtime` / `channel` / `live` / `rerun` | — | **W** | — | simpleTV only (通过 listings 表体现) |
| `vod_playable` / `asset_url` / `drm` | — | — | **W** | VOD only (通过 vod_assets 表物化) |
| `updated_at` | max | max | max | 三源最大值 |

W = 权威写入, R = 兜底读取, — = 不参与。实现参见 §2.3b 的 `build_merged_jsonb` 与 `compute_merged` 函数。

### 3.2 别名与发布
```http
PUT /aliases/content-rag-current?api-version=2024-07-01
{
  "name": "content-rag-current",
  "indexes": ["content-rag-v1"]
}
```
升级流程:
1. 创建 `content-rag-v2`
2. Reindex (从 Postgres `titles` 权威数据 + 重嵌入,series 文档按 §4.3 决策)
3. 运行评估集 (Recall@5, NDCG@10, 去重假阳/假阴率) 对比 v1
4. 通过 → `PUT /aliases/content-rag-current` 指向 v2
5. 保留 v1 7 天,用于紧急回滚

### 3.3 查询模板

**内容推荐 (默认: 可播放 + movie/series)**
```json
POST /indexes/content-rag-current/docs/search?api-version=2024-07-01
{
  "search": "轻松的科幻喜剧",
  "queryType": "semantic",
  "semanticConfiguration": "epg-semantic",
  "vectorQueries": [
    { "kind": "vector",
      "vector": [/* 3072-dim */],
      "fields": "content_vector",
      "k": 50 }
  ],
  "searchFields": "title_local_da,description_long_da,title_original",
  "filter": "vod_playable eq true and (doc_type eq 'movie' or doc_type eq 'series')",
  "top": 10,
  "select": "doc_id,doc_type,title_id,series_id,title_original,title_local_da,description_short_da,genres,vod_playable,sport_meta"
}
```

**系列剧情检索 (tvSeries 分层)**
```json
{
  "search": "绝命毒师",
  "filter": "doc_type eq 'series' and vod_playable eq true",
  "top": 5,
  "select": "doc_id,series_id,title_id,title_original,description_long_en,rag_doc_version"
}
```

**某集具体剧情 (需要 Agent 主动下探 episode)**
```
filter: "doc_type eq 'episode' and series_id eq 175160"
```

**体育类严格过滤** (不受 `doc_type` 限制)
```
filter: "is_sports eq true and sport_meta/competition eq 'Superliga'"
```

**纯语义检索 (不要求 VOD 可播放, 如查 EPG 即将直播的节目)**
```
filter: "doc_type in ('movie','series')"   -- 不过滤 vod_playable
```

### 3.4 文档写入契约
- Key: `doc_id` (格式见 §3.1 字段语义表)
- 写入通过 `mergeOrUpload`,只在 `content_hash` 变化或 series rebuild 触发时推送
- 批量大小: 1000 文档/请求,错误重试指数退避 (max 3)
- 嵌入失败的文档不写入向量字段,记录到死信队列
- **doc_type='series' 的写入**: 需先聚合该 series 下所有 episode title 的 description_long,调用 `gpt-5-mini` 摘要到 ~500 字符,再作为 series_doc 的 `description_long_*` 字段
- **doc_type='episode'** 为可选;默认不开启 (节省 50%+ 文档量), 仅在"单集剧情/演员"类查询超过黄金集 10% 时启用

---

## 4. 数据摄入管道详细设计

### 4.1 总体流程 (三入口 → Title Resolver → 统一下游)

**关键顺序 (v0.2)**: `源入口 → source_records/vod_assets UPSERT → resolve_title (dedupe) → compute_merged (CAS) → 现有 programs/listings UPSERT (simpleTV 特有) → 快照对账 → 体育实体链接 → tvSeries rebuild 决策 → 嵌入与 AI Search 同步`。

体育实体链接仍必须在嵌入之前,因为嵌入文本 (§4.6) 包含 `sport / competition / teams`;而 `compute_merged` 又必须在体育链接之前,因为链接器读的是**合并后**的 `titles.primary_title` / `titles.genres_merged`,更完整也更准确。

```python
# ============================================================
# 入口 1: IMDB 手动批次 (blob-triggered)
# ============================================================
def ingest_imdb(raw_path: str) -> BatchResult:
    batch_time = now_utc()
    payload = load_json(raw_path)  # ADLS /raw/imdb/{batch_id}.json

    touched_title_ids: set[int] = set()
    for record in payload["titles"]:
        title_id = with_advisory_lock(record["imdb_id"], lambda:
            process_source_record(
                source='imdb',
                source_external_id=record["imdb_id"],
                raw_payload=record,
                source_updated_at=record["updated_at"],
            )
        )
        touched_title_ids.add(title_id)

    post_merge_pipeline(touched_title_ids, batch_time, source='imdb')


# ============================================================
# 入口 2: simpleTV Kafka Consumer (Event Hubs-triggered)
# ============================================================
def ingest_simpletv(events: list[KafkaEvent]) -> BatchResult:
    batch_time = now_utc()
    touched_title_ids: set[int] = set()
    touched_listings: list[dict] = []

    for ev in events:
        payload = json.loads(ev.value)

        # simpleTV 可能单条消息内同时包含 channels/programs/listings 三类子对象
        upsert_channels(payload.get("channels", []), batch_time)

        for prog in payload.get("programs", []):
            title_id = with_advisory_lock(
                prog.get("imdb_id") or f"simpletv:{prog['id']}",
                lambda: process_source_record(
                    source='simpletv',
                    source_external_id=str(prog["id"]),
                    raw_payload=prog,
                    source_updated_at=prog["updated_at"],
                    # simpleTV 的 program 用完整 resolve: imdb_id -> fuzzy title
                )
            )
            touched_title_ids.add(title_id)
            # programs 表补写 title_id FK, UPSERT 沿用 §2.3
            upsert_program(prog, title_id=title_id, batch_time=batch_time)

        for listing in payload.get("listings", []):
            touched_listings.append(listing)

    # simpleTV 特有: listings UPSERT + 快照对账
    list_changes = upsert_listings(touched_listings, batch_time)
    tombstoned = tombstone_missing_by_batch(batch_time, touched_listings)

    post_merge_pipeline(touched_title_ids, batch_time,
                        source='simpletv',
                        list_changes=list_changes,
                        tombstoned=tombstoned)


# ============================================================
# 入口 3: VOD Kafka Consumer (Event Hubs-triggered)
# ============================================================
def ingest_vod(events: list[KafkaEvent]) -> BatchResult:
    batch_time = now_utc()
    touched_title_ids: set[int] = set()

    for ev in events:
        payload = json.loads(ev.value)
        # 按 imdb_id 或 fuzzy title 查找既有 title (VOD 不新建 title)
        title_id = resolve_title_for_vod(payload)
        if title_id is None:
            move_to_dead_letter('vod', payload,
                reason='no matching title, awaiting IMDB/simpleTV record')
            continue

        upsert_vod_asset(title_id, payload, batch_time)
        touched_title_ids.add(title_id)

    # VOD 不参与 titles 字段 merge,仅物化 vod_playable 标志
    refresh_vod_playable(touched_title_ids)
    # VOD 变化影响 AI Search 的 vod_playable filter → 需要 mergeOrUpload
    # (仅补 vod_playable 字段,不重嵌入文本向量)
    patch_search_playable(touched_title_ids)

    record_batch_result(source='vod', batch_time=batch_time,
                        touched_title_ids=touched_title_ids)


# ============================================================
# 统一下游 (IMDB + simpleTV 走此路径;VOD 有独立轻量路径)
# ============================================================
def post_merge_pipeline(title_ids: set[int], batch_time, **kwargs):
    # 1. compute_merged — 每个 title 在 advisory lock 下做 CAS
    merged = []
    for tid in title_ids:
        updated, new_hash = pg.fetchone(
            "SELECT * FROM compute_merged(%s)", tid
        )
        if updated:
            merged.append((tid, new_hash))

    # 2. 体育实体链接 — 作用在"被 merge 影响 + 有 active 体育 listing"的 title 上
    #    注意: 必须同时包含 "title 元数据未变但 listing 新增/变更" 的 title_ids,
    #    否则纯排播变更 (新增直播场次、时间调整) 不会触发 sport_events 重链。
    touched_title_ids_via_listings = pg.fetchall("""
        SELECT DISTINCT p.title_id
          FROM programs p
         WHERE p.id = ANY(%s) AND p.title_id IS NOT NULL
    """, [[c["program_id"] for c in kwargs.get('list_changes', [])]])
    relink_title_ids = list({tid for tid, _ in merged} |
                             {row["title_id"] for row in touched_title_ids_via_listings})
    relink_targets = find_relink_candidates_by_titles(
        relink_title_ids,
        kwargs.get('list_changes', [])
    )
    link_result = linker.link_batch(relink_targets)

    # 3. tvSeries rebuild 决策 — 见 §4.3
    series_to_rebuild = decide_series_rebuilds(merged, link_result)

    # 4. 嵌入 + AI Search mergeOrUpload
    reembed_targets = compute_reembed_targets(
        merged, link_result, series_to_rebuild
    )
    embed_and_push(reembed_targets)

    # 5. 审计
    record_batch_result(source=kwargs['source'], batch_time=batch_time,
        titles_merged=len(merged),
        series_rebuilt=len(series_to_rebuild),
        **kwargs)
```

`compute_reembed_targets` 说明 (v0.2): 对每个候选 (title_id, doc_type) 组合,按 §4.6 拼接嵌入输入 (movie / series / episode 三种模板不同),计算 `new_doc_content_hash`;与 AI Search 现有 `content_hash` 比较,仅变化才写入。这样既保证 series rebuild/体育 sport_meta 变化会触发重嵌入,又不会因为无关字段扰动导致冗余嵌入。

### 4.2 去重算法 `resolve_title` (Title Resolver 核心)

```python
def resolve_title(incoming: dict, source: str) -> tuple[int, bool]:
    """
    三段式:
      Stage 1: imdb_id 精确匹配
      Stage 2: normalized_title 模糊匹配 (pg_trgm sim >= 0.85, 同 kind, year ±1)
      Stage 3: 新建 canonical title

    返回 (title_id, is_new)
    低置信匹配 (0.75 <= sim < 0.85) 不自动合并,推送到人工审核队列 (Service Bus)
    """
    # Stage 1
    if imdb := incoming.get("imdb_id"):
        row = pg.fetchone("SELECT id FROM titles WHERE imdb_id = %s", imdb)
        if row:
            return row["id"], False

    # Stage 2
    norm = normalize_title(incoming["primary_title"])
    kind = incoming.get("kind") or "other"
    year = incoming.get("release_year")  # 可能为 None
    # 年份过滤规则:
    #   - 若 incoming 带 year → 要求 candidate.release_year IS NULL 或 abs(diff) <= 1
    #   - 若 incoming 无 year → 不限制 candidate 的 release_year (防止误判为 1970 年段)
    candidates = pg.fetchall("""
        SELECT id, similarity(normalized_title, %(norm)s) AS sim
          FROM titles
         WHERE kind = %(kind)s
           AND (%(year)s::INT IS NULL
                OR release_year IS NULL
                OR abs(release_year - %(year)s::INT) <= 1)
           AND normalized_title %% %(norm)s
         ORDER BY sim DESC
         LIMIT 5
    """, {"norm": norm, "kind": kind, "year": year})

    best = candidates[0] if candidates else None
    if best and best["sim"] >= 0.85:
        return best["id"], False
    if best and best["sim"] >= 0.75:
        enqueue_human_review(incoming, candidates, source)
        # 未自动合并;返回一个临时 title (走 Stage 3) 以不阻塞流水线
    # Stage 3
    return insert_new_title(incoming, norm), True


def normalize_title(s: str) -> str:
    """
    归一化规则 (CJK 安全):
      1. Unicode NFKD 分解
      2. 丢弃组合重音符号 (category Mn)  — 这等价于 SQL 的 unaccent(),但保留 CJK 字符
      3. 转小写 (CJK 字符不受影响)
      4. 非字母数字 + 非字空格 → 空格 (re 的 \\w + Unicode flag 涵盖 CJK 字符)
      5. 多空白压缩

    不得使用 str.encode('ascii','ignore') —— 会把中文/日文/韩文/阿拉伯文全部吞掉,
    导致 normalized_title 为空,pg_trgm 假阳性雪崩。
    """
    import re, unicodedata
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(ch for ch in s if unicodedata.category(ch) != 'Mn')
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s
```

**对应 SQL 侧** (`compute_merged` 里调用的 `normalize_title(text)` 函数):

```sql
CREATE OR REPLACE FUNCTION normalize_title(s TEXT) RETURNS TEXT AS $$
    SELECT lower(
              regexp_replace(
                  regexp_replace(unaccent(s), '[^[:alnum:][:space:]]+', ' ', 'g'),
                  '\s+', ' ', 'g'
              )
           )
$$ LANGUAGE SQL IMMUTABLE;
-- unaccent() 对 CJK 无副作用 (无组合重音), 保留中文/日文/韩文原字符
-- [:alnum:] POSIX class 包括所有 Unicode 字母数字, 涵盖 CJK
```
应用侧与 SQL 侧输出必须一致 (单元测试覆盖 5 种语言样本: da/en/zh/ja/ko)。

**阈值推导**:
- **0.85**: 控制假阳性 ("冰雪奇缘 1" vs "冰雪奇缘 2" sim ≈ 0.87 → 通过 `release_year ±1` 二次过滤再 reject)
- **0.75**: 允许召回长标题变体 ("The Lord of the Rings: Fellowship of the Ring" vs "Fellowship of the Ring" sim ≈ 0.78 → 人工审核)
- 阈值待评估集 (见 §10) 调参后锁定;初始值参考业界同类系统经验

**process_source_record 契约**:

```python
def process_source_record(source, source_external_id, raw_payload,
                          source_updated_at) -> int:
    # 1. 先尝试用已有 source_records.title_id (减少重复 dedupe)
    existing = pg.fetchone("""
        SELECT title_id FROM source_records
         WHERE source = %s AND source_external_id = %s
    """, (source, source_external_id))
    title_id = existing["title_id"] if existing else None

    # 2. 若无 → resolve_title 走三段去重
    if title_id is None:
        title_id, _is_new = resolve_title(extract_minimal(raw_payload), source)

    # 3. UPSERT source_records (§2.3a)
    pg.execute(UPSERT_SOURCE_RECORD_SQL,
               source, source_external_id, title_id,
               raw_payload, source_updated_at)
    return title_id
```

### 4.3 tvSeries 系列级 RAG rebuild 决策

RAG 用的是系列**整体**剧情,不是单集。单集更新到达时不一定要重算系列级向量。

```python
REBUILD_EPISODE_COUNT_THRESHOLD = 10
REBUILD_DAYS_THRESHOLD = 90
REBUILD_LONG_DESC_CHAR_THRESHOLD = 200

def decide_series_rebuilds(merged: list[tuple], link_result) -> list[int]:
    """返回需要重建 series_doc 的 series_id 列表"""
    touched_series_ids = set()
    for title_id, _ in merged:
        t = pg.fetchone(
            "SELECT series_id, kind FROM titles WHERE id = %s", title_id
        )
        if t and t["kind"] == "episode" and t["series_id"]:
            touched_series_ids.add(t["series_id"])

    rebuild = []
    for sid in touched_series_ids:
        if should_rebuild_series_doc(sid):
            rebuild.append(sid)
    return rebuild


def should_rebuild_series_doc(series_id: int) -> bool:
    s = pg.fetchone("""
        WITH series_row AS (
            SELECT id, updated_at
              FROM titles
             WHERE series_id = %(sid)s AND kind = 'series'
             LIMIT 1
        ),
        -- 通过 source_records 拉该系列已有 episode 的长描述最大长度
        -- (titles 表不直接存 description_long, 需回溯 source_records.raw_payload)
        episode_long_desc AS (
            SELECT COALESCE(MAX(char_length(
                       COALESCE(sr.raw_payload #>> '{descriptions,long}', '')
                   )), 0) AS max_long_len
              FROM titles te
              JOIN source_records sr ON sr.title_id = te.id
             WHERE te.series_id = %(sid)s
               AND te.kind = 'episode'
               AND te.updated_at > COALESCE((SELECT updated_at FROM series_row),
                                             '-infinity'::timestamptz)
        )
        SELECT
          (SELECT COUNT(*) FROM titles
            WHERE series_id = %(sid)s AND kind = 'episode'
              AND updated_at > COALESCE((SELECT updated_at FROM series_row),
                                         '-infinity'::timestamptz)
          ) AS episodes_since_last_rebuild,
          (SELECT max_long_len FROM episode_long_desc) AS new_episode_long_len,
          (SELECT EXISTS (
              SELECT 1 FROM titles t2
                JOIN source_records sr2 ON sr2.title_id = t2.id
               WHERE t2.series_id = %(sid)s
                 AND t2.kind = 'episode'
                 AND t2.updated_at <= COALESCE((SELECT updated_at FROM series_row),
                                                '-infinity'::timestamptz)
                 AND char_length(COALESCE(sr2.raw_payload #>> '{descriptions,long}', ''))
                     >= %(threshold)s
           )) AS series_already_has_long_plot,
          EXTRACT(DAY FROM now() - COALESCE((SELECT updated_at FROM series_row), now()))
             AS days_since_rebuild,
          (SELECT id FROM series_row) AS series_title_id
    """, {"sid": series_id, "threshold": REBUILD_LONG_DESC_CHAR_THRESHOLD})

    # A. 新季度首集 (season_id 前所未见)
    if new_season_detected(series_id):
        return True
    # B. 系列元数据显著漂移 (IMDB 刷新了 cast / genres / imdb_rating)
    if series_metadata_drift(series_id):
        return True
    # C. 新集首次引入长描述 (旧集均无, 本批新集 >= 阈值)
    if (s["new_episode_long_len"] or 0) >= REBUILD_LONG_DESC_CHAR_THRESHOLD \
       and not s["series_already_has_long_plot"]:
        return True
    # D. 定期兜底
    if (s["episodes_since_last_rebuild"] or 0) >= REBUILD_EPISODE_COUNT_THRESHOLD:
        return True
    if s["series_title_id"] is None:
        # 系列级 title 尚未存在 → 首次创建, 必须 rebuild
        return True
    if (s["days_since_rebuild"] or 9999) >= REBUILD_DAYS_THRESHOLD:
        return True
    return False
```

rebuild 动作:
1. 聚合该 `series_id` 下所有 `episode` title 的 `description_long_*`
2. 调 `gpt-5-mini` 摘要到 ~500 字符 series-level 剧情
3. 按 §4.6 series 模板拼接嵌入输入,计算 `content_hash`
4. 写入 `titles` where `kind='series'` (若无则新建),`rag_doc_version += 1`
5. `mergeOrUpload` 到 AI Search (`doc_id = f"series:{series_id}"`, `doc_type='series'`)

### 4.4 `find_relink_candidates` 规则 (关键)

**v0.2 变更**: 输入从 `(channel_id, cov_start, cov_end)` 扩展为 `(title_ids, list_changes)`。体育链接器作用在**规范 title_id** 与其 active listings 上,避免多源快照各跑一遍导致重复调 Sportradar。

```python
def find_relink_candidates_by_titles(
        touched_title_ids: list[int],
        list_changes: list[dict]) -> list[int]:
    """返回需要 (重新) 链接的 listing_id 集合"""
    sql = """
    SELECT l.id
      FROM listings l
      JOIN programs p       ON p.id       = l.program_id
      JOIN titles   t       ON t.id       = p.title_id
      LEFT JOIN sport_events se ON se.listing_id = l.id
     WHERE l.status = 'active'
       AND t.id = ANY(%s)
       AND (
            p.category = 'Sports' OR
            'Sports' = ANY(SELECT jsonb_array_elements_text(t.genres_merged)) OR
            EXISTS (SELECT 1 FROM program_genres g
                     WHERE g.program_id = p.id AND g.genre_name = 'Sports')
       )
       AND (
            se.listing_id IS NULL                                  -- 新增
         OR se.source_content_hash <> l.listing_content_hash       -- listing 内容变更 (标题/时间/赛事字段等)
         OR se.linker_confidence < 0.6                             -- 低置信重试
         OR l.id = ANY(%s)                                         -- 本批次变更的 listing_id
       );
    """
    return pg.fetchall(sql, [touched_title_ids,
                              [c["id"] for c in list_changes]])
```

### 4.5 幂等性与并发一致性
- 每批次以 `ingest_batches` 记录 + `(source, source_external_id)` 复合主键保证 **源快照幂等**
- `titles.merge_version` 做 **乐观 CAS**;冲突时应用侧重试一次 (幂等),仍冲突记 `ingest_batches.merge_conflicts++` 并告警
- 跨管道并发写同一实体: PG advisory lock `pg_advisory_xact_lock(hashtextextended(imdb_id, 0))` 按 `imdb_id` 分片;无 imdb_id 的孤儿 title 用 `hashtextextended('simpletv:'||source_external_id, 0)` 备用分片
- simpleTV 批内冲突 (同一 `channel_id + date` 重放): 保留原有 `pg_advisory_xact_lock(hash(channel_id, date))`,叠加使用
- 失败分类:
  - **源数据坏**: 移入 `dead_letter/{source}/{date}.jsonl`,告警 P2
  - **下游 API 失败** (嵌入/AI Search/Sportradar/tvSeries 摘要 LLM): 重试 3 次 + 指数退避,最终失败记入 `ingest_batches.status='partial'`,下一批次自动补跑

### 4.6 嵌入文本规范 (按 doc_type 分支)

拼接顺序固定,便于 hash;`content_hash = sha256(拼接结果)`。

**doc_type = 'movie' (单片/节目)**
```
[MOVIE] {primary_title}
{title_local_<preferred_lang>}
{description_short_<preferred_lang>}
{description_long_<preferred_lang>}
genres: {g1, g2, ...}
year: {release_year}
imdb_rating: {imdb_rating}
sport: {sport}
competition: {competition}
teams: {t1, t2}
```

**doc_type = 'series' (系列聚合,LLM 摘要后)**
```
[SERIES] {series_primary_title}
seasons: {N}
episodes_total: {M}
summary: {gpt-5-mini 产出的 ~500 字系列剧情摘要}
genres: {g1, g2, ...}
cast: {a1, a2, ...}
imdb_rating: {imdb_rating}
sport: {sport}
competition: {competition}
```

**doc_type = 'episode' (单集, 可选)**
```
[EPISODE] {series_primary_title} — {episode_title}
season: {season_no} episode: {episode_no}
{description_long_<preferred_lang>}
```

模板中任何字段为空即空行占位 (不省略,保持 hash 稳定)。`preferred_lang` 默认 `en`,在已有多语言地区库中可配置为 `da` 等。

---

## 5. 体育实体链接器详细设计

### 5.1 处理流程
```
Listing (sports)
   ├─► 文本归一化 (unaccent, 小写, 去标点)
   ├─► 规则抽取
   │     - 竞赛正则词典 (Superliga, Allsvenskan, UCL, ...)
   │     - 分隔符切分球队: "A – B", "A vs B", "A mod B"
   ├─► 实体规范化
   │     - 球队别名表 (内置 CSV,初始规模 ~2000 条,覆盖北欧 + 五大联赛)
   ├─► Sportradar/Opta API
   │     - 按 (sport, competition, date) 查 fixtures
   │     - 按 (home_team, away_team) 匹配
   │     - 返回 match_external_id
   └─► 写入 sport_events + AI Search sport_meta 补丁
         linker_confidence =
             0.5 * rule_match_quality
           + 0.3 * api_match_exactness
           + 0.2 * date_proximity_score
```

### 5.2 置信度阈值
| 区间 | 行为 |
|---|---|
| ≥ 0.8 | 直接写入,不重试 |
| 0.6–0.8 | 写入,但下一批次重新评估 |
| < 0.6 | 不写 `match_external_id`,仅保留 `home_team/away_team`;告警日志 |

### 5.3 版本化
- `linker_version` 随规则/模型升级打标 (`rule-v1.2`, `llm-v0.3`)
- 每次升级后,对 `linker_confidence < 0.8` 的行全部重跑

---

## 6. Agent 工具 JSON Schema

### 6.1 `search_programs`
```json
{
  "name": "search_programs",
  "description": "语义检索内容库 (movie/series/episode),适用于描述模糊、语义相关的查询或内容推荐。默认只返回可播放 (vod_playable=true) 的 movie 与 series 文档。",
  "parameters": {
    "type": "object",
    "required": ["query", "locale"],
    "properties": {
      "query":   { "type": "string", "description": "用户自然语言查询" },
      "locale":  { "type": "string", "enum": ["da","en","sv","no"] },
      "doc_type": {
        "type": "array",
        "items": { "type": "string", "enum": ["movie","series","episode"] },
        "default": ["movie","series"],
        "description": "检索的文档类型。推荐类查询默认 movie+series;单集类查询可加入 episode"
      },
      "require_playable": {
        "type": "boolean",
        "default": true,
        "description": "true = 仅返回 vod_playable 为 true 的文档。推荐类默认 true;EPG 即将直播场景应显式设为 false"
      },
      "filters": {
        "type": "object",
        "properties": {
          "is_sports":  { "type": "boolean" },
          "category":   { "type": "string" },
          "genres":     { "type": "array", "items": { "type": "string" } },
          "country":    { "type": "string", "pattern": "^[A-Z]{2}$" },
          "series_id":  { "type": "integer" }
        },
        "additionalProperties": false
      },
      "top":     { "type": "integer", "minimum": 1, "maximum": 20, "default": 5 }
    }
  }
}
```

返回值 schema:
```json
{
  "type": "array",
  "items": {
    "type": "object",
    "required": ["doc_id","doc_type","title_id","title","score"],
    "properties": {
      "doc_id":       { "type": "string" },
      "doc_type":     { "type": "string", "enum": ["movie","series","episode"] },
      "title_id":     { "type": "integer" },
      "series_id":    { "type": "integer" },
      "title":        { "type": "string" },
      "snippet":      { "type": "string" },
      "score":        { "type": "number" },
      "vod_playable": { "type": "boolean" },
      "sport_meta":   { "type": "object" }
    }
  }
}
```

### 6.2 `query_schedule`
```json
{
  "name": "query_schedule",
  "description": "按时间/频道/体育谓词查询排播,返回 listing + title + sport_events 合并视图。体育类问题首选此工具。",
  "parameters": {
    "type": "object",
    "properties": {
      "title_ids":      { "type": "array",  "items": { "type": "integer" } },
      "program_ids":    { "type": "array",  "items": { "type": "string" } },
      "channel_id":     { "type": "integer" },
      "country":        { "type": "string", "pattern": "^[A-Z]{2}$" },
      "time_range": {
        "type": "object",
        "properties": {
          "from": { "type": "string", "format": "date-time" },
          "to":   { "type": "string", "format": "date-time" }
        },
        "required": ["from", "to"]
      },
      "is_live":        { "type": "boolean" },
      "sport":          { "type": "string", "description": "football / basketball / tennis / '*' = any" },
      "competition":    { "type": "string" },
      "team":           { "type": "string", "description": "模糊匹配 home_team / away_team / teams[]" },
      "has_match_id":   { "type": "boolean", "description": "true = 仅返回可查实时比分的场次" },
      "require_playable":{ "type": "boolean", "description": "true = 仅返回 titles.vod_playable=true 的排播; 默认 false (EPG 排播本身不要求 VOD 副本)" },
      "limit":          { "type": "integer", "minimum": 1, "maximum": 50, "default": 10 }
    },
    "additionalProperties": false
  }
}
```

返回值 schema (契约):
```json
{
  "type": "array",
  "items": {
    "type": "object",
    "required": ["listing_id","program_id","title_id","channel","start","end"],
    "properties": {
      "listing_id":        { "type": "integer" },
      "program_id":        { "type": "integer" },
      "title_id":          { "type": "integer" },
      "title":             { "type": "string" },
      "channel":           { "type": "string" },
      "channel_id":        { "type": "integer" },
      "start":             { "type": "string", "format": "date-time" },
      "end":               { "type": "string", "format": "date-time" },
      "is_live":           { "type": "boolean" },
      "is_rerun":          { "type": "boolean" },
      "tune_url":          { "type": "string" },
      "vod_playable":      { "type": "boolean" },
      "sport":             { "type": "string" },
      "competition":       { "type": "string" },
      "teams":             { "type": "array", "items": { "type": "string" } },
      "match_external_id": { "type": "string" }
    }
  }
}
```

### 6.2a `query_vod` (新增)
```json
{
  "name": "query_vod",
  "description": "按 title_id 返回 VOD 可播放副本列表。用于'立即播放'按钮或回答'能在哪里看到...'",
  "parameters": {
    "type": "object",
    "required": ["title_ids"],
    "properties": {
      "title_ids": { "type": "array", "items": { "type": "integer" }, "minItems": 1, "maxItems": 50 },
      "country":   { "type": "string", "pattern": "^[A-Z]{2}$", "description": "用于 geo_restriction 过滤" },
      "only_current":{ "type": "boolean", "default": true, "description": "true = 仅返回 now() 在 available_from/to 区间的副本" }
    }
  }
}
```
返回:
```json
{
  "type": "array",
  "items": {
    "type": "object",
    "required": ["title_id","assets"],
    "properties": {
      "title_id": { "type": "integer" },
      "assets": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "asset_url":      { "type": "string" },
            "drm":            { "type": "string" },
            "resolution":     { "type": "string" },
            "quality":        { "type": "string" },
            "available_from": { "type": "string", "format": "date-time" },
            "available_to":   { "type": "string", "format": "date-time" }
          }
        }
      }
    }
  }
}
```

### 6.3 `get_live_scores`
```json
{
  "name": "get_live_scores",
  "description": "按 match_external_id 获取实时或已结束比赛的比分/事件。",
  "parameters": {
    "type": "object",
    "required": ["match_external_id"],
    "properties": {
      "match_external_id": { "type": "string" },
      "include_events":    { "type": "boolean", "default": false }
    }
  }
}
```

### 6.4 `web_grounding`
```json
{
  "name": "web_grounding",
  "description": "Bing 网络搜索,用于球员/球队背景等知识类问题。禁止用于实时比分与排播。",
  "parameters": {
    "type": "object",
    "required": ["query"],
    "properties": {
      "query": { "type": "string" },
      "freshness": { "type": "string", "enum": ["Day","Week","Month","Year","Any"], "default": "Month" }
    }
  }
}
```

### 6.5 `tune_to_channel`
```json
{
  "name": "tune_to_channel",
  "description": "向 TV 客户端发送跳台指令(客户端回调,不影响后端状态)。",
  "parameters": {
    "type": "object",
    "required": ["channel_id"],
    "properties": {
      "channel_id": { "type": "integer" },
      "listing_id": { "type": "integer", "description": "可选,用于上报跳台来源" }
    }
  }
}
```

### 6.6 系统提示词模版 (供 Foundry Agent 注册)
```
你是海信电视的体育与内容助手。严格遵守以下规则:
1. 回复语言必须与入参 locale 一致。
2. 涉及节目时必须同时给出频道与时间;若正在直播,追加一个 action=tune_to_channel。
3. 内容推荐类问题 ("推荐一部..." / "有什么好看的..."): 调用 search_programs,
   默认 doc_type=['movie','series'] 且 require_playable=true。
   系列类问题先找 doc_type='series' 的命中,仅在用户问"某集剧情"时扩展到 'episode'。
4. 体育类问题优先调用 query_schedule (支持 sport/competition/team 过滤);
   仅当 query_schedule 无结果时才 fallback 到 search_programs。
5. 比分类问题: 必须先从 query_schedule 结果中拿到 match_external_id,
   再调用 get_live_scores;不得凭空编造比分。
6. "立即播放"或"在哪里能看到 ...": 命中 search_programs 后用 query_vod
   解析 asset_url;若 vod_playable=false 但是排播中,退而推荐 tune_to_channel。
7. 球员/球队背景类问题使用 web_grounding;不得用于实时比分。
8. 工具调用深度 ≤ 3;无结果时直接告知用户,不要反复重试。
输出 JSON: { "speak": <给 TTS 的自然语言>, "card": <结构化卡片>, "actions": [...] }
```

---

## 7. 客户端 ↔ 后端 API 契约

### 7.1 `POST /v1/ai/ask`
**Headers**
- `Authorization: Bearer <jwt>`
- `X-Device-Id: <hash(mac)>`
- `X-Region: DK`
- `Content-Type: application/json`

**Body**
```json
{
  "query_text": "今晚有什么足球比赛?",
  "locale": "da",
  "channel_context": { "channel_id": 1, "now_playing_program_id": 18648748 },
  "session_id": "uuid-optional"
}
```

**Response (SSE, streamed)**
```
event: token    data: {"text":"今晚","seq":1}
event: token    data: {"text":"在 DR1","seq":2}
event: card     data: {"type":"listing","title":"...","channel":"DR1", ...}
event: action   data: {"type":"tune_to_channel","channel_id":1}
event: done     data: {"request_id":"...","latency_ms":1870}
```

**错误**
| HTTP | Code | 含义 |
|---|---|---|
| 400 | `bad_request` | 入参校验失败 |
| 401 | `unauthorized` | JWT 无效/过期 |
| 429 | `rate_limited` | APIM 配额 |
| 503 | `upstream_unavailable` | 下游工具全部失败 |

---

## 8. Redis 缓存键规范

| 用途 | Key 模式 | TTL | Value |
|---|---|---|---|
| 语义缓存 | `sem:{locale}:{country}:{sha1(normalized_query)}:{5min_bucket}` | 5 min | gzip JSON 响应 |
| 实时比分 | `score:{match_external_id}` | 30 s (live), 24 h (ended) | JSON |
| 排播快速路径 | `sched:{channel_id}:{hour_bucket}` | 10 min | JSON |
| 嵌入结果 | `emb:{sha256(input)}` | 30 天 | float[3072] (base64) |
| 工具幂等锁 | `lock:tune:{device_id}:{channel_id}` | 3 s | "1" |
| 去重候选缓存 (Stage 2) | `dedupe:norm:{sha1(normalized_title)}:{kind}:{year_bucket}` | 7 天 | JSON: [{title_id, sim}] |
| Title Resolver 幂等锁 | `lock:resolver:{imdb_id}` | 60 s | "1" |
| VOD 物化锁 | `lock:vod_refresh:{title_id}` | 30 s | "1" |

规范化查询: 小写 → unaccent → 去标点 → 连续空白压缩。

`dedupe:norm:*` 使用说明: 摄入管道高频调用 pg_trgm;对同一 normalized_title + kind + year_bucket (e.g. 2020/2021/2022 同桶) 的查询结果缓存 7 天,命中时跳过 Stage 2 SQL,仅当缓存未命中或该 normalized_title 有新 title 入库 (触发 cache invalidation) 时回表。

---

## 9. 可观测性埋点字段表

每次请求写入一条结构化日志:
```
request_id, device_id_hash, region, locale,
start_ts, end_ts, ttft_ms, total_ms,
model_name, prompt_tokens, completion_tokens,
cache_level_hit,                 -- none | semantic | score | schedule | dedupe
tool_calls: [
  { name, start_ms, duration_ms, ok, error_code,
    args_digest, result_size }
],
search_metrics: { bm25_top, vector_top, rerank_scores_top3,
                  doc_type_hits: {movie, series, episode} },
sql_metrics:    { rows, elapsed_ms, index_hit },
final_status                     -- ok | partial | error
```

**摄入管道专有指标** (每批次一条 `ingest_metrics` 日志):
```
batch_id, source,                -- imdb | simpletv | vod
source_batch_time,
source_records_upserted,
titles_new, titles_merged,
dedupe_hit_stage_distribution,   -- { imdb: N, fuzzy: M, new: K }
merge_conflicts,                 -- CAS 重试次数
rag_rebuild_reasons,             -- { new_season: N, metadata_drift: M, periodic: K }
series_rebuilt,
vod_playable_rate,               -- refreshed titles 中 vod_playable=true 比例
dead_letter_count, partial_retries
```
存 Application Insights + Log Analytics,保留 30 天;聚合后写 Fabric/Synapse 作 BI,保留 1 年。

核心仪表盘 (KQL 片段):
```
Requests
| summarize p50=percentile(ttft_ms,50), p95=percentile(ttft_ms,95)
            by bin(timestamp, 5m), region
```

---

## 10. 测试设计

### 10.1 单元测试
- `upsert_listings` 幂等性:同一 JSON 跑两次,行数不变,`updated_at` 第一次之后不再更新
- 对账逻辑:构造"第二天少一条 listing"场景,断言原行 `status='removed'`
- `find_relink_candidates`:验证 4 种重链触发条件全部命中
- 嵌入 `content_hash` 稳定性:同输入两次必须相同 (三种 doc_type 分别测)
- **去重 Stage 1** (新): 用 imdb_id 命中既存 title
- **去重 Stage 2** (新): 20 条假阳性基准 + 20 条假阴性基准,断言阈值 0.85 表现
- **compute_merged** (新): 构造 IMDB-only / simpleTV-only / 双源完整 / 字段冲突四种场景,断言 titles.* 按 §3.1a 优先级填充
- **merge_version CAS** (新): 并发写同一 title_id,断言第二次 UPDATE 报 `40001`,应用重试一次后成功
- **should_rebuild_series_doc** (新): 覆盖 4 条触发条件 (新季/长描述/元数据漂移/定期) + 不触发场景
- **VOD 物化** (新): `vod_playable` 在 available_to 过期后自动变 false

### 10.2 集成测试
- E2E simpleTV: 摄入样例 `1.2026-04-14.json` → 跑一遍 → Postgres 生成对应 `titles` / `programs` / `listings`, AI Search `content-rag-v1` 文档数 = 去重后 title 数
- E2E IMDB: 构造 mock IMDB 批次覆盖已有 simpleTV title → 断言 merge 后 primary_title 变成 IMDB 值,simpleTV 值降为 fallback
- E2E VOD: 构造 VOD 批次 → 断言 `titles.vod_playable=true` + AI Search `vod_playable` 字段同步
- E2E 三源并发: 三个 Function 同时处理同一 imdb_id → 无重复 title,merge_conflicts ≤ 3
- Agent 工具调用: mock 6 个工具 (含 query_vod),跑 HLD §5.2 的 8 条典型查询,断言工具调用链正确

### 10.3 评估集 (持续运行)
- 100 条黄金 query,分 6 类 (EPG / 体育 / 比分 / 知识 / 多语言 / 可播放推荐)
- **去重假阳/假阴**: 额外 200 条标注对 (is_same_title: yes/no),测算 precision/recall
- **merge 优先级**: 20 个冲突场景,断言 titles.* 与优先级表一致
- **tvSeries rebuild 触发**: 10 个场景模拟数据,断言决策与预期一致 (避免级联 rebuild)
- 指标: 工具选择准确率、答案相关性 (LLM-as-judge 4/5 起)、事实一致性、去重 F1 ≥ 0.95
- 门禁: PR 合并前必须通过;索引重建后必须通过;周度报告

### 10.4 压测
- k6 脚本,10 → 200 QPS 阶梯,观察 p95 < 5s 的最大承载
- 热点场景: "今晚有什么体育比赛" 占 40% 流量 (缓存命中率应 > 80%)
- **摄入侧压测**: simpleTV Kafka 10x 周度消息量突发,观察 Title Resolver advisory lock 竞争与 CAS 冲突率

---

## 11. 部署与环境

| 环境 | 用途 | AI Search | Postgres | Function |
|---|---|---|---|---|
| dev | 开发 | Basic,1 replica | Burstable B1ms | Consumption |
| staging | 集成+评估 | S1,2 replica | GP_Standard_D2s_v3 | Premium EP1 |
| prod | 生产 | S2,3 replica,2 partition | GP_Standard_D4s_v3,HA zone-redundant | Premium EP2,预热实例 ≥ 2 |

IaC: Bicep 模块 `infra/{network, postgres, search, function, foundry, keyvault}.bicep`,CI pipeline `azure-pipelines.yml` 按环境参数部署。

---

## 12. 开发任务拆解 (供排期)

| # | 任务 | 预估工时 | 依赖 |
|---|---|---|---|
| T1 | PG DDL + 迁移脚本 (含 titles/source_records/vod_assets) | 3d | — |
| T1a| `compute_merged` plpgsql 函数 + `build_merged_jsonb` | 2d | T1 |
| T2 | IMDB Blob-trigger Function (raw → source_records UPSERT) | 2d | T1 |
| T2a| simpleTV Kafka Consumer (Event Hubs-trigger Function) | 3d | T1 |
| T2b| VOD Kafka Consumer + `refresh_vod_playable` (含每小时定时扫描过期副本) | 2d | T1 |
| T3 | Title Resolver 服务 (resolve_title 三段去重) | 3d | T1,T1a |
| T3a| 人工审核队列 (Service Bus + 简易审核 UI stub) | 1d | T3 |
| T4 | 对账 + 幂等 + 批次审计 (ingest_batches v0.2 字段) | 2d | T2,T2a |
| T5 | AI Search 索引创建 (content-rag-v1) + 别名脚本 | 1d | — |
| T6 | 变化检测 + 嵌入 + AI Search push (含 doc_type 分支) | 3d | T3,T5 |
| T6a| tvSeries rebuild 决策 + LLM 聚合摘要 | 2d | T6 |
| T7 | 体育实体链接器 (规则 + Sportradar,作用在 title_id) | 4d | T3 |
| T8 | Agent 工具后端 (search_programs/query_schedule/query_vod/scores/tune) | 5d | T1,T5 |
| T9 | Agent 注册 + 系统提示词 | 1d | T8 |
| T10| API Gateway + SSE 流式端点 | 2d | T9 |
| T11| Redis 缓存层 (含 dedupe:norm 候选缓存) | 1d | T10 |
| T12| 可观测性埋点 + 仪表盘 (含摄入批次指标) | 2d | T10 |
| T13| 评估集 + CI 门禁 (含去重假阳/假阴、merge 优先级) | 3d | T9 |
| T14| 压测 + 调优 (含摄入侧压测) | 3d | T10 |
| T15| IaC + 环境部署 (含 Event Hubs + 3 个 Function App) | 4d | All |
| **合计** | | **~49 人日** | |

建议里程碑:
- **M2 数据层基础**: T1 + T1a + T2 + T5 (7d)
- **M2.5 多源摄入 + Title Resolver**: T2a + T2b + T3 + T3a + T4 (11d)
- **M3 体育增强**: T7 (4d)
- **M3.5 tvSeries + 嵌入**: T6 + T6a (5d)
- **M4 Agent 编排**: T8 + T9 + T10 + T11 (9d)
- **M5 可观测与评估**: T12 + T13 (5d)
- **M6 生产加固**: T14 + T15 (7d)

并行时最快 9-10 日历周完成 M2–M6。

---

## 13. 未决事项 (需产品/架构确认后开工)

**既有 v0.1 遗留**
1. 多语言内容在 Postgres 的存储方式: 保留原始 `titles`/`descriptions` JSONB (当前方案) vs. 拆列 (多写冗余)。本文档默认 JSONB,如需高频查询具体语言字段再加物化列。
2. `sport_events.teams` 的主/客规范化: 当前保留 `home_team`/`away_team` 双字段 + jsonb 数组,仅在实体链接器置信度 ≥ 0.8 时填充。
3. `match_external_id` 的源选择优先级 (Sportradar vs Opta) — 需商务确认合同。
4. 会话记忆 (多轮对话) 是否纳入 v1 — 若是需加 `sessions` 表与 Redis 前置缓存。
5. GDPR 下 `device_id` 哈希方案 (salt 轮换、保留期) — 需法务确认。

**v0.2 多源融合新增**
6. **IMDB 字段级 schema**: 需客户提供实际字段清单 (是否含 cast / 长描述 / 多语言),影响 `build_merged_jsonb` 的字段列表与 §3.1a 优先级表边界。
7. **去重阈值 0.85**: 待评估集 (§10.3 的 200 条标注对) 回归后锁定;可能按国家/语言分层调参。
8. **低置信候选人工审核 UI**: §4.2 将候选推送 Service Bus,但审核 UI 与工作流未设计 (运营流程问题)。
9. **tvSeries rebuild 频率上限**: 当前 "10 集或 90 天" 兜底可能在大型长篇剧集 (数百集) 上产生成本峰值,需业务确认是否加上限 (如单系列每季度不超过 6 次)。
10. **VOD 孤儿**: VOD Kafka 到达时若 IMDB/simpleTV 均未匹配,当前进 dead_letter。需评估是否允许"先建 VOD-only title",待后续源补齐再 merge (会引入更多 schema 分支)。
11. **simpleTV Kafka 消息契约**: topic 名称、partition key (推荐按 channel_id)、schema registry 使用 (Avro/Protobuf/JSON) 需与数据组对齐。
12. **Event Hubs TU 规模**: 100K titles × 3-5 条/周 source_records 属于极小体量,1 TU 足够;待客户确认 VOD 全量条目数后复核。

---

> **下一步**: 本 LLD v0.2 评审通过后,按 §12 任务表立即进入实现阶段。迁移脚本、IaC、评估集 CI 建议第 1 周全部落地;去重阈值调参与 IMDB 字段对齐可并行进行。
