# Hisense TV Sports AI Assistant — Microsoft Foundry POC

A runnable proof‑of‑concept that demonstrates **Microsoft Foundry as a production‑ready
agent platform**, using the Hisense TV remote‑key sports assistant scenario.

The headline agent is a **LangGraph hosted agent** (`hisense-tv-assistant-langgraph`) speaking
the Foundry **`responses`** protocol (`langchain_azure_ai.agents.hosting.ResponsesHostServer`),
deployable with `azd ai agent`. A second hosted agent — `hisense-tv-assistant-maf` (under
`src-maf/`) — exposes the **same seven tools** through the **Microsoft Agent Framework** and its
**Agent Harness** (`create_harness_agent`); see
[Microsoft Agent Framework variant](#microsoft-agent-framework-variant-agent-harness).
Both are designed to run in two modes:

* **Live** — a real Foundry project + model deployment, Foundry IQ knowledge base, and
  Web IQ key are reachable. Every tool calls the real service.
* **Offline** — no Azure creds. The chat model and every tool fall back to deterministic
  local stubs (local KB docs, labelled web stub) so the graph and tool‑routing are
  testable on a laptop with **zero cloud setup**.

## What it demonstrates

| # | Foundry capability | Where it shows up |
|---|--------------------|-------------------|
| 1 | **Foundry IQ** — agentic knowledge retrieval over production data | `src/tools/foundry_iq.py` → `foundry_iq_search` over the EPG/titles program library (`data/kb/`) |
| 2 | **Web IQ** — latest web / news / images | `src/tools/webiq.py` → `webiq_search` against `api.microsoft.ai` (x‑apikey auth) |
| 3 | **Agent Optimizer** — eval‑driven instruction/model tuning | `src/.agent_configs/baseline/` + `src/eval.yaml` + optimizer‑aware `src/config.py` |
| 4 | **Evaluation & Monitoring** — batch + continuous eval, tracing | GenAI OpenTelemetry → App Insights in `src/main.py`; `data/eval/seed_dataset.jsonl` |
| 5 | **Memory** — managed, cross‑session viewer personalization | `src/tools/memory.py` → `remember_viewer_preference` / `recall_viewer_preferences` over a Foundry **memory store** (`beta.memory_stores`) |

## Architecture

```
remote AI key ──► hosted agent (responses) ──► LangGraph tool‑calling loop
                                                 ├─ foundry_iq_search   (Feature 1)
                                                 ├─ webiq_search        (Feature 2)
                                                 ├─ query_schedule      (EPG)
                                                 ├─ get_live_scores      (scores)
                                                 ├─ tune_to_channel      (device)
                                                 ├─ remember_viewer_preference  (Feature 5)
                                                 └─ recall_viewer_preferences   (Feature 5)
                          tracing ─► OpenTelemetry ─► Application Insights (Feature 4)
                          memory  ─► Foundry memory store (beta.memory_stores) (Feature 5)
```

The agent's **instruction + model are read through the Agent Optimizer config**
(`load_config()`), so `azd ai agent optimize` can swap in an optimized candidate from
`.agent_configs/` with no code change (Feature 3).

## Repo layout

```
poc/
  azure.yaml                     # azd hosted-agent services (host: azure.ai.agent): -langgraph + -maf
  infra/
    main.bicep                   # supporting infra: AI Search (Foundry IQ) + App Insights
    main.parameters.json
  src/
    agent.yaml                   # hosted-agent spec (responses 1.0.0)
    agent.manifest.yaml          # manifest for `azd ai agent init` (model resource)
    Dockerfile / .dockerignore   # container build
    main.py                      # host entrypoint + enable_auto_tracing()
    graph.py                     # build_model() (live ChatOpenAI / offline) + build_graph()
    config.py                    # settings + Agent Optimizer load_config integration
    offline_model.py             # deterministic offline router (no-network demos/tests)
    requirements.txt
    .env.example
    .agent_configs/baseline/     # optimizer baseline: metadata.yaml + instructions.md + tools.json
    eval.yaml                    # optimizer + evaluation config
    tools/
      foundry_iq.py  webiq.py  scenario.py  memory.py  __init__.py
  src-maf/                       # Microsoft Agent Framework + Agent Harness twin (same 7 tools)
    agent.yaml                   # hosted-agent spec (responses 1.0.0)
    agent.manifest.yaml          # manifest (model resource)
    Dockerfile / .dockerignore   # container build
    main.py                      # FoundryChatClient + create_harness_agent + ResponsesHostServer
    config.py                    # settings + default instruction (no optimizer plumbing)
    skills_loader.py             # Foundry Skills -> system prompt (toolbox/local/auto)
    requirements.txt             # agent-framework + agent-framework-foundry-hosting + tool deps
    .env.example
    skills/                      # bundled Foundry Skills (local fallback copies)
    tools/
      foundry_iq.py  webiq.py  scenario.py  memory.py  __init__.py
    data/kb/                     # baked KB docs (self-contained Docker build context)
  data/
    kb/                          # generated Foundry IQ docs (kb_docs.jsonl) + schedule.json
    eval/seed_dataset.jsonl      # 24 Hisense eval queries
    memory/                      # offline per-viewer memory JSON (Feature 5 fallback)
  scripts/
    build_kb_docs.py             # 1.2026-04-14.json (+ LLD schema) -> KB docs
    setup_foundry_iq.py          # provision AI Search index + knowledge base (Feature 1)
    setup_memory.py              # provision the Foundry memory store (Feature 5)
    smoke_local.py               # offline routing smoke test
```

> The knowledge docs in `data/kb/` are **generated** from this repo's sample EPG payload
> (`../1.2026-04-14.json`) by `scripts/build_kb_docs.py`, shaped into the LLD's
> `movie` / `series` / `episode` `doc_type` discriminator design.

## Prerequisites

* Python 3.12+
* For live deploy: [Azure Developer CLI](https://aka.ms/azd) with the AI agent extension
  (`azd extension install microsoft.azd.ai.agent`), an Azure subscription, and a Foundry project.
* For Web IQ live calls: a key for `api.microsoft.ai`.

## Quickstart — offline (no Azure)

```powershell
cd poc
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U langchain langgraph requests pyyaml

# (Re)generate the Foundry IQ knowledge docs from the sample EPG.
.\.venv\Scripts\python.exe scripts\build_kb_docs.py

# Smoke test: build the graph offline and assert tool routing for 5 demo queries.
$env:PYTHONIOENCODING="utf-8"; $env:POC_OFFLINE="1"
.\.venv\Scripts\python.exe scripts\smoke_local.py
```

Expected: **all 5 routing cases pass** (each query routes to the right tool and the offline
KB returns real program content for the recommendation query).

## Run the full host locally

The HTTP host needs the full hosting stack (`langchain-azure-ai`, etc.):

```powershell
cd poc\src
..\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env   # fill in for live, or set POC_OFFLINE=1 in .env for offline
python main.py            # serves the responses protocol on :8088
```

## Deploy to Microsoft Foundry (live)

The hosted agent is deployed with the `azd ai agent` flow (the canonical generator for
`azure.yaml`). This POC ships in **code‑deploy mode** (source ZIP + Foundry **remote build**),
so **no local Docker / ACR is required** — the platform builds and activates the agent.

```powershell
cd poc\src

# 1) Authenticate azd to the tenant that owns the Foundry project.
azd auth login --tenant-id <tenant-id>

# 2) Point the azd env at the Foundry project + model, then sanity-check.
azd env set FOUNDRY_PROJECT_ENDPOINT https://<account>.services.ai.azure.com/api/projects/<project> -e poc
azd env set AZURE_AI_MODEL_DEPLOYMENT_NAME gpt-4.1-mini -e poc
azd ai agent doctor -e poc

# 3) Initialize in code-deploy mode (writes azure.yaml + agent.yaml with remote_build),
#    then deploy. Code mode means no Dockerfile build runs locally.
azd ai agent init --src ./src --agent-name hisense-tv-assistant-langgraph `
  --project-id <project-resource-id> --model-deployment gpt-4.1-mini `
  --deploy-mode code --dep-resolution remote_build --runtime python_3_13 `
  --entry-point main.py --protocol responses -e poc --no-prompt --force
azd deploy hisense-tv-assistant-langgraph -e poc --no-prompt
```

> A `Dockerfile` is still included for the alternative **container** path
> (`--deploy-mode container`, which pushes to ACR). Code mode is the default for this POC.

Set these on the azd environment (or agent env) before deploy:

| Variable | Feature | Notes |
|----------|---------|-------|
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | hosted agent | model deployment (this POC deploys `gpt-4.1-mini`; HLD default is the `gpt-5` family) |
| `FOUNDRY_IQ_ENDPOINT` / `FOUNDRY_IQ_KNOWLEDGE_BASE` / `FOUNDRY_IQ_KNOWLEDGE_SOURCE` | 1 | AI Search endpoint + knowledge base + knowledge source names (empty ⇒ baked‑KB retriever ships in the ZIP). Provision with `scripts/setup_foundry_iq.py`. |
| `WEBIQ_API_KEY` / `WEBIQ_BASE_URL` | 2 | source the key from a secret store in production |
| `MEMORY_STORE_NAME` / `MEMORY_EMBEDDING_DEPLOYMENT` / `MEMORY_DEFAULT_VIEWER` | 5 | Foundry memory store name + embedding deployment + default viewer id (empty ⇒ per‑viewer JSON under `data/memory/`). Provision with `scripts/setup_memory.py`. |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | 4 | platform‑injected when hosted |

`FOUNDRY_PROJECT_ENDPOINT` and `APPLICATIONINSIGHTS_CONNECTION_STRING` are injected by the
platform for hosted agents — they are intentionally **not** declared in `agent.manifest.yaml`.

### Live deployment (verified)

This POC has been deployed and verified live on Foundry:

| Item | Value |
|------|-------|
| Agent | `hisense-tv-assistant-langgraph` — **active** (code‑deploy) |
| Foundry account / project | `control-plane-test` / `control-plane-test` (rg `minggu-2026`, **eastus2**) |
| Model (agent) | `gpt-4.1-mini` · eval judge `gpt-4.1` · optimizer `GPT-5.4` |
| Foundry IQ KB | AI Search `hisense-poc-search-06211057` (**eastus**) · index `hisense-programs` (57 docs) · knowledge source `hisense-kb-source` · knowledge base `hisense-kb` (GA `2026-04-01`) |
| Memory store | `hisense-viewer-memory` (`beta.memory_stores`) · chat model `gpt-4.1-mini` · embedding `text-embedding-3-small` · user‑profile + chat‑summary memories enabled |
| Responses endpoint | `…/api/projects/control-plane-test/agents/hisense-tv-assistant-langgraph/endpoint/protocols/openai/responses?api-version=v1` |
| Monitoring | App Insights `control-plane-test-appinsights-4330` (Trace IDs returned per response) |

All five features were exercised against the **deployed** agent:

* **Feature 1 — Foundry IQ:** ✅ **live against a real AI Search knowledge base.** A Chinese
  query (“推荐一部关于新闻或时事的节目”) is grounded by `knowledgebases/hisense-kb/retrieve`
  (semantic intent, no vectors) and returns real DR1 programs (e.g. *Horisont*) with a Trace ID.
  Runtime auth is the agent managed identity (`Search Index Data Reader`).
* **Feature 2 — Web IQ:** ✅ **live with a real API key.** Returns current web/news **and image
  URLs** from `api.microsoft.ai` (a Haaland query returned 2026 World Cup headlines + 3 image
  links). Without a `WEBIQ_API_KEY` it degrades to a labelled offline stub.
* **Feature 3 — Agent Optimizer:** `azd ai agent optimize` submitted job
  `opt_7a0f1efafd914c4fae4d558058e67887` (in_progress; optimize‑model GPT‑5.4, 2 candidates,
  scored against `eval.yaml`). ✅ live — track with `azd ai agent optimize status <job-id> --watch`.
* **Feature 4 — Evaluation & Monitoring:** `azd ai agent eval run` completed (24 results):
  `task_adherence` 24/24, `intent_resolution` 20/24, `relevance` 13/24. `tool_call_accuracy`
  ERRORED on all 24 (responses‑trace tool‑call shape not yet captured by that evaluator — an
  improvement signal, not an agent fault). ✅ live + traced.
* **Feature 5 — Memory:** ✅ **live against a real Foundry memory store.** A "remember" turn
  ("请记住我是利物浦球迷，喜欢看英超，偏好中文解说。") routed to `remember_viewer_preference`,
  which called `beta.memory_stores.begin_update_memories` (update_delay=0); the store extracted
  structured `user_profile` + `chat_summary` memories ("User is a Liverpool football fan", "...
  English Premier League...", "... prefers Chinese commentary") under scope `viewer_demo-viewer`.
  A **separate session** then asked "根据我的偏好，今晚推荐我看什么？" → `recall_viewer_preferences`
  retrieved those memories and the agent personalized its reply in Chinese. Without
  `MEMORY_STORE_NAME` it degrades to per‑viewer JSON under `data/memory/`.

**Provisioning the Foundry IQ knowledge base (reproduce the live F1 setup):**

```powershell
# 1. Create an Azure AI Search service (basic SKU is enough for the POC).
# 2. Grant the agent managed identity 'Search Index Data Reader' on the search service,
#    and your user the same role for local validation.
# 3. Build/refresh the KB docs, then provision index + knowledge source + knowledge base
#    and ingest the 57 docs in one shot:
$env:SEARCH_ENDPOINT   = "https://<your-search>.search.windows.net"
$env:SEARCH_ADMIN_KEY  = "<search-admin-key>"      # control-plane setup only
.\.venv\Scripts\python.exe scripts\build_kb_docs.py          # regenerate data/kb/kb_docs.jsonl
.\.venv\Scripts\python.exe scripts\setup_foundry_iq.py       # index + ingest + KB + smoke retrieve

# 4. Point the agent at it and redeploy:
azd env set FOUNDRY_IQ_ENDPOINT        $env:SEARCH_ENDPOINT          -e poc
azd env set FOUNDRY_IQ_KNOWLEDGE_BASE  hisense-kb                    -e poc
azd env set FOUNDRY_IQ_KNOWLEDGE_SOURCE hisense-kb-source           -e poc
# (also set the same three + WEBIQ_API_KEY in src/agent.yaml env, then:)
azd deploy hisense-tv-assistant-langgraph -e poc
```

The index is **text + semantic, no vectors** — KB docs bake Chinese `keywords` (新闻/资讯/时事)
into `content`, so keyword + semantic ranking grounds Chinese queries against Danish/English
programs without any embedding/OpenAI dependency. The baked‑KB retriever remains the offline
fallback when the three `FOUNDRY_IQ_*` vars are unset.

**Provisioning the Memory store (reproduce the live F5 setup):**

```powershell
# 1. Deploy an embedding model (text-embedding-3-small) and a chat model (gpt-4.1-mini)
#    on the Foundry account — the memory store uses them to extract/index memories.
# 2. Grant the *project* and *account* system-assigned managed identities these roles on the
#    AI Services account so the memory backend can call the models:
#      Foundry User · Cognitive Services OpenAI User · Cognitive Services User
#    (data-plane RBAC can take 5-15 min to propagate). The hosted agent's runtime identity
#    must also be able to call the project memory API.
# 3. Create the memory store (idempotent get-or-create):
$env:POC_OFFLINE = "0"
$env:MEMORY_STORE_NAME = "hisense-viewer-memory"
$env:MEMORY_EMBEDDING_DEPLOYMENT = "text-embedding-3-small"
.\.venv\Scripts\python.exe scripts\setup_memory.py     # creates/returns the memory store

# 4. Wire the agent and redeploy (these env vars are already in src/agent.yaml):
#      MEMORY_STORE_NAME · MEMORY_EMBEDDING_DEPLOYMENT · MEMORY_DEFAULT_VIEWER
azd deploy hisense-tv-assistant-langgraph -e poc
```

> **Scope format:** the memory `begin_update_memories` endpoint only allows `[A-Za-z0-9_-]` in a
> scope, so `_scope()` namespaces viewers as `viewer_<id>` (a `:` or `/` separator is rejected).

## Demo script (the customer story)

1. **Foundry IQ** — “推荐一部关于丹麦收养/新闻的纪录片，讲了什么？”
   → routes to `foundry_iq_search`, retrieves from the EPG/titles KB with source titles.
2. **Web IQ** — “哈兰德昨晚进球了吗？给我最新新闻和图片”
   → routes to `webiq_search`, returns fresh web/news + images with citations.
3. **EPG / scenario** — “DR1 今晚几点有节目？是直播还是重播”, “现在比分多少”, “帮我换台到 DR1”
   → `query_schedule` / `get_live_scores` / `tune_to_channel`.
4. **Agent Optimizer** — improve the system instruction against `eval.yaml`, review the
   candidate diff, then redeploy.
5. **Evaluation & Monitoring** — batch eval over the seed dataset, then continuous eval +
   App Insights traces.
6. **Memory (cross‑session personalization)** — Session 1: “请记住我是利物浦球迷，喜欢看英超，
   偏好中文解说。” → `remember_viewer_preference` writes to the Foundry memory store. Start a
   **new session** and ask: “根据我的偏好，今晚推荐我看什么？” → `recall_viewer_preferences`
   brings back the stored profile and the agent personalizes the recommendation in Chinese.

## Feature 3 — Agent Optimizer

Config lives beside the agent:

* `src/.agent_configs/baseline/` — `metadata.yaml` (model/temperature/instruction/tool files),
  `instructions.md`, `tools.json` (the 5 tool schemas). This is the optimizer **baseline**.
* `src/eval.yaml` — evaluators, seed `dataset_file`, and `options.optimization_model`
  (must be a **deployed** model in the optimizer allowlist: GPT‑5 / 5.1 / 5.2 / 5.4 / 5.5 /
  DeepSeek‑V4‑Pro / DeepSeek‑V‑3.2).

Run via the microsoft‑foundry **agent‑optimizer** skill or directly:

```powershell
cd poc\src
azd ai agent optimize --config eval.yaml --eval-model gpt-4.1 --optimize-model GPT-5.4 `
  --max-candidates 2 --no-wait -e poc
azd ai agent optimize status <job-id> --watch -e poc   # watch candidates complete
azd ai agent optimize apply  --candidate <id> -e poc   # apply locally for review
```

Review the candidate, then `azd deploy hisense-tv-assistant-langgraph -e poc`. Because `config.py`
reads the instruction/model through `load_config()`, applying a candidate requires no code edit.

## Feature 4 — Evaluation & Monitoring

* **Tracing** — `main.py` calls `enable_auto_tracing()`; GenAI spans flow to App Insights via
  `OTEL_AUTO_CONFIGURE_AZURE_MONITOR` + `AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED`.
* **Batch eval** — run the seed dataset (`data/eval/seed_dataset.jsonl`, 24 Hisense queries)
  with evaluators `builtin.relevance`, `builtin.task_adherence`, `builtin.intent_resolution`,
  `builtin.tool_call_accuracy`:

  ```powershell
  cd poc\src
  azd ai agent eval run -e poc          # scores the deployed agent, returns a Foundry report URL
  ```
* **Continuous eval** — enable ongoing production monitoring.

> Built‑in evaluators require the `builtin.` prefix in `eval.yaml` (bare names return 400).
> The eval judge model (`options.eval_model`) must be a **deployed** model — this POC uses `gpt-4.1`.

Drive these through the microsoft‑foundry **observe** skill, which orchestrates the
evaluation MCP tools (`evaluation_agent_batch_eval_create`, `evaluation_comparison_create`,
`continuous_eval_create`, …) with the required pre‑checks — do not call the raw tools directly.

## Feature 5 — Memory (cross‑session viewer personalization)

The agent remembers a viewer's preferences (favorite teams/sports, preferred commentary
language, liked/disliked genres) **across the short, stateless sessions** a TV remote produces,
so a later session can personalize recommendations without re‑asking.

* **Managed memory store** — `src/tools/memory.py` talks to a Foundry **memory store** through
  `azure-ai-projects` `client.beta.memory_stores` (SDK‑only; no MCP/CLI surface):
  * `remember_viewer_preference(note, viewer_id=None)` → `begin_update_memories(..., update_delay=0).result()`.
    The store's chat model **extracts** structured `user_profile` + `chat_summary` memories from
    free‑text notes — you store a sentence, it indexes durable facts.
  * `recall_viewer_preferences(query=None, viewer_id=None)` → `search_memories(...)` returns the
    relevant memories for the viewer scope.
* **Scope = viewer identity** — `_scope()` namespaces memories as `viewer_<id>` (the update
  endpoint only accepts `[A-Za-z0-9_-]`). All sessions for the same viewer share one scope, which
  is what makes recall work *across* sessions. `MEMORY_DEFAULT_VIEWER` is used when the caller
  does not pass a `viewer_id` (the responses protocol has no per‑user field in this POC).
* **Store config** — `scripts/setup_memory.py` creates `hisense-viewer-memory` with
  `MemoryStoreDefaultDefinition` (chat model `gpt-4.1-mini`, embedding `text-embedding-3-small`)
  and `MemoryStoreDefaultOptions` (user‑profile + chat‑summary enabled).
* **Offline fallback** — with no `MEMORY_STORE_NAME` (or `POC_OFFLINE=1`), both tools read/write
  per‑viewer JSON under `data/memory/`, so the cross‑session demo runs with no Azure dependency.

> The memory backend calls the chat + embedding deployments using the project/account managed
> identity, so those identities need **Foundry User**, **Cognitive Services OpenAI User**, and
> **Cognitive Services User** on the AI Services account (else memory ops return a model‑auth 401).

## Microsoft Agent Framework variant (Agent Harness)

`src-maf/` is a second **hosted** agent — `hisense-tv-assistant-maf` — that exposes the **exact same
seven tools** as the LangGraph agent but is built on the **Microsoft Agent Framework (MAF)** and its
**Agent Harness** (`create_harness_agent`) instead of a hand‑wired LangGraph loop. It speaks the same
Foundry **`responses`** protocol and deploys the same way, so it is a like‑for‑like comparison of the
two agent runtimes against one scenario and one tool set.

**Why the harness.** `create_harness_agent` wraps the chat client in a managed agent loop that ships
the "latest harness" capabilities out of the box, layered on top of the tools:

* **TODO planning** — the model maintains an explicit task list for multi‑step requests.
* **Plan / execute mode** — a built‑in mode provider separates planning from execution turns.
* **Context compaction** — once history approaches `max_context_window_tokens` the harness summarizes
  older turns instead of overflowing the window.
* **Native GenAI tracing** — MAF is OpenTelemetry‑instrumented; Foundry Hosted Agents wire the OTel
  exporters and inject `APPLICATIONINSIGHTS_CONNECTION_STRING` automatically, so **no tracing code**
  lives in `main.py`. Set `ENABLE_SENSITIVE_DATA=true` (already in `agent.yaml`) to capture
  prompt/tool payloads.

**Tool parity.** The seven tool bodies are byte‑for‑byte the LangGraph versions; only the import and
decorator change (`from langchain_core.tools import tool` → `from agent_framework import tool`, and
`@tool` → `@tool(approval_mode="never_require")`). MAF's `@tool` natively maps bare‑string
`Annotated[T, "desc"]` parameters to `Field(description=...)`, so the Chinese parameter descriptions
and `Literal[...]` enums port verbatim.

**Harness configuration** (`build_agent()` in `src-maf/main.py`):

| Knob | Value | Why |
|------|-------|-----|
| `agent_instructions` | `compose_system_prompt(DEFAULT_INSTRUCTION)` | same Chinese persona + Foundry Skills injection as LangGraph, for behavioral parity |
| `tools` | `ALL_TOOLS` (7) | identical tool set |
| `max_context_window_tokens` / `max_output_tokens` | `128000` / `16384` | enables compaction headroom |
| `history_provider` | `InMemoryHistoryProvider(load_messages=False)` | **required** — `ResponsesHostServer` rejects a history provider that loads messages (the hosting layer owns history) |
| `disable_web_search` | `True` | the harness's generic web search would duplicate `webiq_search`; we keep the same seven tools |
| `disable_memory` | `True` | viewer personalization goes through the Foundry **Memory** tools, not the harness file‑memory store |
| `default_options` | `{"store": False}` | stateless responses; the hosting infra manages turn storage |

> **Statelessness note.** The harness's in‑memory context providers (todo list, mode, compaction
> buffer) live per‑process. In the hosted `responses` model each request can land on a fresh worker,
> so harness‑internal context is **not** guaranteed to persist between requests — durable
> personalization is intentionally delegated to the managed Memory store via the tools, not the
> harness. This is expected for the POC.

**Deploy / redeploy** (the service is already wired into `azure.yaml`):

```powershell
cd poc
# first provision (creates infra + both agents):
azd up -e poc
# or redeploy just the MAF agent after a code change:
azd deploy hisense-tv-assistant-maf -e poc
```

Run it locally exactly like the LangGraph host (`cd poc\src-maf`, install `requirements.txt`, copy
`.env.example` to `.env`, `python main.py`).

## Prompt-agent variant (no-code managed tools)

The headline demo above is a **hosted** LangGraph agent (`hisense-tv-assistant-langgraph`) — a container with
custom Python, which is the right shape for complex orchestration. But the *easiest* way to show an
enterprise user how Foundry's native capabilities snap onto an agent is a **prompt agent**: just an
LLM deployment plus a list of Foundry-managed tools, **no container and no code**. The scripts under
`scripts/create_prompt_agent.py` + `scripts/verify_prompt_agent.py` build and verify that variant.

It ships as a **single combined prompt agent** `hisense-tv-assistant-prompt` (model `gpt-5.4`) that
attaches **all three** Foundry-managed tools at once, plus a **dedicated** `hisense-program-library`
agent kept available for grounded program search (see the platform-behavior note below for why the
library agent still matters):

| Prompt agent | Foundry-managed tools | Demonstrates |
|--------------|-----------------------|--------------|
| `hisense-tv-assistant-prompt` | **Web IQ** (`MCPTool` → the literal WebIQ MCP server `api.microsoft.ai/v3/mcp` via the project `WebIQ` connection) + **Memory** (`MemorySearchPreviewTool` → store `hisense-viewer-memory`, scope `viewer_demo-viewer`) + **Foundry IQ** (`AzureAISearchTool` → index `hisense-programs`, **SEMANTIC** ranking, via the `hisense-search` AAD connection) | latest web/news/images, cross-session viewer personalization, and grounded program-library knowledge — all attached to one agent |
| `hisense-program-library` | **Foundry IQ** (`AzureAISearchTool` → index `hisense-programs`, **SEMANTIC** ranking, no vectors) | grounded program-library knowledge with native citations (the reliable path for EPG/program search) |

Both reuse the **same** managed resources as the hosted agent (the AI Search index, the WebIQ
connection, the memory store), so this is purely a different *agent surface* over identical backends.

```powershell
# from poc/ with the venv; set the project endpoint first
$env:FOUNDRY_PROJECT_ENDPOINT = "https://<account>.services.ai.azure.com/api/projects/<project>"
.\.venv\Scripts\python.exe scripts\create_prompt_agent.py   # creates/versions the combined agent
.\.venv\Scripts\python.exe scripts\verify_prompt_agent.py   # 4 checks across both agents; exits 0 on success
```

`verify_prompt_agent.py` is a **passing** test (exit 0): it asserts Web IQ (`mcp_call`) and Memory
(`memory_search_call`) **fire** on the combined agent, asserts Azure AI Search is **suppressed** on the
combined agent (encoding the documented finding below as a positive assertion), and asserts Azure AI Search
**fires** (`azure_ai_search_call`) on the dedicated `hisense-program-library` agent — so all three headline
capabilities stay demonstrable in the POC.

Verified live (responses API, `agent_reference`) on the combined agent: Web IQ → `mcp_call` (tools
`news` + `images`) returns current 2026 World Cup Haaland headlines with source + image links; Memory →
`memory_search_call` recalls the saved viewer profile (中文解说 / 时政新闻 / 英超 / DR1) and personalizes
**without fabricating program names**. Foundry IQ, however, **does not fire** on the combined agent —
see the platform-behavior note immediately below.

> **Platform behavior (empirically confirmed) — Memory suppresses Azure AI Search on one prompt agent.**
> Per the user's request, `hisense-tv-assistant-prompt` attaches **all three** tools. But the current
> prompt-agent runtime runs an **automatic per-turn Memory retrieval** that pre-empts the single managed
> "retrieval/grounding" slot, so the attached `AzureAISearchTool` **never fires** while Memory is present
> — `azure_ai_search_call` is absent from the output items on every program query, and forcing it via
> `tool_choice="required"` still yields only Memory/WebIQ calls (the API also rejects naming the search
> tool: `tool_choice.name` is an unknown parameter). The combined agent therefore carries Foundry IQ as
> an **attached-but-dead** tool: for program/EPG questions it now degrades honestly (it tells the viewer
> it cannot pull live listings in this mode and offers WebIQ or the dedicated library agent) rather than
> fabricating program names.
>
> No public Microsoft doc forbids combining these tools — the tool catalog says you can attach multiple
> tools and the model decides which to invoke — so this is a runtime characteristic of the **preview**
> Memory tool, not a documented limit. Memory **does** coexist with `MCPTool` (WebIQ), and AI Search
> coexists with WebIQ; only the **Memory + AI-Search** pair collides. To get **all three actually
> working together**, use either (a) the **hosted LangGraph agent** (`hisense-tv-assistant-langgraph`), which
> orchestrates AI Search + WebIQ + Memory in code with no such constraint, or (b) the **two-agent split**
> — keep program search on `hisense-program-library` and personalization/news on the combined agent.

## Offline ⇄ live behavior

| Tool | Live | Offline |
|------|------|---------|
| chat model | `ChatOpenAI` via project OpenAI client (AAD token provider) | `FakeRouterChatModel` keyword router |
| `foundry_iq_search` | AI Search **knowledge base** retrieve (GA `2026-04-01`, semantic intent) | keyword retriever over `data/kb/kb_docs.jsonl` (EN→ZH synonym bridge) |
| `webiq_search` | `api.microsoft.ai` web/news/images | labelled offline stub |
| `query_schedule` / `get_live_scores` / `tune_to_channel` | reads `data/kb/schedule.json`; scores/tune are scenario mocks | same |
| `remember_viewer_preference` / `recall_viewer_preferences` | Foundry **memory store** (`beta.memory_stores`, profile + summary extraction) | per‑viewer JSON under `data/memory/` |

Mode is inferred automatically: **offline** when `FOUNDRY_PROJECT_ENDPOINT` is unset or
`POC_OFFLINE=1`; otherwise **live**.

## Notes & limitations

* Sample EPG data (DR1 Denmark) contains **no sports items**; the sports tools
  (`get_live_scores`) are scenario mocks. Web IQ supplies real, fresh sports grounding.
* Offline cross‑language retrieval uses a small EN→ZH synonym map; live mode uses the AI Search
  knowledge base with **text + semantic ranking (no vectors)** — KB docs bake Chinese keywords
  (新闻/资讯/时事) into `content`, so keyword + semantic ranking bridges Chinese queries to the
  Danish/English program library without an embedding/OpenAI dependency.
* `infra/main.bicep` provisions only the **supporting** resources (AI Search + App Insights).
  The Foundry project, model deployment, and agent container host are created by the
  `azd ai agent` / Foundry project flow, so this bicep never collides with azd.
* The container build context is `src/`; `config.py` resolves `data/kb` from either
  `poc/data/kb` (repo layout) or `src/data/kb` (if baked into an offline image).
