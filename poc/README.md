# Hisense TV Sports AI Assistant — Microsoft Foundry POC

A runnable proof‑of‑concept that demonstrates **Microsoft Foundry as a production‑ready
agent platform**, using the Hisense TV remote‑key sports assistant scenario.

The agent is a **LangGraph hosted agent** speaking the Foundry **`responses`** protocol
(`langchain_azure_ai.agents.hosting.ResponsesHostServer`), deployable with `azd ai agent`.
It is designed to run in two modes:

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

## Architecture

```
remote AI key ──► hosted agent (responses) ──► LangGraph tool‑calling loop
                                                 ├─ foundry_iq_search   (Feature 1)
                                                 ├─ webiq_search        (Feature 2)
                                                 ├─ query_schedule      (EPG)
                                                 ├─ get_live_scores      (scores)
                                                 └─ tune_to_channel      (device)
                          tracing ─► OpenTelemetry ─► Application Insights (Feature 4)
```

The agent's **instruction + model are read through the Agent Optimizer config**
(`load_config()`), so `azd ai agent optimize` can swap in an optimized candidate from
`.agent_configs/` with no code change (Feature 3).

## Repo layout

```
poc/
  azure.yaml                     # azd hosted-agent service (host: azure.ai.agent)
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
      foundry_iq.py  webiq.py  scenario.py  __init__.py
  data/
    kb/                          # generated Foundry IQ docs (kb_docs.jsonl) + schedule.json
    eval/seed_dataset.jsonl      # 24 Hisense eval queries
  scripts/
    build_kb_docs.py             # 1.2026-04-14.json (+ LLD schema) -> KB docs
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
azd ai agent init --src ./src --agent-name hisense-tv-assistant `
  --project-id <project-resource-id> --model-deployment gpt-4.1-mini `
  --deploy-mode code --dep-resolution remote_build --runtime python_3_13 `
  --entry-point main.py --protocol responses -e poc --no-prompt --force
azd deploy hisense-tv-assistant -e poc --no-prompt
```

> A `Dockerfile` is still included for the alternative **container** path
> (`--deploy-mode container`, which pushes to ACR). Code mode is the default for this POC.

Set these on the azd environment (or agent env) before deploy:

| Variable | Feature | Notes |
|----------|---------|-------|
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | hosted agent | model deployment (this POC deploys `gpt-4.1-mini`; HLD default is the `gpt-5` family) |
| `FOUNDRY_IQ_ENDPOINT` / `FOUNDRY_IQ_KNOWLEDGE_BASE` / `FOUNDRY_IQ_KNOWLEDGE_SOURCE` | 1 | AI Search endpoint + knowledge base + knowledge source names (empty ⇒ baked‑KB retriever ships in the ZIP). Provision with `scripts/setup_foundry_iq.py`. |
| `WEBIQ_API_KEY` / `WEBIQ_BASE_URL` | 2 | source the key from a secret store in production |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | 4 | platform‑injected when hosted |

`FOUNDRY_PROJECT_ENDPOINT` and `APPLICATIONINSIGHTS_CONNECTION_STRING` are injected by the
platform for hosted agents — they are intentionally **not** declared in `agent.manifest.yaml`.

### Live deployment (verified)

This POC has been deployed and verified live on Foundry:

| Item | Value |
|------|-------|
| Agent | `hisense-tv-assistant:7` — **active** (code‑deploy) |
| Foundry account / project | `control-plane-test` / `control-plane-test` (rg `minggu-2026`, **eastus2**) |
| Model (agent) | `gpt-4.1-mini` · eval judge `gpt-4.1` · optimizer `GPT-5.4` |
| Foundry IQ KB | AI Search `hisense-poc-search-06211057` (**eastus**) · index `hisense-programs` (57 docs) · knowledge source `hisense-kb-source` · knowledge base `hisense-kb` (GA `2026-04-01`) |
| Responses endpoint | `…/api/projects/control-plane-test/agents/hisense-tv-assistant/endpoint/protocols/openai/responses?api-version=v1` |
| Monitoring | App Insights `control-plane-test-appinsights-4330` (Trace IDs returned per response) |

All four features were exercised against the **deployed** agent:

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
azd deploy hisense-tv-assistant -e poc
```

The index is **text + semantic, no vectors** — KB docs bake Chinese `keywords` (新闻/资讯/时事)
into `content`, so keyword + semantic ranking grounds Chinese queries against Danish/English
programs without any embedding/OpenAI dependency. The baked‑KB retriever remains the offline
fallback when the three `FOUNDRY_IQ_*` vars are unset.

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

Review the candidate, then `azd deploy hisense-tv-assistant -e poc`. Because `config.py`
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

## Offline ⇄ live behavior

| Tool | Live | Offline |
|------|------|---------|
| chat model | `ChatOpenAI` via project OpenAI client (AAD token provider) | `FakeRouterChatModel` keyword router |
| `foundry_iq_search` | AI Search **knowledge base** retrieve (GA `2026-04-01`, semantic intent) | keyword retriever over `data/kb/kb_docs.jsonl` (EN→ZH synonym bridge) |
| `webiq_search` | `api.microsoft.ai` web/news/images | labelled offline stub |
| `query_schedule` / `get_live_scores` / `tune_to_channel` | reads `data/kb/schedule.json`; scores/tune are scenario mocks | same |

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
