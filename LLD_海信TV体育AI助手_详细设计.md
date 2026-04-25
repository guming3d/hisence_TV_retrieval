# 海信 TV 体育 AI 助手 — 详细设计 (LLD)

> 版本: v0.1 (供开发实施)
> 日期: 2026-04-25
> 上游文档: `HLD_海信TV体育AI助手_高阶设计.md` (v0.1)
> 读者: 后端 / 数据工程 / ML 工程开发者
> 产出目标: 本文档内容可直接转化为代码、schema 变更、索引配置与 CI 任务

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
| AI Search 索引 | `epg-programs-v{n}` (别名 `epg-current`) | `epg-programs-v1` |

### 1.3 技术栈锁定
- PostgreSQL **16**, Azure Flexible Server, 扩展: `btree_gist`, `pg_trgm`, `unaccent`
- Azure AI Search (2024-07-01 GA API),Standard S1 起步
- Azure Functions (Python 3.11, isolated) 作摄入 Worker
- Embeddings: `text-embedding-3-large` via Azure OpenAI
- Agent: Azure AI Foundry Agent Service,`gpt-5-mini` 主模型

---

## 2. PostgreSQL 详细设计

### 2.1 DDL (生产可执行)

```sql
-- =============================================================
-- V202604250900__init.sql
-- =============================================================
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- 枚举类型 -----------------------------------------------------
CREATE TYPE listing_status AS ENUM ('active', 'removed');
CREATE TYPE program_kind   AS ENUM ('episode', 'movie', 'show', 'clip', 'other');

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

-- 摄入批次审计 -------------------------------------------------
CREATE TABLE ingest_batches (
    id                  BIGSERIAL     PRIMARY KEY,
    source_path         TEXT          NOT NULL,       -- ADLS raw path
    country             CHAR(2)       NOT NULL,
    channel_id          INTEGER       NOT NULL,
    coverage_start      TIMESTAMPTZ   NOT NULL,
    coverage_end        TIMESTAMPTZ   NOT NULL,
    batch_time          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    programs_upserted   INTEGER       NOT NULL DEFAULT 0,
    listings_upserted   INTEGER       NOT NULL DEFAULT 0,
    listings_tombstoned INTEGER       NOT NULL DEFAULT 0,
    programs_reembedded INTEGER       NOT NULL DEFAULT 0,
    sport_events_relinked INTEGER     NOT NULL DEFAULT 0,
    status              TEXT          NOT NULL DEFAULT 'success', -- success|partial|failed
    error_detail        TEXT
);
CREATE INDEX ix_ingest_batches_channel_time ON ingest_batches(channel_id, batch_time DESC);
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

### 2.4 连接池与并发
- 使用 **PgBouncer** (transaction pooling),应用侧 `psycopg[pool]` 或 `asyncpg.Pool`
- 推荐连接数: `min=2, max=10` per worker pod
- 批量写入使用 `COPY FROM STDIN` 进入临时表,然后 `INSERT ... ON CONFLICT` 合并
- 事务隔离: 默认 `READ COMMITTED`;对账步骤用单事务 + `FOR UPDATE` 防并发批次冲突

---

## 3. Azure AI Search 索引详细设计

### 3.1 索引定义 (`epg-programs-v1`)

```json
{
  "name": "epg-programs-v1",
  "fields": [
    { "name": "program_id",          "type": "Edm.String", "key": true, "filterable": true },
    { "name": "imdb_id",             "type": "Edm.String", "filterable": true },
    { "name": "series_id",           "type": "Edm.Int64",  "filterable": true },
    { "name": "season_id",           "type": "Edm.Int64",  "filterable": true },

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

### 3.2 别名与发布
```http
PUT /aliases/epg-current?api-version=2024-07-01
{
  "name": "epg-current",
  "indexes": ["epg-programs-v1"]
}
```
升级流程:
1. 创建 `epg-programs-v2`
2. Reindex (从 Postgres 权威数据 + 嵌入)
3. 运行评估集 (Recall@5, NDCG@10) 对比 v1
4. 通过 → `PUT /aliases/epg-current` 指向 v2
5. 保留 v1 7 天,用于紧急回滚

### 3.3 查询模板

**混合检索 (语义 + 向量)**
```json
POST /indexes/epg-current/docs/search?api-version=2024-07-01
{
  "search": "丹麦王室 纪录片",
  "queryType": "semantic",
  "semanticConfiguration": "epg-semantic",
  "vectorQueries": [
    { "kind": "vector",
      "vector": [/* 3072-dim */],
      "fields": "content_vector",
      "k": 50 }
  ],
  "searchFields": "title_local_da,description_long_da,title_original",
  "filter": "is_sports eq false and production_countries/any(c: c eq 'DK')",
  "top": 10,
  "select": "program_id,title_original,title_local_da,description_short_da,genres,sport_meta"
}
```

**体育类严格过滤**
```
filter: "is_sports eq true and sport_meta/competition eq 'Superliga'"
```

### 3.4 文档写入契约
- Key: `program_id` (string, Postgres bigint 转字符串)
- 写入通过 `mergeOrUpload`,只在 `content_hash` 变化时推送
- 批量大小: 1000 文档/请求,错误重试指数退避 (max 3)
- 嵌入失败的文档不写入向量字段,记录到死信队列

---

## 4. 数据摄入管道详细设计

### 4.1 流程图 (伪代码)

**关键顺序**: 体育实体链接必须在嵌入之前完成,因为嵌入文本 (§4.4) 包含 `sport / competition / teams`,嵌入 hash 也依赖这些字段。如果先嵌入再链接,新上线的体育节目向量会缺少球队/赛事语义,后续体育语义查询会失真,且再也不会触发重嵌入。

```python
# entrypoint: Azure Function blob-triggered on ADLS /raw/{country}/{channel}/{date}.json

def ingest(raw_path: str) -> BatchResult:
    batch_time = now_utc()
    payload = load_json(raw_path)
    country, channel_id, cov_start, cov_end = infer_coverage(raw_path, payload)

    # 1. UPSERT channels / programs / listings --------------------
    with pg.transaction():
        upsert_channels(payload["channels"], batch_time)
        prog_changes  = upsert_programs(payload["programs"], batch_time)
        list_changes  = upsert_listings(payload["listings"], batch_time)

        # 2. 快照对账 (tombstone) --------------------------------
        tombstoned = tombstone_missing(channel_id, cov_start, cov_end, batch_time)

    # 3. 体育实体链接 (先于嵌入,保证嵌入输入里 sport_meta 是最新的) --
    relink_targets = find_relink_candidates(
        channel_id, cov_start, cov_end,
        list_changes, prog_changes
    )
    link_result = linker.link_batch(relink_targets)
        # link_result 返回受影响的 program_id 集合,用于触发下游重嵌入

    # 4. 变化检测 + 嵌入 + AI Search 同步 ------------------------
    #    重嵌入触发条件 (任一):
    #      a. 节目文本 hash 变化 (title/description)
    #      b. 本批次体育链接有变动,且该 program 仍有 active 的体育 listing
    #    二者都会导致 §4.4 的嵌入输入变化 -> content_hash 变化 -> 重算向量
    reembed_targets = compute_reembed_targets(
        prog_changes, link_result, active_sport_program_ids=True
    )
    embed_and_push(reembed_targets)

    # 5. 审计 ----------------------------------------------------
    record_batch_result(country, channel_id, cov_start, cov_end,
                        batch_time, prog_changes, list_changes,
                        tombstoned, reembed_targets, relink_targets)
```

`compute_reembed_targets` 说明: 对每个候选 program,按 §4.4 拼接嵌入输入 (包含最新 `sport_meta`),计算 `new_content_hash`;只有当 `new_content_hash ≠ programs.content_hash` 时才加入重嵌入集合。这样既保证 sport_meta 变化会触发重嵌入,又不会因为无关扰动导致冗余嵌入。

### 4.2 `find_relink_candidates` 规则 (关键)

```python
def find_relink_candidates(channel_id, cov_start, cov_end,
                            list_changes, prog_changes) -> list[ListingId]:
    sql = """
    SELECT l.id
      FROM listings l
      JOIN programs p ON p.id = l.program_id
      LEFT JOIN sport_events se ON se.listing_id = l.id
     WHERE l.channel_id = %s
       AND l.status = 'active'
       AND l.airtime && tstzrange(%s, %s, '[)')
       AND (
            p.category = 'Sports' OR
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
    return pg.fetchall(sql, [channel_id, cov_start, cov_end,
                              [c.id for c in list_changes]])
```

### 4.3 幂等性与重复处理
- 每批次以 `ingest_batches` 记录 + 行级 `source_updated_at` 比较保证幂等
- 失败分类:
  - **源数据坏**: 移入 `dead_letter/{country}/{channel}/{date}.json`,告警 P2
  - **下游 API 失败** (嵌入/AI Search/Sportradar): 重试 3 次 + 指数退避,最终失败记入 `ingest_batches.status='partial'`,下一批次自动补跑
- 并发保护: 单 `(channel_id, date)` 摄入用 PG advisory lock (`pg_advisory_xact_lock(hash(channel_id, date))`)

### 4.4 嵌入文本规范
单条文档的嵌入输入 (拼接顺序固定,便于 hash):
```
{title_original}\n
{title_local_<preferred_lang>}\n
{description_short_<preferred_lang>}\n
{description_long_<preferred_lang>}\n
genres: {g1, g2, ...}\n
sport: {sport}\ncompetition: {competition}\nteams: {t1, t2}
```
`content_hash = sha256(上述拼接)`。

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
  "description": "语义检索节目元数据,适用于描述模糊、语义相关的查询(如'讲欧冠历史的节目')。",
  "parameters": {
    "type": "object",
    "required": ["query", "locale"],
    "properties": {
      "query":   { "type": "string", "description": "用户自然语言查询" },
      "locale":  { "type": "string", "enum": ["da","en","sv","no"] },
      "filters": {
        "type": "object",
        "properties": {
          "is_sports":  { "type": "boolean" },
          "category":   { "type": "string" },
          "genres":     { "type": "array", "items": { "type": "string" } },
          "country":    { "type": "string", "pattern": "^[A-Z]{2}$" }
        },
        "additionalProperties": false
      },
      "top":     { "type": "integer", "minimum": 1, "maximum": 20, "default": 5 }
    }
  }
}
```

### 6.2 `query_schedule`
```json
{
  "name": "query_schedule",
  "description": "按时间/频道/体育谓词查询排播,返回 listing + sport_events 合并视图。体育类问题首选此工具。",
  "parameters": {
    "type": "object",
    "properties": {
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
    "required": ["listing_id","program_id","channel","start","end"],
    "properties": {
      "listing_id":        { "type": "integer" },
      "program_id":        { "type": "integer" },
      "title":             { "type": "string" },
      "channel":           { "type": "string" },
      "channel_id":        { "type": "integer" },
      "start":             { "type": "string", "format": "date-time" },
      "end":               { "type": "string", "format": "date-time" },
      "is_live":           { "type": "boolean" },
      "is_rerun":          { "type": "boolean" },
      "tune_url":          { "type": "string" },
      "sport":             { "type": "string" },
      "competition":       { "type": "string" },
      "teams":             { "type": "array", "items": { "type": "string" } },
      "match_external_id": { "type": "string" }
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
你是海信电视的体育节目助手。严格遵守以下规则:
1. 回复语言必须与入参 locale 一致。
2. 涉及节目时必须同时给出频道与时间;若正在直播,追加一个 action=tune_to_channel。
3. 体育类问题优先调用 query_schedule (支持 sport/competition/team 过滤);
   仅当 query_schedule 无结果时才 fallback 到 search_programs。
4. 比分类问题: 必须先从 query_schedule 结果中拿到 match_external_id,
   再调用 get_live_scores;不得凭空编造比分。
5. 球员/球队背景类问题使用 web_grounding;不得用于实时比分。
6. 工具调用深度 ≤ 3;无结果时直接告知用户,不要反复重试。
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

规范化查询: 小写 → unaccent → 去标点 → 连续空白压缩。

---

## 9. 可观测性埋点字段表

每次请求写入一条结构化日志:
```
request_id, device_id_hash, region, locale,
start_ts, end_ts, ttft_ms, total_ms,
model_name, prompt_tokens, completion_tokens,
cache_level_hit,                 -- none | semantic | score | schedule
tool_calls: [
  { name, start_ms, duration_ms, ok, error_code,
    args_digest, result_size }
],
search_metrics: { bm25_top, vector_top, rerank_scores_top3 },
sql_metrics:    { rows, elapsed_ms, index_hit },
final_status                     -- ok | partial | error
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
- 嵌入 `content_hash` 稳定性:同输入两次必须相同

### 10.2 集成测试
- E2E: 摄入样例 `1.2026-04-14.json` → 跑一遍 → Postgres 33 programs/36 listings,AI Search `epg-programs-v1` 33 docs
- Agent 工具调用: mock 5 个工具,跑 §5.2 的 6 条典型查询,断言工具调用链正确

### 10.3 评估集 (持续运行)
- 50 条黄金 query,分 5 类 (EPG / 体育 / 比分 / 知识 / 多语言)
- 指标: 工具选择准确率、答案相关性 (LLM-as-judge 4/5 起)、事实一致性
- 门禁: PR 合并前必须通过;索引重建后必须通过;周度报告

### 10.4 压测
- k6 脚本,10 → 200 QPS 阶梯,观察 p95 < 5s 的最大承载
- 热点场景: "今晚有什么体育比赛" 占 40% 流量 (缓存命中率应 > 80%)

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
| T1 | PG DDL + 迁移脚本 | 2d | — |
| T2 | Ingest Function 骨架 (raw → upsert) | 3d | T1 |
| T3 | 对账 + 幂等 + 批次审计 | 2d | T2 |
| T4 | 变化检测 + 嵌入 + AI Search push | 3d | T2 |
| T5 | AI Search 索引创建 + 别名脚本 | 1d | — |
| T6 | 实体链接器 (规则 + Sportradar) | 4d | T3 |
| T7 | Agent 工具后端 (search/schedule/scores/tune) | 4d | T1,T5 |
| T8 | Agent 注册 + 系统提示词 | 1d | T7 |
| T9 | API Gateway + SSE 流式端点 | 2d | T8 |
| T10| Redis 缓存层 | 1d | T9 |
| T11| 可观测性埋点 + 仪表盘 | 2d | T9 |
| T12| 评估集 + CI 门禁 | 2d | T8 |
| T13| 压测 + 调优 | 2d | T9 |
| T14| IaC + 环境部署 | 3d | All |
| **合计** | | **~32 人日** | |

并行时最快 6-7 日历周完成 M2–M5。

---

## 13. 未决事项 (需产品/架构确认后开工)

1. 多语言内容在 Postgres 的存储方式: 保留原始 `titles`/`descriptions` JSONB (当前方案) vs. 拆列 (多写冗余)。本文档默认 JSONB,如需高频查询具体语言字段再加物化列。
2. `sport_events.teams` 的主/客规范化: 当前保留 `home_team`/`away_team` 双字段 + jsonb 数组,仅在实体链接器置信度 ≥ 0.8 时填充。
3. `match_external_id` 的源选择优先级 (Sportradar vs Opta) — 需商务确认合同。
4. 会话记忆 (多轮对话) 是否纳入 v1 — 若是需加 `sessions` 表与 Redis 前置缓存。
5. GDPR 下 `device_id` 哈希方案 (salt 轮换、保留期) — 需法务确认。

---

> **下一步**: 本 LLD 评审通过后,按 §12 任务表立即进入实现阶段。迁移脚本、IaC、评估集 CI 建议第 1 周全部落地,避免后期返工。
