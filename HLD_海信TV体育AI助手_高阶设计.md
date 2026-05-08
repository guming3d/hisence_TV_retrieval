# 海信 TV 体育 AI 助手 — 高阶设计 (HLD)

> 版本: v0.2 (多源数据融合更新)
> 日期: 2026-05-07
> 数据样例: `1.2026-04-14.json` (simpleTV 单日样例: DR1 Denmark, 33 个 programs, 36 条 listings)
> 场景: 用户通过电视遥控器一键触发 AI 流程,用自然语言询问节目单、比分、球员/球队背景,以及可播放内容推荐。
>
> **v0.2 主要变化**: 数据层从"单源 simpleTV EPG"升级为"IMDB (主) + simpleTV (补充) + VOD (可播放标志)"三源融合。引入 `titles` 作为规范实体、Title Resolver 去重/合并服务、tvSeries 分层 RAG 策略。体育能力 (sport_events + 实体链接器) 全部保留,仅作用点由 `program_id` 迁移到规范 `title_id`。
>
> **2026-05-08 IMDB 样例补丁**: 新增 `tmdb_id` 作为第二精确键;扩充 titles 字段集(多分数 / 多语言 / age_ratings / runtime_sec / release_infos);softened IMDB genre 权威假设(样例显示 `categories` / `tags` / `theme` / `Mood` / `IABTaxonomy` 常为 null);payload 清洗规则(HTML entity unescape、字符串 `"null"` → SQL NULL、cast bio 条带化)明示。

---

## 1. 业务目标与范围

### 1.1 用户场景
海信电视用户按下遥控器 AI 键,通过语音或文本提问,系统在 **3–5 秒** 内返回答案,并支持一键跳转到对应频道。

### 1.2 支持的查询类型
| 类型 | 示例 | 数据来源 |
|---|---|---|
| **EPG 节目单查询** | "今晚有什么体育比赛?"、"皇马的比赛几点开始?"、"是直播还是重播?" | PostgreSQL (结构化, simpleTV) |
| **语义检索 / 内容推荐** | "那部关于丹麦收养的纪录片什么时候播?"、"找一个讲欧冠历史的节目"、"推荐一部轻松的喜剧" | Azure AI Search (RAG) — 基于 IMDB 主数据 |
| **实时比分/赛果** | "现在比分多少?"、"昨晚谁赢了?" | 体育数据 API (Sportradar/Opta) |
| **球员/球队知识** | "哈兰德是谁?"、"皇马上赛季战绩如何?" | Bing Grounding / 精选体育 KB |
| **可播放过滤 (横切)** | "我现在能看的欧冠比赛有哪些?"、"今晚点播里有什么好片?" | VOD `vod_playable` 标志 |

> **可播放默认**: 以内容推荐为目的的查询 (语义检索、"我能看什么") 默认附加 `require_playable=true` 过滤。EPG 节目单查询不强制(节目即将直播,即使 VOD 无副本,用户仍可"调台观看")。

### 1.3 非功能目标
- **端到端延迟**: p50 ≤ 2.5s,p95 ≤ 5s (TTFT,首 token 到达客户端)
- **可用性**: 99.9% 月度 SLA
- **数据新鲜度**: IMDB 手动批次 (周/月频率,运营驱动),simpleTV/VOD 通过 Kafka 每周消费,直播比分 ≤ 60s
- **初始容量**: AI Search 首期承载 ~100K titles (10GB tier,约 5% 占用,余量充足),后续按反馈扩展
- **多语言**: 首期支持丹麦语/英语/瑞典语/挪威语 (基于样例数据),架构需可扩展到中文等其他市场

---

## 2. 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│ 海信电视客户端 (遥控器 AI 键 → STT)                              │
└─────────────────────────────────────────────────────────────────┘
                              │ mTLS / JWT
                              ▼
                   ┌──────────────────────┐
                   │ Azure API Management │ (鉴权、限流、区域路由)
                   └──────────────────────┘
                              │
                              ▼
            ┌──────────────────────────────────────┐
            │  Foundry Agent Service (编排层)       │
            │  模型: GPT-5.4-mini (PoC 单模型)       │
            │                                      │
            │  Tools (Function Calling):           │
            │   1. search_programs  → AI Search    │
            │   2. query_schedule   → PostgreSQL   │
            │   3. get_live_scores  → Sports API   │
            │   4. web_grounding    → Bing         │
            │   5. tune_to_channel  → TV 回调       │
            └──────────────────────────────────────┘
                 │           │           │
                 ▼           ▼           ▼
    ┌──────────────────┐  ┌─────────────┐  ┌──────────────┐
    │ Azure AI Search  │  │ PostgreSQL  │  │ Redis (缓存) │
    │ (RAG 语义层,    │  │ Flexible    │  │ 语义缓存 +   │
    │  10GB tier,     │  │ Server      │  │ 比分 TTL +   │
    │  doc_type 区分)  │  │ (规范实体 +  │  │ 去重候选     │
    │                  │  │  排播 + VOD)│  │              │
    └──────────────────┘  └─────────────┘  └──────────────┘
            ▲                     ▲
            │                     │
            └──────────┬──────────┘
                       │
            ┌──────────────────────────────────────┐
            │  Title Resolver + Merge 服务           │
            │  1. dedupe: imdb_id → fuzzy title     │
            │  2. field-level merge (源优先级表)     │
            │  3. compute_merged(title_id) + CAS    │
            │  4. tvSeries rebuild 决策 (见 §4.3)   │
            │  5. 体育实体链接 (调用下游 Sportradar) │
            │  6. 嵌入 + AI Search mergeOrUpload    │
            └──────────────────────────────────────┘
                 ▲              ▲              ▲
                 │              │              │
   ┌─────────────┴──┐  ┌────────┴───────┐  ┌──┴───────────────┐
   │ IMDB Ingest   │  │ simpleTV Kafka │  │ VOD Kafka         │
   │ (blob-trigger)│  │ Consumer       │  │ Consumer          │
   │ ADLS /raw/    │  │ Event Hubs     │  │ Event Hubs        │
   │   imdb/       │  │ (weekly topic) │  │ (weekly topic)    │
   │ 手动批量/低频 │  │ simpletv.epg   │  │ vod.catalog       │
   └───────────────┘  └────────────────┘  └───────────────────┘
          ▲                   ▲                     ▲
          │                   │                     │
    IMDB 主数据          simpleTV 供应商        VOD 媒资库
    (运营驱动)          (周度推送)            (周度推送)
```

### 2.1 组件职责一览
| 组件 | 职责 | 关键技术 |
|---|---|---|
| **API Management** | 客户端接入、鉴权、限流、可观测性入口 | Azure APIM |
| **Foundry Agent** | LLM 编排、工具选择、流式响应 | Azure AI Foundry Agent Service |
| **PostgreSQL** | 规范实体 `titles` + 源记录快照 + 排播 + 体育元数据 + VOD 资产 | Azure DB for PostgreSQL Flexible Server |
| **AI Search** | RAG 语义检索:按 `doc_type ∈ {movie, series, episode}` 区分,统一索引支持混合检索 + 语义排序 | Azure AI Search Standard, 10GB tier |
| **Redis** | 语义缓存 + 实时比分缓存 + 去重归一化候选 | Azure Cache for Redis |
| **Title Resolver 服务** | 三源去重 (imdb_id → fuzzy title)、字段级 merge、tvSeries rebuild 决策、嵌入 orchestration | Azure Function + Python + pg_trgm |
| **IMDB 摄入管道** | 手动批量 JSON/CSV 落地 ADLS,blob-triggered Function 写入 `source_records` (source='imdb') | ADLS Gen2 + Azure Function |
| **simpleTV Kafka Consumer** | 周度消费 EPG 流,写入 `source_records` (source='simpletv'),触发 Title Resolver | Azure Event Hubs (Kafka API) + Function |
| **VOD Kafka Consumer** | 周度消费媒资流,写入 `vod_assets`,刷新 `titles.vod_playable` 标志 | Azure Event Hubs (Kafka API) + Function |
| **ADLS Gen2** | IMDB 原始 JSON 不可变归档 + Kafka dead-letter 落盘 | ADLS Gen2 |
| **体育实体链接器** | 从 title/description 抽取球队、赛事,关联 `match_external_id` (作用在规范 title_id 上) | Function + 规则 + Sportradar/Opta |

---

## 3. 数据层设计 (多源融合 + 结构化/非结构化双存储)

> **设计原则**:
> 1. **PostgreSQL 为权威 SoR,AI Search 为派生 RAG 索引**。任何时候 AI Search 可从 Postgres 完全重建。
> 2. **规范实体 `titles` 与源快照 `source_records` 分离**。字段级 merge 写入 `titles`,原始值永久留存在 `source_records` 中可审计/回滚。
> 3. **三源更新频率差异通过 Title Resolver 吸收**,Agent 工具层看不到源的存在。

### 3.0 数据源清单

| 源 | 角色 | 交付方式 | 频率 | 覆盖字段 |
|---|---|---|---|---|
| **IMDB** | 权威主数据 (master) | 手动批量 JSON/CSV 落地 ADLS `/raw/imdb/{batch_id}.json`,blob trigger | 运营驱动,典型月度/季度 | `imdb_id`、`tmdb_id`、`primary_title` + `multilang[{langCode,title,description,storyline}]`、`originalLangCode`、`originalCountries`、`releaseYear` + `releaseInfos[{country,date}]`、`runtime`(秒)、`images[{type,url}]`、`roles[{DIRECTOR\|STARRING\|WRITER, bio, actAs}]`、`score.{imdb,tmdb,tomato}`、`ageRating[{country,value}]`、`locations`、`BOPerfermance`。**注意**:`categories` / `subCategory` / `tags` / `theme` / `Mood` / `IABTaxonomy` 在样例中均为 null,**不能假设 IMDB 是 genre 权威源**(详见 §3.4 修订) |
| **simpleTV** | EPG 补充 (timely supplement) | Azure Event Hubs (Kafka API) topic `simpletv.epg.weekly` | 每周 | 频道、排播时间、多语言标题/描述、直播/重播、事件 ID |
| **VOD** | 可播放副本 (playability) | Azure Event Hubs (Kafka API) topic `vod.catalog.weekly` | 每周 | asset_url、DRM、分辨率、`playable` 标志、上下架时间 |

**源优先级总纲**: IMDB > simpleTV (元数据字段); simpleTV 独占 (排播字段); VOD 独占 (可播放字段)。详细字段级优先级见 §3.4。

### 3.0.1 存储栈选型依据 (PostgreSQL + Azure AI Search)

客户给出候选 "MySQL / PostgreSQL / Elasticsearch" 三选一。本设计选 **PostgreSQL (SoR) + Azure AI Search (语义派生层)**,理由如下。

**三个不可妥协的负载** (均由既有 HLD/LLD 决策推出):

| 负载 | 为何关键 |
|---|---|
| 归一化标题模糊去重 (trigram + GIN) | 100K 规模下 20–40ms/条,`pg_trgm` 原生 |
| 跨管道乐观并发 (`merge_version` CAS + `pg_advisory_xact_lock`) | 三管道同时写同一 canonical title 的唯一安全模型 |
| 排播时间范围查询 + tombstone 语义 (`tstzrange` + GIST,`status='active'` 部分索引) | EPG 查询热路径,tombstone 对账不可放弃 |

**三选一对照** (✓ 原生 / ✓* 可实现但有代价 / ✗ 不支持或不胜任):

| 负载 | PostgreSQL | MySQL | Elasticsearch |
|---|---|---|---|
| 模糊去重 (trigram) | ✓ (`pg_trgm`) | ✗ 需应用层 n-gram/MinHash | ✓* 语义不同,阈值需重调 |
| 乐观并发 / advisory lock | ✓ 原生 | ✓* 仅行锁,粒度粗 | ✗ 非 SoR,无事务 |
| `tstzrange` + GIST 时间范围 | ✓ 原生 | ✗ 无 range type | ✓* 需放弃 JOIN 语义 |
| JSONB 可索引 | ✓ (`jsonb` + GIN) | ✓* JSON 但索引弱 | ✓ 原生 |
| 作为 System of Record (ACID + PITR) | ✓ | ✓ | ✗ 非 SoR |
| 多表 JOIN (titles ← programs ← listings ← sport_events) | ✓ SQL | ✓ SQL | ✗ 需反规范化,破坏 titles 规范实体 |

**结论**:
- **PostgreSQL 是唯一同时命中三个必需负载的选项**。MySQL 折损 #1 和 #3,ES 在 #2/#3 上胜任,且 ES 不是 SoR(要在 Postgres 背后再做,与 AI Search 派生层角色重复)。
- **ES 已由 Azure AI Search 占据语义派生层**,再把 ES 当结构化存储会让两套 Lucene 引擎并存,增加不必要的运维压力。
- **Azure Database for PostgreSQL Flexible Server** 原生支持 `pg_trgm` / `pgvector` / `pg_stat_statements` / `pg_cron`,支持 Managed Identity、Private Endpoint、PITR ≤ 5min RPO (见 §9.3)。与 MySQL Flexible Server 同档成本。

**MySQL 回退路径** (仅当客户有硬性 MySQL 标准化 / DBA 储备时启用):
- Stage 2 去重改为独立服务(Azure Cognitive Services 语义相似度,或应用层 MinHash/LSH over `normalized_title`)。代价:+1 服务、+10–30ms/条、去重评估多组件化。
- `tstzrange` + GIST 改为 `(start_time, end_time)` btree + 应用侧范围对账。代价:周度批次对账 O(N)。
- Advisory lock 改为 Redis Redlock(按 `imdb_id`)。代价:跨服务失败模式、Redis HA 要求。
- **综合代价**: 追加 ~3–4 工程周 + 两项性能保证下降。

**Elasticsearch 路径**: 不适合。ES 非 SoR,且替代 Postgres 会要求把 titles/programs/listings/sport_events 反规范化为单文档,与 v0.2 规范实体模型冲突。

### 3.1 PostgreSQL Schema (结构化 + 规范实体)

五个核心表承载多源融合 + 精确过滤/时间范围/关联查询:

```
channels          (id, name, country, language, logo_url, updated_at)
                  -- 不变,仍由 simpleTV 填充

titles            (id, imdb_id?, tmdb_id?, primary_title, normalized_title,
                   original_lang, kind, release_year, series_id?,
                   runtime_sec?, production_countries jsonb,
                   titles_by_lang jsonb, descriptions_by_lang jsonb,
                   images jsonb, cast_merged jsonb, genres_merged jsonb,
                   ratings jsonb,     -- {imdb, tmdb, tomato},任一可 null
                   age_ratings jsonb, -- [{country, value}]
                   release_infos jsonb, -- [{country, date}]
                   vod_playable, rag_doc_version,
                   content_hash, merge_version,
                   updated_at)
                  -- ★ 新增:规范实体,AI Search 文档的逻辑 key
                  -- merge_version 为乐观锁,content_hash 驱动 AI Search 重嵌入
                  -- tmdb_id 为 IMDB 样例带来的第二精确键,参与去重 Stage 1b

source_records    (source_id, source_external_id, title_id?,
                   raw_payload jsonb, source_updated_at,
                   linker_metadata jsonb?, status,
                   PRIMARY KEY (source_id, source_external_id))
                  -- ★ 新增:每源原样快照,幂等 UPSERT
                  -- source_id ∈ {'imdb', 'simpletv'} (VOD 走 vod_assets,不落 source_records)

programs          (id, title_id FK, series_id, season_id, imdb_id,
                   category, kind, release_year, duration_sec,
                   attributes_jsonb, updated_at)
                  -- 保留,作为 simpleTV 侧"节目实例",为 listings 提供可链接锚点
                  -- title_id 指向规范实体,一个 title 可能有多条 programs (重播、跨频道)

program_genres    (program_id, genre_id, genre_name)
                  -- 不变

listings          (id, program_id FK, channel_id FK,
                   start_time, end_time, accurate_start, accurate_end,
                   airtime tstzrange GENERATED,
                   is_live, is_rerun, catchup,
                   broadcast_event_id, listing_content_hash,
                   status, tombstoned_at, last_seen_batch,
                   source_updated_at, updated_at)
                  -- 不变 (tombstone/status 已入库),新增:通过 programs.title_id 可上溯到规范实体

sport_events      (listing_id PK, title_id, sport, competition,
                   home_team, away_team, teams jsonb,
                   match_external_id,
                   linker_version, linker_confidence, linked_at,
                   source_content_hash)
                  -- 新增字段:title_id (冗余, 便于 title 粒度聚合查询)
                  -- 其余不变

vod_assets        (id, title_id FK, asset_url, drm, resolution,
                   quality, available_from, available_to,
                   vod_external_id, updated_at)
                  -- ★ 新增:VOD 侧可播放副本,many-per-title
                  -- titles.vod_playable 由 (∃ vod_assets WHERE now() BETWEEN available_from AND available_to) 物化
```

**关键索引** (增量):
- `UNIQUE(imdb_id) WHERE imdb_id IS NOT NULL ON titles` → 去重 Stage 1a
- `UNIQUE(tmdb_id) WHERE tmdb_id IS NOT NULL ON titles` → 去重 Stage 1b (imdb_id 未命中时兜底)
- `GIN(normalized_title gin_trgm_ops) ON titles` → 去重 Stage 2 (pg_trgm 模糊匹配)
- `btree(series_id) WHERE series_id IS NOT NULL ON titles` → tvSeries 聚合
- `btree(vod_playable) WHERE vod_playable = TRUE ON titles` → 可播放过滤热路径
- `btree(title_id) ON vod_assets`、`btree(title_id) ON programs`、`btree(title_id) ON sport_events` → title 粒度关联
- 已有:GIST(airtime)、channel_id+start_time 等 listings 索引全部保留

**为什么新增 `titles` 而不是直接在 `programs` 加 `source_id`**:
- 同一实体可能同时有 IMDB + simpleTV 两条源记录,单表无法承载 (3NF 违反)
- 字段级 merge 审计需要"原始值"与"合并值"分离,否则合并决策不可回溯
- 加入新源 (如 TMDB) 时,`source_records` 只需枚举值变更,无 schema 迁移

### 3.2 Azure AI Search 索引 (RAG 语义层)

**粒度决策 (v0.2 升级)**: 单索引,按 `doc_type` 字段区分三类文档,共享多语言/向量字段与语义排序配置。

| doc_type | key 构造 | 源 | 数量级 (100K titles 基准) |
|---|---|---|---|
| `movie` | `title:{title_id}` | IMDB/simpleTV 合并后的单片/综艺节目 | ~70K |
| `series` | `series:{series_id}` | tvSeries 聚合级文档 (拼接该系列所有集剧情 → LLM 摘要) | ~5K |
| `episode` | `episode:{title_id}` | (可选) 单集,用于"某集具体问题" | ~25K |

```
index: content-rag-v{n}   (通过别名 content-rag-current 实现蓝绿部署)

字段:
  doc_id                  (key,格式见上表)
  doc_type                (filterable, facetable: movie|series|episode)
  title_id                (filterable, 规范实体 FK)
  series_id               (filterable,仅 series/episode 文档有值)
  imdb_id                 (filterable)
  title_original          (searchable)
  title_local_da / _en / _sv / _no   (对应 microsoft 语言分词器)
  description_short_*     (同上,多语言)
  description_long_*      (同上,多语言;series 文档为 LLM 聚合后的系列剧情)
  genres                  (collection, filterable)
  category                (filterable, facetable)
  is_sports               (filterable)
  vod_playable            (filterable, facetable)   -- ★ 可播放过滤
  sport_meta              (complex: sport, competition, teams, match_external_id)
  content_vector          (Edm.Single[3072], text-embedding-3-large)
  imdb_rating             (filterable, sortable)
  rag_doc_version         (filterable)              -- 系列级重建计数器
  content_hash            (filterable)
  updated_at              (filterable, sortable)
```

**检索策略**:
- 混合检索 = BM25 + 向量,Top-50 候选
- 语义排序器 (Semantic Ranker) 重排至 Top-5
- 嵌入模型: `text-embedding-3-large` (3072 维,多语言表现优秀)
- **按 doc_type 过滤**:内容推荐默认 `doc_type in ('movie', 'series')` + `vod_playable eq true`;"某集具体剧情"才下探 `episode`
- **不存排播时间**:排播由 Postgres 负责,Search 返回 `title_id` + `series_id` 后由 Agent 调用 `query_schedule` 或 `query_vod` 工具拿时间/可播放地址

### 3.3 体育实体链接器 (质量关键,作用点迁移到 title_id)

**问题**: 样例数据的 33 个节目中体育类为 0,但真实 EPG 里标题常形如 `"Superliga: FC Midtjylland – Brøndby IF"`,没有结构化的球队字段。如果直接喂给 Agent,回答"今晚谁打谁"的质量会很差。

**解决方案** (在 Title Resolver 完成去重/merge 后、嵌入之前执行):
1. 过滤 `category = 'Sports'` 或 `genres_merged` 含 Sports 的 title/listing
2. 正则 + NER 从 merged title/description 抽取球队、赛事 token
3. 调用 Sportradar/Opta 解析为标准实体,关联 `match_external_id`
4. 写入 `sport_events` 表 (含 `title_id` 冗余列,便于 title 粒度聚合) + 补丁到 AI Search 的 `sport_meta`
5. 指标:抽取覆盖率、实体解析成功率 → 告警

**为何不再直接在单源 program 上跑**: 多源合并后的 title 文本更完整 (IMDB 权威 + simpleTV 本地化),链接器准确率显著提升。

### 3.4 字段级 merge 优先级表

Title Resolver 的 `compute_merged(title_id)` 函数按下表聚合 `source_records.*` 写入 `titles.*`。W = write (权威), R = read-only, — = 不参与。

| 字段 | IMDB | simpleTV | VOD | 合并规则 |
|---|---|---|---|---|
| `imdb_id` | **W** | W (兜底) | — | 精确键;simpleTV 自带 imdb_id 时也可写入 |
| `tmdb_id` | **W** | — | — | IMDB 专属,作为 imdb_id 之外的第二精确键(去重 Stage 1b) |
| `primary_title` / `original_title` | **W** | R | — | IMDB 权威;IMDB 缺失时回退 simpleTV |
| `titles_by_lang` | W (英 + 原文) | **W** (本地 DA/SV/NO) | — | union-by-lang;IMDB 覆盖同 lang 时胜出,否则 simpleTV 保留 |
| `description_long` | **W** | W (IMDB 缺失时) | — | IMDB 优先;无则 simpleTV |
| `descriptions_by_lang` | W (英 + 原文) | **W** (本地 DA/SV/NO) | — | 同 `titles_by_lang` 规则 |
| `ratings.{imdb, tmdb, tomato}` | **W** | — | — | 三分数独立存,任一可 null;`imdb_rating` 为 `ratings.imdb` 的快捷列 |
| ~~`genres_merged`~~ **(修订)** | W (若非空) | W (兜底) | — | **IMDB 非空时主,否则 simpleTV 主**;样例显示 `categories/tags/theme/Mood/IABTaxonomy` 常为 null,不能假设 IMDB 是权威源。最终为集合 union |
| `release_year` | **W** | R | — | IMDB 权威 |
| `release_infos` | **W** | R (兜底) | — | 多国发行日期 `[{country, date}]` |
| `runtime_sec` | **W** | R (兜底) | — | IMDB 权威,秒为单位 |
| `original_lang` / `production_countries` | **W** | R | — | IMDB 权威 |
| `age_ratings` | **W** | R | — | 按 `countryCode` 分桶,IMDB 权威 |
| `cast_merged` | **W** | — | — | IMDB only;**条带化存储** `{role_type, name, act_as, image_url}`,完整 bio 留在 `source_records.raw_payload` |
| `images` | **W** | W (兜底) | — | IMDB 权威(含 COVER 等类型),simpleTV 兜底 |
| `kind` | **W** | R | — | IMDB 权威 (movie/series/episode) |
| `series_id` | **W** | R (兜底) | — | IMDB 权威;IMDB 无则用 simpleTV 自带 series_id |
| `airtime` / `channel` / `live` / `rerun` | — | **W** | — | simpleTV only (通过 listings 表体现) |
| `vod_playable` / `asset_url` / `drm` | — | — | **W** | VOD only (通过 vod_assets 表物化) |
| `updated_at` | max | max | max | 三源最大值 |

**并发安全**: `titles.merge_version` 做 optimistic CAS; 同一 `imdb_id` 的三管道并发更新用 PG advisory lock `pg_advisory_xact_lock(hashtextextended(imdb_id, 0))` 分片序列化。

---

## 4. 数据注入管道 (三个数据源 → Title Resolver → 双存储)

### 4.1 整体流程

```
  ┌─────────────────┐   ┌──────────────────┐   ┌──────────────────┐
  │ IMDB 手动批次   │   │ simpleTV Kafka   │   │ VOD Kafka        │
  │ ADLS /raw/imdb/ │   │ simpletv.epg.*   │   │ vod.catalog.*    │
  │ (blob-trigger)  │   │ (Event Hubs)     │   │ (Event Hubs)     │
  └────────┬────────┘   └────────┬─────────┘   └────────┬─────────┘
           │                      │                       │
           ▼                      ▼                       ▼
    UPSERT source_records       UPSERT source_records    UPSERT vod_assets
    (source='imdb')             (source='simpletv')      (加/退全量副本记录)
           │                      │                       │
           │                      │                       │
           └──────┬───────────────┴───────────────────────┤
                  │ (按 imdb_id 分片 advisory lock)         │
                  ▼                                        │
    ┌──────────────────────────────────────┐               │
    │  Title Resolver                       │               │
    │  1. resolve_title(incoming)           │               │
    │     - Stage 1a: imdb_id 精确匹配      │               │
    │     - Stage 1b: tmdb_id 精确匹配       │               │
    │       (1a 未命中时兜底,IMDB 样例自带) │               │
    │     - Stage 2:  normalized_title      │               │
    │       pg_trgm 模糊匹配 (>= 0.85 +     │               │
    │       同 kind + year ±1)              │               │
    │     - Stage 3:  新建 canonical title  │               │
    │  2. compute_merged(title_id)          │               │
    │     按 §3.4 字段级优先级聚合          │               │
    │     titles.merge_version CAS          │               │
    │  3. 触发 programs/listings UPSERT     │               │
    │     (simpleTV 路径)                   │               │
    │  4. 快照对账 (仅 listings, 按批次)    │               │
    └──────────────────┬───────────────────┘               │
                       │                                    │
                       ▼                                    ▼
                ┌──────────────────────┐        刷新 titles.vod_playable
                │ 体育实体链接器        │        (∃ vod_assets WHERE now() ∈
                │ (在嵌入之前!)         │         [available_from, available_to])
                │ 仅作用在已去重的 title │◄────────────────┘
                │ 与其 listings 上      │
                └──────────┬───────────┘
                           │
                           ▼
                ┌──────────────────────┐
                │ tvSeries rebuild 决策 │  (见 §4.3)
                │ + 嵌入触发判定        │
                │ + content_hash 对比   │
                └──────────┬───────────┘
                           │
                           ▼
                ┌──────────────────────────────────┐
                │ 嵌入 (text-embedding-3-large)    │
                │ + AI Search mergeOrUpload        │
                │ + sport_meta patch               │
                │ + 指标上报 / 审计 / dead-letter   │
                └──────────────────────────────────┘
```

### 4.2 关键设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 原始数据归档 | ADLS Gen2 不可变 + Kafka dead-letter | 支持回放、回填、审计 |
| 结构化写入 | source_records UPSERT → `compute_merged` 再写 titles | 字段级 merge 可审计/回滚 |
| 去重策略 | `imdb_id` 精确 → `tmdb_id` 精确 → 归一化 title pg_trgm 模糊 (阈值 0.85 + kind + year±1) | 双精确键兜底(IMDB 样例含 `tmdbID`),再走模糊;兼顾召回与假阳性控制 |
| 嵌入触发 | 仅 titles.content_hash 变化或 series rebuild 决策命中 | 嵌入成本可降低 80–95% |
| 索引发布 | 蓝绿部署 (alias swap) | 失败可即时回滚 |
| 幂等性 | `(source_id, source_external_id)` 为 source_records PK;titles.merge_version CAS | 重跑/乱序/并发三重幂等 |
| tombstone 语义 | 仅 listings 用 (排播取消);titles 永不 tombstone | IMDB 主数据不因 simpleTV/VOD 失联而消失 |
| VOD 失联语义 | vod_playable ← FALSE (不删 title) | 不可播放只是过滤维度,实体身份仍在 |
| 实体链接重算 | 内容变化即重跑 | 上游修正标题/赛事后,比分查询仍能命中正确 match_id |
| 错误处理 | 坏数据进 `dead_letter/{source}/{date}.jsonl`,不阻塞主流程 | 可观测,可重放 |

### 4.3 tvSeries 分层处理与系列级 RAG 文档 rebuild 决策

RAG 用的是系列**整体**剧情,不是单集。因此单集更新到达时,不一定要重算系列级向量。

`should_rebuild_series_doc(series_id, incoming_episode)` 在以下任一条件成立时返回 True:

| 条件 | 说明 |
|---|---|
| A. 新季度首集 | `season_id` 前所未见 → 剧情阶段性推进 |
| B. 新集引入长描述且系列之前无 | `description_long > 200 字符` 且 `series_has_long_plot = FALSE` |
| C. 系列元数据漂移 | IMDB 刷新了 cast / genres / imdb_rating 等 title 级字段 |
| D. 定期兜底 | 自上次 rebuild 已 ≥ 10 集或 ≥ 90 天 |

不满足则仅写 `episode` 文档 + 更新结构化字段,跳过系列向量重算。`titles.rag_doc_version` 在每次 series rebuild 时递增,便于审计与灰度回滚。

### 4.4 数据质量关注点
- `schedule.accurate` (实际播出) vs `schedule.start_time` (预定) → 两个都入库,"is_on_now" 优先用 accurate
- 多语言标题回退链: 用户 locale → 原始语言 → 英语
- 时间统一 UTC 存储,客户端渲染时转时区
- `broadcast_ids.event` 作为跨系统关联键 (供 TV 回调使用)
- 同一事件在多频道重播 → 多条 listings 指向同一 program,多 programs 指向同一 title,Agent 返回"跨频道重播"提示
- **三源冲突处理**: IMDB 说 "release_year=2023", simpleTV 说 "2024" → IMDB 权威,simpleTV 记入 `merge_conflicts` 指标但不覆盖
- **孤儿 simpleTV 条目**: simpleTV 有但 IMDB 无匹配 → 允许 `titles.imdb_id IS NULL`,之后 IMDB 补丁进来时走 fuzzy title 匹配再合并
- **IMDB payload 清洗**: HTML entity(`&apos;` 等)必须 unescape **在 `content_hash` 计算之前**,否则上游修正编码会导致所有记录 churn 重嵌入
- **"null" 字符串语义**: IMDB 样例多处出现 `"null"`(字符串)表示缺失,非 JSON null;ingest normalizer 统一转 SQL NULL,否则 merge 规则中的"非空判定"(如 `genres_merged` 的 IMDB/simpleTV 权威切换)会失效
- **Cast 条带化**: `roles[].originalDesc` 可达 1KB+/人,`titles.cast_merged` 只保留 `{role_type, name, act_as, image_url}`;完整 bio 通过 `source_records.raw_payload` 审计获取,不进 RAG 文档

---

## 5. Agent 编排层设计

### 5.1 工具集
```
search_programs(
    query, locale, filters?,
    doc_type?,           # movie | series | episode (默认: ['movie','series'])
    require_playable?    # 默认 true:只返回 vod_playable = true 的文档
)
    → AI Search 语义检索
    → 返回 [{title_id, series_id?, doc_type, title, snippet, score,
              vod_playable, sport_meta?}]

query_schedule(
    title_ids?, program_ids?, channel_id?, time_range?, is_live?, country?,
    # 体育谓词 (与 sport_events 表关联,用于直接回答体育类问题)
    sport?,              # e.g. "football", "basketball"
    competition?,        # e.g. "Superliga", "UEFA Champions League"
    team?,               # e.g. "Real Madrid" (模糊匹配 sport_events.teams)
    has_match_id?,       # 仅返回已完成实体链接、可查比分的场次
    require_playable?    # 仅返回 titles.vod_playable = true 的排播 (可选)
)
    → PostgreSQL 结构化查询 (titles ← programs ← listings LEFT JOIN sport_events)
    → 返回 [{
          listing_id, program_id, title_id, title, channel, start, end,
          live, rerun, tune_url, vod_playable,
          # 体育元数据 (若该 listing 在 sport_events 中存在)
          sport?, competition?, teams?, match_external_id?
      }]
    → 说明: 返回值里的 match_external_id 可直接喂给 get_live_scores(),
           无需 Agent 再做二次检索

query_vod(title_ids)
    → 返回 [{title_id, vod_assets: [{asset_url, drm, resolution, ...}]}]
    → 用于"立即播放"按钮的 asset_url 解析

get_live_scores(match_external_id)
    → 体育数据 API (Redis 缓存 30–60s)

web_grounding(query)
    → Bing Grounding,用于球员/球队背景知识

tune_to_channel(channel_id)
    → 回调 TV 客户端,实现一键跳台

# 注: resolve_title 为 Title Resolver 服务内部工具,Agent 不直接调用
```

### 5.2 典型查询编排
| 用户问题 | 工具调用链 | 预估延迟 |
|---|---|---|
| "今晚有什么体育比赛?" | `query_schedule(sport='*', time_range=tonight, country='DK')` | ~50ms DB + LLM |
| "找那部丹麦收养的纪录片" | `search_programs(require_playable=false)` → `query_schedule(title_ids=[...])` | 语义 + 结构化 |
| "现在能看的欧冠比赛" | `search_programs(query='UEFA Champions League', doc_type='movie|series', require_playable=true)` | 单次 AI Search |
| "《绝命毒师》第 5 季" | `search_programs(query='绝命毒师', doc_type='series')` → `query_vod(title_ids=[...])` | 系列文档命中 + VOD 地址 |
| "DR1 今晚 8 点谁打谁?" | `query_schedule(channel='DR1', time_range=8pm±1h)` → 返回 teams + match_external_id | 结构化单次 |
| "皇马的比赛几点?" | `query_schedule(team='Real Madrid', time_range=next_7d)` | 结构化单次 |
| "哈兰德是谁?" | `web_grounding` | 背景知识 |
| "那个比赛比分多少?" | `query_schedule(...)` → `get_live_scores(match_external_id)` | 结构化 + API |

### 5.3 系统提示词契约
- 回复语言跟随 TV locale
- 涉及节目时必须给出 **频道 + 时间**
- 若比赛正在直播,必须提供"一键跳台"动作
- 返回结构化对象 (供客户端渲染卡片) + 自然语言 (供 TTS)
- 最大工具调用深度 = 3 (防止级联超时)

### 5.4 模型选型 (PoC 阶段单模型)

**PoC 阶段**: 仅使用 `GPT-5.4-mini` 一款模型,覆盖所有查询场景(EPG 时间窗、体育/比分/推荐、单轮问答、tvSeries 摘要写入)。

| 模型 | 场景 | 典型延迟 |
|---|---|---|
| `GPT-5.4-mini` | 全部在线查询 + 离线 tvSeries 摘要 | 首 token 400–600ms |

**理由**: PoC 的首要目标是验证端到端延迟和答案质量基线。引入多模型路由会增加升级判定、置信度校准、评估拆分等复杂度,应在 PoC 跑完、观察到确实存在 mini 无法胜任的查询类别之后再评估。

**后续升级路径 (v2+,非 PoC 范围)**: 若评估集显示多工具级联、跨系列对比、tvSeries 摘要等场景下 `GPT-5.4-mini` 答案质量不足,再引入 `GPT-5.4` 作为复杂推理回退,届时另行设计路由规则与升级触发条件。本 HLD 不预先绑定路由逻辑。

### 5.5 流式响应
LLM 首 token 通过 SSE 流回客户端,STT→TTS 管道即时朗读。这是 3–5s 体验流畅的关键。

---

## 6. 性能设计

### 6.1 延迟预算 (端到端 TTFT)
| 阶段 | 预算 |
|---|---|
| STT + 客户端到 APIM | 600–900 ms |
| Agent 规划 + 首次工具调用 | ~150 ms |
| AI Search 混合检索 + 语义排序 (含 `vod_playable` 过滤) | 200–500 ms |
| 实时比分工具 (按需) | 300–500 ms |
| PostgreSQL 结构化查询 (titles + listings JOIN) | 20–80 ms |
| LLM 首 token (流式,GPT-5.4-mini) | 400–600 ms |
| **端到端 TTFT** | **~1.8–2.8 s** (p50),**<5s** (p95) |

> **摄入路径延迟** (离线,不计入用户 TTFT): pg_trgm 模糊查重 20–40ms / 条; compute_merged CAS 约 10–20ms / 条; tvSeries rebuild (含 LLM 摘要) 约 1–3s / series, 但仅新季/显著变化触发。

### 6.2 性能关键措施
- **优先过滤而非语义重排**: 时间窗口、频道、体育标志用 Postgres/Search 过滤器下推
- **缓存分层**:
  - L1: Redis 语义缓存,键为 `(locale, country, normalized_query, time_bucket=5min)`
  - L2: Redis 实时比分缓存,直播期 30s TTL,赛后 24h
  - L3: Foundry 内置提示词缓存
- **模型**: PoC 阶段统一使用 `GPT-5.4-mini`(见 §5.4);多模型路由留待 v2+ 按评估结果再引入
- **并发工具调用**: 无依赖的工具调用并行发起 (Foundry Agent 原生支持)
- **冷启动规避**: Function 常驻 (Premium plan) 或用 App Service

---

## 7. 质量保障

### 7.1 评估体系
- **黄金问题集**: 50–100 条覆盖所有查询类型 (EPG / 比分 / 知识 / 多语言)
- **Foundry Evaluations**: CI 中运行,指标包括工具选择准确率、答案相关性、事实一致性
- **回归门禁**: 索引发布、模型变更、提示词变更前必须跑通评估集
- **A/B 试点**: 新版本先在 5% 流量灰度

### 7.2 数据质量指标 (可观测)
| 指标 | 目标 | 来源 |
|---|---|---|
| EPG 摄入完整率 (期望频道/实际) | ≥ 99% | 管道日志 |
| 体育实体链接器覆盖率 | ≥ 90% | 链接器指标 |
| AI Search 索引新鲜度 (最新 updated_at 延迟) | < 1h | 索引元数据 |
| Agent 工具选择准确率 | ≥ 95% | 评估集 |
| 答案相关性 (人工/LLM 评审) | ≥ 4/5 | 采样评审 |

### 7.3 内容安全
- 输入 + 输出均经 Azure AI Content Safety
- 输入过滤恶意 prompt (用户遥控器输入可能异常)
- 输出过滤仇恨、暴力等 (体育话题一般安全,仍需兜底)

---

## 8. 成本设计

### 8.1 主要成本项
| 项目 | 主要驱动因素 | 优化手段 |
|---|---|---|
| **LLM 推理** | 查询量 × 上下文长度 (GPT-5.4-mini,PoC 单模型) | 紧凑系统提示词、语义缓存;多模型路由留待 v2+ |
| **嵌入 (在线检索)** | 用户 query embedding 调用次数 | Redis 语义缓存命中 query embedding |
| **嵌入 (摄入)** | 变化的 titles × doc_type 数 | 按 `titles.content_hash` 变化检测;tvSeries rebuild 仅 4 条件命中时触发 (省 80–95%) |
| **tvSeries LLM 摘要** | 系列 rebuild 次数 | 定期兜底 + 显著变化触发,避免每集重算 |
| **AI Search** | 文档数 × 副本 × 分区 (含 doc_type=series 聚合) | 按规范 title 去重 (而非多源快照) |
| **PostgreSQL** | 计算 + 存储 (titles + source_records) | Flexible Server,按负载自动扩缩;source_records 可按月归档 |
| **Redis** | 内存容量 | 仅缓存热 query + 去重候选 (TTL 7d) |
| **Event Hubs (Kafka)** | 吞吐单元 (TU) × 保留天数 | 周度消费,1–2 TU 即可;保留 3 天便于重放 |
| **体育 API** | 调用次数 | 比分 30–60s 缓存,赛后长缓存 |

### 8.2 成本控制机制
- **工具调用深度上限**: ≤ 3,防止失控级联
- **请求配额**: APIM 按设备/区域限流
- **定期审计**: 月度成本报告按租户/查询类型分摊

### 8.3 成本预估口径 (100K 初始基准)

**规模验证** (100K titles):
- AI Search 存储:假设每文档 ~5KB (含 3072 维 vector) → ~500MB,10GB tier 余量 95%
- PostgreSQL:titles 100K + source_records (~2 源 avg,200K) + listings (~5 排播 avg,500K) + vod_assets (~1.5 副本 avg,150K) ≈ <1M 行级,Flexible Server Burstable 足够
- 初始嵌入:100K × 3072d (text-embedding-3-large) ≈ 单次 ~$30 USD
- 周度增量:假设 5% 新建 + 10% 系列 rebuild (decision-gated) → 月度嵌入 ~$45 USD

**仍需客户提供**:
- 日活设备数 (DAU)
- 人均日查询数
- IMDB 初始批次规模 + 周度/月度增量预期
- simpleTV/VOD Kafka 消息量级 (用于 Event Hubs TU 规划)
- 每周元数据变化率 (估算 merge + 重嵌入成本)

---

## 9. 运维卓越 (Operational Excellence)

### 9.1 可观测性
- **分布式追踪**: Foundry Traces + Application Insights,每次查询完整链路可回溯
- **关键埋点**:
  - `request_id, device_id, region, locale, channel_id`
  - `tool_calls[]` (每个工具耗时、命中与否)
  - `search_rerank_scores`
  - `llm_tokens_in/out`
  - `cache_hit/miss`
- **日志分层**: 结构化 JSON 日志,敏感字段脱敏 (device_id 哈希)
- **仪表盘**: 延迟 P50/P95/P99、错误率、工具调用分布、缓存命中率、成本日报

### 9.2 发布与回滚
| 资产 | 发布方式 | 回滚方式 |
|---|---|---|
| AI Search 索引 | 蓝绿 (别名切换) | 别名切回旧索引,秒级 |
| Postgres Schema | Flyway/Liquibase 迁移 | 向后兼容策略,必要时 PITR |
| Agent 提示词/工具定义 | 版本化,灰度 5%→25%→100% | 版本回退 |
| 模型版本 | Foundry Deployment,灰度 | 切回旧 deployment |
| 摄入管道 | ADF pipeline version | ADF 版本回滚 |

### 9.3 灾难恢复
- **Postgres**: PITR,RPO ≤ 5 min,RTO ≤ 30 min
- **AI Search**: 无原生备份,但可从 Postgres 完全重建,RTO ≤ 2h (取决于规模)
- **ADLS 原始区**: GRS 跨区复制,用于从源头回放
- **多区域**: 首期单区域,v2 再做多区域 Active-Passive

### 9.4 告警 (SRE)
| 告警 | 阈值 | 优先级 |
|---|---|---|
| 端到端 P95 延迟 | > 5s 持续 5min | P1 |
| Agent 错误率 | > 2% 持续 5min | P1 |
| 任一源 ingest 管道失败 | 单次失败 | P2 |
| IMDB 源 SLA 违反 | 无新批次 > 30 天 | P2 |
| simpleTV / VOD Kafka 消费堆积 | lag > 1 周 | P2 |
| AI Search 索引延迟 | > 2h 无新数据 | P2 |
| Title Resolver merge 冲突率 | > 5% | P2 |
| pg_trgm 模糊匹配低置信队列 | 人工审核积压 > 500 条 | P3 |
| 嵌入成本日环比 | > 150% | P3 |
| tvSeries 级联 rebuild | 单 24h 内 series rebuild > 2x 基线 | P3 |

### 9.5 安全与合规
- **身份**: Foundry → Search / Postgres / Storage 全部走 Managed Identity,无静态密钥
- **密钥**: Azure Key Vault,轮换策略 90 天
- **网络**: 私有终结点 (AI Search / Postgres / Storage),APIM 为唯一公网入口
- **设备认证**: TV 侧 mTLS 或签名 JWT,防止设备冒用
- **数据合规**: 欧盟市场需考虑 GDPR,日志中 PII 脱敏,用户查询不跨境存储
- **审计日志**: 所有管理操作留痕,保留 180 天

---

## 10. 待评审的关键决策

以下决策建议客户确认,不同选择会显著影响架构:

| # | 决策点 | 推荐 | 备选 |
|---|---|---|---|
| 1 | Search 文档粒度 | Title (canonical) 级,单索引按 `doc_type` 分 movie/series/episode | 多索引物理隔离 (运维复杂) |
| 2 | 多源数据模型 | `titles` 规范实体 + `source_records` 源快照分离 | 在 programs 表加 source_id 列 (3NF 违反) |
| 3 | tvSeries 文档策略 | 系列级聚合文档 (LLM 摘要) + 单集文档 (可选) | 仅集级 (剧情碎片化,语义差) |
| 4 | 去重阈值 (Stage 2) | pg_trgm similarity ≥ 0.85 + 同 kind + year ±1。**Stage 1 双精确键**: `imdb_id` → `tmdb_id`(IMDB 样例含 `tmdbID`) | 无阈值 (假阳泛滥) / 纯 embedding 相似度 (成本高) / 仅 imdb_id 单键 (错失 TMDB 兜底) |
| 5 | 源写入与 merge | 先写 source_records 无锁 UPSERT,再 advisory lock 下 compute_merged | 直接在 titles 做 pessimistic lock (并发瓶颈) |
| 6 | 体育知识库 | Bing Grounding + 精选 KB 双兜底 | 仅 Bing / 仅自建 KB |
| 7 | 嵌入模型 | text-embedding-3-large (3072d) | -small (更便宜,多语言稍弱) |
| 8 | LLM 模型 (PoC) | **GPT-5.4-mini 单模型** 覆盖全部场景,先验证延迟和质量基线;多模型路由留待 v2+ 按评估结果引入 | 直接采用 GPT-5.4-mini + GPT-5.4 双档 (PoC 即引入路由复杂度,不必要) / gpt-4o-mini (多语言稍弱) |
| 9 | Kafka 基础设施 | Azure Event Hubs (Kafka-compatible endpoint) | 自建 Confluent / 第三方托管 |
| 10 | IMDB 分发 | 手动批量 JSON/CSV 落 ADLS,blob-triggered Function | 走 Kafka (IMDB 静态源不匹配流式) |
| 11 | 多区域部署 | v1 单区域,v2 Active-Passive | 首期就多区域 (成本高) |
| 12 | 是否需要会话记忆 | 首期无,单轮 | 支持上下文,需要会话存储 |
| 13 | 体育实体链接器 | 规则 + Sportradar,作用在规范 title 上 | 纯 LLM 抽取 (成本高不稳定) |
| 14 | 结构化存储 (SoR) | **PostgreSQL Flexible Server** — `pg_trgm` 模糊去重、`merge_version` CAS + `pg_advisory_xact_lock` 并发、`tstzrange` + GIST 排播范围查询皆原生。详见 §3.0.1 | **MySQL Flexible Server** 可接受但需 +3–4 工程周(去重搬到应用层、`tstzrange` 降级、advisory lock 改 Redis Redlock)。**Elasticsearch** 拒绝(非 SoR,且与 AI Search 派生层角色重复) |

---

## 11. 交付路线图 (建议)

| 阶段 | 时长 | 里程碑 |
|---|---|---|
| **M1 架构 PoC** | 2 周 | 单频道、英语,端到端跑通 EPG 查询 |
| **M2 数据层** | 3 周 | Postgres + AI Search 双存储,摄入管道上线 |
| **M3 体育增强** | 2 周 | 实体链接器 + Sportradar 集成 |
| **M4 Agent 编排** | 2 周 | 四工具 + 流式响应 + 缓存 |
| **M5 可观测与评估** | 2 周 | 评估集、仪表盘、告警 |
| **M6 生产加固** | 2 周 | 私网、安全、压测、灾演 |
| **M7 灰度上线** | 2 周 | 5% → 25% → 100% 流量 |

**总计 ~15 周至生产全量**。

---

## 12. 评审问题清单

请客户在评审时反馈以下问题:

**数据与规模**
1. EPG 数据的最终规模 (国家数 × 频道数 × 语言) 是多少?
2. IMDB 主数据的字段级 schema (字段清单、是否含 cast/长描述/多语言) 与首批规模?  
   **(2026-05-08 已部分回答: 收到单条 movie 样例,确认包含 `imdbID` + `tmdbID` + 多语言数组 + 多分数 + roles bio + age_ratings + release_infos + runtime(秒);§3.0/§3.1/§3.4 已据此更新。仍待确认:**  
   **(a)** `categories` / `subCategory` / `tags` / `theme` / `Mood` / `IABTaxonomy` 是否在热门/主流作品上会有值,还是样例显示的"全部为 null"是常态(直接决定 IMDB 是否可作 genre 权威源);  
   **(b)** tvSeries + episode 样例(`series_id` / `season_id` 层级、单集是否拆独立 JSON 行),驱动 §4.3 系列级 rebuild 决策的实现;  
   **(c)** 每次批次是"全量快照"还是"增量 delta"—— 若是撤回型全量,需重新评估 §4.2 "titles 永不 tombstone" 决策;  
   **(d)** 首批规模与周/月增量量级,用于 compute_merged 并发与嵌入成本预算。)
3. simpleTV Kafka topic 的消息契约 (schema、retention、key 策略) 是否已定?
4. VOD 元数据可用字段 (DRM、分辨率、上下架时间、地区授权) 清单?
5. 三源更新频率是否有硬性 SLA (如 IMDB 最长间隔、simpleTV 周度窗口宽度)?

**基础设施与运维**
5a. **结构化存储选型**: HLD §3.0.1 论证了 **PostgreSQL Flexible Server** 为推荐方案(`pg_trgm` / `merge_version` CAS / `tstzrange` + GIST 三项关键负载原生支持)。请客户确认:
   - (a) 是否已有 **Postgres 运维实践 (DBA / 监控 / 备份策略)**,或需要从零搭建?
   - (b) 若客户有硬性 **MySQL 标准化** 要求,接受 §3.0.1 所述回退代价(~3–4 工程周 + 两项性能保证下降)吗?
   - (c) Elasticsearch 已拒绝作 SoR(非事务、非 ACID),但若客户期望 ES 扮演"可搜索结构化副本",应在 §3 之外单开讨论。

**业务与合规**
6. 是否已有体育数据 API 供应商合作 (Sportradar / Opta / 其他)?
7. 遥控器交互是否限定语音?是否需要屏幕虚拟键盘兜底?
8. 是否需要用户画像/个性化推荐 (涉及到用户 ID 和隐私策略)?
9. 目标市场是否包含中国大陆 (若是,模型选型和合规路径不同)?
10. 成本上限 (每月 / 每查询) 是否有硬预算?
11. 首期 GA 的目标国家和语言?
12. 与现有海信 AI 中台的集成关系?

**多源融合策略**
13. 去重 Stage 2 阈值 (0.85) 是否接受?是否有历史人工标注数据可用于调参?
14. 低置信度模糊匹配 (0.75–0.85) 是走人工审核队列,还是自动保留两条?
15. tvSeries 级 rebuild 的"定期兜底"频率 (推荐 90 天 / 10 集) 是否合适?
16. VOD 失联的语义 (推荐:`vod_playable=FALSE` 不 tombstone title) 是否与业务一致?

---

> **下一步**: 请客户评审本 HLD v0.2。评审通过后,LLD 同步升级为 v0.2,补齐 `titles` / `source_records` / `vod_assets` DDL、Title Resolver 伪代码、doc_type 索引字段、三源字段归属矩阵与新增评估集 (去重假阳/假阴、merge 优先级、tvSeries rebuild 触发)。
