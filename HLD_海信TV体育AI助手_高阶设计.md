# 海信 TV 体育 AI 助手 — 高阶设计 (HLD)

> 版本: v0.1 (待评审)
> 日期: 2026-04-25
> 数据样例: `1.2026-04-14.json` (EPG 单日数据: DR1 Denmark, 33 个 programs, 36 条 listings)
> 场景: 用户通过电视遥控器一键触发 AI 流程,用自然语言询问体育相关信息 (节目单、比分、球员/球队背景)。

---

## 1. 业务目标与范围

### 1.1 用户场景
海信电视用户按下遥控器 AI 键,通过语音或文本提问,系统在 **3–5 秒** 内返回答案,并支持一键跳转到对应频道。

### 1.2 支持的查询类型
| 类型 | 示例 | 数据来源 |
|---|---|---|
| **EPG 节目单查询** | "今晚有什么体育比赛?"、"皇马的比赛几点开始?"、"是直播还是重播?" | PostgreSQL (结构化) |
| **语义检索** | "那部关于丹麦收养的纪录片什么时候播?"、"找一个讲欧冠历史的节目" | Azure AI Search (非结构化) |
| **实时比分/赛果** | "现在比分多少?"、"昨晚谁赢了?" | 体育数据 API (Sportradar/Opta) |
| **球员/球队知识** | "哈兰德是谁?"、"皇马上赛季战绩如何?" | Bing Grounding / 精选体育 KB |

### 1.3 非功能目标
- **端到端延迟**: p50 ≤ 2.5s,p95 ≤ 5s (TTFT,首 token 到达客户端)
- **可用性**: 99.9% 月度 SLA
- **数据新鲜度**: EPG 每日批量更新,直播比分 ≤ 60s
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
            │  模型: gpt-5-mini (默认) / gpt-4o     │
            │  (复杂推理场景回退)                    │
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
    │ (语义+向量检索)   │  │ Flexible    │  │ 语义缓存 +   │
    │                  │  │ Server      │  │ 比分 TTL     │
    └──────────────────┘  └─────────────┘  └──────────────┘
            ▲                     ▲
            │                     │
            └──────────┬──────────┘
                       │
            ┌──────────────────────┐
            │   数据摄入管道 (每日)  │
            │   ADF / Azure Function│
            │   + 体育实体链接器     │
            └──────────────────────┘
                       ▲
            ┌──────────────────────┐
            │   ADLS Gen2 (原始区)  │
            │   每日 JSON 不可变存档 │
            └──────────────────────┘
                       ▲
                       │
              EPG 供应商每日推送
```

### 2.1 组件职责一览
| 组件 | 职责 | 关键技术 |
|---|---|---|
| **API Management** | 客户端接入、鉴权、限流、可观测性入口 | Azure APIM |
| **Foundry Agent** | LLM 编排、工具选择、流式响应 | Azure AI Foundry Agent Service |
| **PostgreSQL** | 结构化系统 of record:频道、节目、排播、体育赛事元数据 | Azure DB for PostgreSQL Flexible Server |
| **AI Search** | 非结构化语义检索:标题、长描述、多语言分词、向量 | Azure AI Search (混合检索 + 语义排序) |
| **Redis** | 语义缓存 + 实时比分缓存 | Azure Cache for Redis |
| **摄入管道** | 每日 JSON 落地、规范化、UPSERT、变化检测、嵌入 | ADF / Azure Function + Python |
| **ADLS Gen2** | 原始 JSON 不可变存档,支持回放/回填 | ADLS Gen2 |
| **体育实体链接器** | 从标题/描述抽取球队、赛事,关联 `match_external_id` | Function + 规则 + Sportradar/Opta |

---

## 3. 数据层设计 (核心:结构化 + 非结构化双存储)

> **设计原则**: PostgreSQL 为权威数据源 (System of Record),AI Search 为派生索引,任何时候可从 Postgres 完全重建。

### 3.1 PostgreSQL Schema (结构化数据)

承载需要 **精确过滤 + 时间范围 + 关联查询** 的字段:

```
channels          (id, name, country, language, logo_url, updated_at)
programs          (id, series_id, season_id, imdb_id, category, kind,
                   release_year, duration_sec, imdb_rating,
                   attributes_jsonb, updated_at)
program_genres    (program_id, genre_id, genre_name)
listings          (id, program_id, channel_id,
                   start_time, end_time, accurate_start, accurate_end,
                   airtime tstzrange GENERATED,
                   is_live, is_rerun, catchup,
                   broadcast_event_id,
                   status,                     -- 'active' | 'removed' (tombstone)
                   tombstoned_at,              -- 上游快照不再包含该 listing 的时间
                   updated_at)
sport_events      (listing_id, sport, competition, teams jsonb,
                   match_external_id)     -- 体育实体链接器产出
```

**关键索引**:
- `GIST(airtime) WHERE status = 'active'` → 时间范围查询 (如"今晚 8–10 点")
- `btree(channel_id, start_time) WHERE status = 'active'` → 单频道时间线
- `btree(program_id)` → AI Search 返回候选后的回查
- `btree(is_live) WHERE is_live AND status = 'active'` → 直播过滤
- `btree(tombstoned_at)` → 审计/清理 tombstone 行

**为什么是 Postgres 而不是全部放 AI Search**:
- `tstzrange` + GiST 索引查询 < 10ms,成本确定性高
- 支持关系 JOIN (program ↔ series ↔ season ↔ sport_event)
- UPSERT 基于 `updated_at` 字段,只更新变化行,节省 80%+ 写入成本
- PITR 备份支持灾难恢复

### 3.2 Azure AI Search 索引 (非结构化数据)

承载需要 **语义相似度 + 多语言分词 + 向量检索** 的字段。

**粒度决策**: **每个 `program_id` 一个文档** (不是每个 listing 一个)。原因:同一节目多次重播共享相同的描述文本,按 program 建索引避免重复嵌入,大幅降低成本。

```
index: epg-programs-v{n}   (通过别名 epg-current 实现蓝绿部署)

字段:
  program_id              (key)
  title_original          (searchable)
  title_local_da / _en / _sv / _no   (对应 microsoft 语言分词器)
  description_short_*     (同上,多语言)
  description_long_*      (同上,多语言)
  genres                  (collection, filterable)
  category                (filterable, facetable)
  is_sports               (filterable)
  sport_meta              (complex: sport, competition, teams)
  content_vector          (Edm.Single[3072], text-embedding-3-large)
  imdb_rating             (filterable, sortable)
```

**检索策略**:
- 混合检索 = BM25 + 向量,Top-50 候选
- 语义排序器 (Semantic Ranker) 重排至 Top-5
- 嵌入模型: `text-embedding-3-large` (3072 维,多语言表现优秀)
- **不存排播时间**:排播由 Postgres 负责,Search 返回 `program_id` 后由 Agent 调用 `query_schedule` 工具拿时间

### 3.3 体育实体链接器 (质量关键)

**问题**: 样例数据的 33 个节目中体育类为 0,但真实 EPG 里标题常形如 `"Superliga: FC Midtjylland – Brøndby IF"`,没有结构化的球队字段。如果直接喂给 Agent,回答"今晚谁打谁"的质量会很差。

**解决方案** (夜间异步任务):
1. 过滤 `category = 'Sports'` 或 `genres` 含 Sports 的节目
2. 正则 + NER 从 title/description 抽取球队、赛事 token
3. 调用 Sportradar/Opta 解析为标准实体,关联 `match_external_id`
4. 写入 `sport_events` 表 + 补丁到 AI Search 的 `sport_meta`
5. 指标:抽取覆盖率、实体解析成功率 → 告警

---

## 4. 摄入管道 (每日 JSON → 双存储)

### 4.1 流程
```
每日 JSON (如 1.2026-04-14.json)
  │
  ├─► ADLS 原始区:/raw/{country}/{channel}/{date}.json  (不可变归档)
  │
  ├─► Normalizer (Azure Function)
  │     │
  │     ├─► PostgreSQL: UPSERT channels / programs / listings / genres
  │     │     ├─ 基于 updated_at 比较,跳过未变化行
  │     │     └─ 标记本批次出现的 listing_id (用于下游对账)
  │     │
  │     ├─► 快照对账 (Reconciliation)
  │     │     对 (channel_id, 本批 date 覆盖的时间窗) 内的 listings:
  │     │       - 存在于 DB 但不在本批次 → 软删除 (status='removed', tombstoned_at=now)
  │     │       - query_schedule 默认过滤 status='active',避免返回已取消/替换的排播
  │     │       - 保留 tombstone 行 7 天便于审计与回溯
  │     │
  │     └─► 变化检测器
  │            │
  │            ├─ 节目 title/description 变化
  │            │    → 嵌入 (text-embedding-3-large)
  │            │    → AI Search mergeOrUpload
  │            │
  │            └─ 体育类 listings 需要 (重新) 链接的条件 (任一成立):
  │                 • 新增的体育类 listing
  │                 • 现有 listing 的 title / description / schedule / 竞赛相关字段 变化
  │                 • 现有 listing 对应 sport_events 行缺失或解析置信度低
  │                 → 实体链接器 → sport_events UPSERT (match_external_id 重算)
  │                              → AI Search sport_meta 补丁 (mergeOrUpload)
  │
  └─► 指标上报: UPSERT 行数、tombstone 行数、重建索引文档数、
                 嵌入 token 数、链接器覆盖率、链接器重跑率
```

### 4.2 关键设计决策
| 决策点 | 选择 | 理由 |
|---|---|---|
| 原始数据归档 | ADLS Gen2 不可变 | 支持回放、回填、审计 |
| 结构化写入 | Postgres UPSERT | 基于 `updated_at` 跳过未变化,降低 IO |
| 嵌入触发 | 仅文本变化时触发 | 嵌入成本可降低 80–95% |
| 索引发布 | 蓝绿部署 (alias swap) | 失败可即时回滚 |
| 幂等性 | `listing_id` 为天然主键 | 重跑管道不产生重复 |
| 删除语义 | 批内缺失 → 软删除 (tombstone) | 节目取消/替换时不再向用户返回幽灵排播 |
| 实体链接重算 | 内容变化即重跑 | 上游修正标题/赛事后,比分查询仍能命中正确 match_id |
| 错误处理 | 坏数据进 `dead_letter/`,不阻塞主流程 | 可观测,可重放 |

### 4.3 数据质量关注点
- `schedule.accurate` (实际播出) vs `schedule.start_time` (预定) → 两个都入库,"is_on_now" 优先用 accurate
- 多语言标题回退链: 用户 locale → 原始语言 → 英语
- 时间统一 UTC 存储,客户端渲染时转时区
- `broadcast_ids.event` 作为跨系统关联键 (供 TV 回调使用)
- 同一事件在多频道重播 → 多条 listings 指向同一 program,Agent 需对用户解释

---

## 5. Agent 编排层设计

### 5.1 工具集
```
search_programs(query, locale, filters?)
    → AI Search 语义检索
    → 返回 [{program_id, title, snippet, score, sport_meta?}]

query_schedule(
    program_ids?, channel_id?, time_range?, is_live?, country?,
    # 体育谓词 (与 sport_events 表关联,用于直接回答体育类问题)
    sport?,              # e.g. "football", "basketball"
    competition?,        # e.g. "Superliga", "UEFA Champions League"
    team?,               # e.g. "Real Madrid" (模糊匹配 sport_events.teams)
    has_match_id?        # 仅返回已完成实体链接、可查比分的场次
)
    → PostgreSQL 结构化查询 (listings LEFT JOIN sport_events)
    → 返回 [{
          listing_id, program_id, title, channel, start, end,
          live, rerun, tune_url,
          # 体育元数据 (若该 listing 在 sport_events 中存在)
          sport?, competition?, teams?, match_external_id?
      }]
    → 说明: 返回值里的 match_external_id 可直接喂给 get_live_scores(),
           无需 Agent 再做二次检索

get_live_scores(match_external_id)
    → 体育数据 API (Redis 缓存 30–60s)

web_grounding(query)
    → Bing Grounding,用于球员/球队背景知识

tune_to_channel(channel_id)
    → 回调 TV 客户端,实现一键跳台
```

### 5.2 典型查询编排
| 用户问题 | 工具调用链 | 预估延迟 |
|---|---|---|
| "今晚有什么体育比赛?" | `query_schedule(sport='*', time_range=tonight, country='DK')` | ~50ms DB + LLM |
| "找那部丹麦收养的纪录片" | `search_programs` → `query_schedule(program_ids=[...])` | 语义 + 结构化 |
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

### 5.4 流式响应
LLM 首 token 通过 SSE 流回客户端,STT→TTS 管道即时朗读。这是 3–5s 体验流畅的关键。

---

## 6. 性能设计

### 6.1 延迟预算 (端到端 TTFT)
| 阶段 | 预算 |
|---|---|
| STT + 客户端到 APIM | 600–900 ms |
| Agent 规划 + 首次工具调用 | ~150 ms |
| AI Search 混合检索 + 语义排序 | 200–500 ms |
| 实时比分工具 (按需) | 300–500 ms |
| PostgreSQL 结构化查询 | 20–80 ms |
| LLM 首 token (流式) | 400–800 ms |
| **端到端 TTFT** | **~1.8–2.8 s** (p50),**<5s** (p95) |

### 6.2 性能关键措施
- **优先过滤而非语义重排**: 时间窗口、频道、体育标志用 Postgres/Search 过滤器下推
- **缓存分层**:
  - L1: Redis 语义缓存,键为 `(locale, country, normalized_query, time_bucket=5min)`
  - L2: Redis 实时比分缓存,直播期 30s TTL,赛后 24h
  - L3: Foundry 内置提示词缓存
- **模型选择**: 默认 `gpt-5-mini`,仅在 Agent 低置信时升级至 `gpt-4o`
- **并发工具调用**: 无依赖的工具调用并行发起 (Foundry Agent 原生支持)
- **冷启动规避**: Function 常驻 (Premium plan) 或用 App Service

---

## 7. 质量保障

### 7.1 评估体系
- **黄金问题集**: 50–100 条覆盖所有查询类型 (EPG / 比分 / 知识 / 多语言)
- **Foundry Evaluations**: CI 中运行,指标包括工具选择准确率、答案相关性、事实一致性
- **回归门禁**: 索引发布、模型升级、提示词变更前必须跑通评估集
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
| **LLM 推理** | 查询量 × 上下文长度 | 默认小模型、紧凑系统提示词、语义缓存 |
| **嵌入** | 变化的节目文本量 | 变化检测,仅变化才重嵌入 (省 80–95%) |
| **AI Search** | 文档数 × 副本 × 分区 | 按 program_id 去重 (而非 listing) |
| **PostgreSQL** | 计算 + 存储 | Flexible Server,按负载自动扩缩 |
| **Redis** | 内存容量 | 仅缓存热 query,TTL 严格 |
| **体育 API** | 调用次数 | 比分 30–60s 缓存,赛后长缓存 |

### 8.2 成本控制机制
- **模型降级策略**: 简单查询 (EPG lookup) 用 mini,复杂查询 (多轮推理) 用大模型
- **工具调用深度上限**: ≤ 3,防止失控级联
- **请求配额**: APIM 按设备/区域限流
- **定期审计**: 月度成本报告按租户/查询类型分摊

### 8.3 成本预估口径 (待供数据量后细化)
需要客户提供:
- 日活设备数 (DAU)
- 人均日查询数
- EPG 覆盖频道数 × 国家 × 语言
- 每日节目变化率 (估算嵌入成本)

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
| EPG 管道失败 | 单次失败 | P2 |
| AI Search 索引延迟 | > 2h 无新数据 | P2 |
| 嵌入成本日环比 | > 150% | P3 |

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
| 1 | Search 文档粒度 | Program 级 | Listing 级 (冗余大) |
| 2 | 体育知识库 | Bing Grounding + 精选 KB 双兜底 | 仅 Bing / 仅自建 KB |
| 3 | 嵌入模型 | text-embedding-3-large (3072d) | -small (更便宜,多语言稍弱) |
| 4 | 默认 LLM | gpt-5-mini | gpt-4o-mini (更便宜) / gpt-4o (更准) |
| 5 | 多区域部署 | v1 单区域,v2 Active-Passive | 首期就多区域 (成本高) |
| 6 | 是否需要会话记忆 | 首期无,单轮 | 支持上下文,需要会话存储 |
| 7 | 体育实体链接器 | 规则 + Sportradar | 纯 LLM 抽取 (成本高不稳定) |

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

1. EPG 数据的最终规模 (国家数 × 频道数 × 语言) 是多少?
2. 是否已有体育数据 API 供应商合作 (Sportradar / Opta / 其他)?
3. 遥控器交互是否限定语音?是否需要屏幕虚拟键盘兜底?
4. 是否需要用户画像/个性化推荐 (涉及到用户 ID 和隐私策略)?
5. 目标市场是否包含中国大陆 (若是,模型选型和合规路径不同)?
6. 成本上限 (每月 / 每查询) 是否有硬预算?
7. 首期 GA 的目标国家和语言?
8. 与现有海信 AI 中台的集成关系?

---

> **下一步**: 请客户评审本 HLD。评审通过后,我将进入详细设计 (LLD),包括 Postgres DDL、AI Search 索引 JSON、Agent 工具 Schema、摄入管道代码骨架与评估集设计。
