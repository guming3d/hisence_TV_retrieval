# Copyright (c) Microsoft. All rights reserved.
"""Provision the live **Foundry IQ** knowledge base for the Hisense POC.

Builds, on an existing Azure AI Search service, the full agentic-retrieval stack
that backs ``tools/foundry_iq.py`` in *live* mode:

1. **Index** ``hisense-programs`` — text fields + a semantic configuration over the
   ``movie`` / ``series`` / ``episode`` knowledge docs (no vectors needed: the docs
   carry Chinese ``keywords`` + ``content`` so keyword + semantic ranking grounds
   Chinese queries against Danish/English programs).
2. **Ingest** the 57 docs from ``data/kb/kb_docs.jsonl``.
3. **Knowledge source** ``hisense-kb-source`` (kind ``searchIndex``) over the index.
4. **Knowledge base** ``hisense-kb`` referencing the knowledge source (extractive,
   no LLM) — this is the Foundry IQ object the agent's ``retrieve`` call targets.
5. **Smoke retrieve** to confirm grounding before the agent goes live.

Uses the GA REST API ``2026-04-01`` with the search **admin key** (control-plane
setup); the deployed agent uses its managed identity + ``Search Index Data Reader``
for the runtime ``retrieve`` call.

Run::

    SEARCH_ENDPOINT=https://hisense-poc-search-06211057.search.windows.net \
    SEARCH_ADMIN_KEY=<key> \
    python scripts/setup_foundry_iq.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

API_VERSION = os.environ.get("FOUNDRY_IQ_API_VERSION", "2026-04-01")
ENDPOINT = os.environ.get("SEARCH_ENDPOINT", "").rstrip("/")
ADMIN_KEY = os.environ.get("SEARCH_ADMIN_KEY", "")
INDEX_NAME = os.environ.get("FOUNDRY_IQ_INDEX", "hisense-programs")
KS_NAME = os.environ.get("FOUNDRY_IQ_KNOWLEDGE_SOURCE", "hisense-kb-source")
KB_NAME = os.environ.get("FOUNDRY_IQ_KNOWLEDGE_BASE", "hisense-kb")
SEMANTIC_CONFIG = "hisense-semantic"

KB_DOCS = Path(__file__).resolve().parent.parent / "data" / "kb" / "kb_docs.jsonl"


def _headers() -> dict[str, str]:
    if not ENDPOINT or not ADMIN_KEY:
        sys.exit("ERROR: set SEARCH_ENDPOINT and SEARCH_ADMIN_KEY env vars.")
    return {"Content-Type": "application/json", "api-key": ADMIN_KEY}


def _put(path: str, body: dict) -> requests.Response:
    url = f"{ENDPOINT}/{path}?api-version={API_VERSION}"
    resp = requests.put(url, headers=_headers(), data=json.dumps(body), timeout=60)
    return resp


def _check(resp: requests.Response, what: str) -> None:
    if resp.status_code >= 300:
        print(f"  ! {what}: HTTP {resp.status_code}\n{resp.text[:1500]}")
        resp.raise_for_status()
    print(f"  ok {what}: HTTP {resp.status_code}")


def create_index() -> None:
    print(f"[1/5] index '{INDEX_NAME}'")
    fields = [
        {"name": "title_id", "type": "Edm.String", "key": True, "filterable": True},
        {"name": "doc_type", "type": "Edm.String", "filterable": True, "facetable": True},
        {"name": "series_id", "type": "Edm.String", "filterable": True},
        {"name": "title", "type": "Edm.String", "searchable": True},
        {"name": "original_title", "type": "Edm.String", "searchable": True},
        {"name": "season", "type": "Edm.Int32", "filterable": True},
        {"name": "episode_number", "type": "Edm.Int32", "filterable": True},
        {"name": "genres", "type": "Collection(Edm.String)", "searchable": True, "filterable": True, "facetable": True},
        {"name": "keywords", "type": "Collection(Edm.String)", "searchable": True, "filterable": True, "facetable": True},
        {"name": "channel", "type": "Edm.String", "searchable": True, "filterable": True, "facetable": True},
        {"name": "release_year", "type": "Edm.Int32", "filterable": True, "facetable": True, "sortable": True},
        {"name": "imdb_rating", "type": "Edm.Double", "filterable": True, "sortable": True},
        {"name": "content", "type": "Edm.String", "searchable": True},
    ]
    body = {
        "name": INDEX_NAME,
        "fields": fields,
        "semantic": {
            "configurations": [
                {
                    "name": SEMANTIC_CONFIG,
                    "prioritizedFields": {
                        "titleField": {"fieldName": "title"},
                        "prioritizedContentFields": [
                            {"fieldName": "content"},
                            {"fieldName": "original_title"},
                        ],
                        "prioritizedKeywordsFields": [
                            {"fieldName": "keywords"},
                            {"fieldName": "genres"},
                            {"fieldName": "channel"},
                        ],
                    },
                }
            ]
        },
    }
    _check(_put(f"indexes/{INDEX_NAME}", body), "create/update index")


_INDEX_FIELDS = {
    "title_id", "doc_type", "series_id", "title", "original_title", "season",
    "episode_number", "genres", "keywords", "channel", "release_year",
    "imdb_rating", "content",
}


def _load_docs() -> list[dict]:
    docs: list[dict] = []
    for line in KB_DOCS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        doc = json.loads(line)
        # Keep only fields defined on the index; drop null values (e.g. episode_number).
        clean = {k: v for k, v in doc.items() if k in _INDEX_FIELDS and v is not None}
        clean["@search.action"] = "mergeOrUpload"
        docs.append(clean)
    return docs


def upload_docs() -> None:
    docs = _load_docs()
    print(f"[2/5] ingest {len(docs)} docs")
    url = f"{ENDPOINT}/indexes/{INDEX_NAME}/docs/index?api-version={API_VERSION}"
    # Batch (well under the 1000-doc / 16MB limits for 57 small docs).
    resp = requests.post(url, headers=_headers(), data=json.dumps({"value": docs}), timeout=120)
    _check(resp, "upload batch")
    failed = [r for r in resp.json().get("value", []) if not r.get("status")]
    if failed:
        print(f"  ! {len(failed)} docs failed: {failed[:3]}")
        sys.exit(1)
    time.sleep(3)  # let indexing settle before the smoke retrieve


def create_knowledge_source() -> None:
    print(f"[3/5] knowledge source '{KS_NAME}'")
    body = {
        "name": KS_NAME,
        "kind": "searchIndex",
        "description": "Hisense program library (EPG/titles) — movie/series/episode docs.",
        "searchIndexParameters": {
            "searchIndexName": INDEX_NAME,
            "semanticConfigurationName": SEMANTIC_CONFIG,
        },
    }
    _check(_put(f"knowledgesources/{KS_NAME}", body), "create/update knowledge source")


def create_knowledge_base() -> None:
    print(f"[4/5] knowledge base '{KB_NAME}'")
    body = {
        "name": KB_NAME,
        "description": "Foundry IQ knowledge base for the Hisense TV assistant POC.",
        "knowledgeSources": [{"name": KS_NAME}],
        "models": [],
    }
    _check(_put(f"knowledgebases/{KB_NAME}", body), "create/update knowledge base")


def smoke_retrieve() -> None:
    print("[5/5] smoke retrieve")
    url = f"{ENDPOINT}/knowledgebases/{KB_NAME}/retrieve?api-version={API_VERSION}"
    body = {
        "intents": [{"type": "semantic", "search": "推荐一部关于新闻时事的节目"}],
        "knowledgeSourceParams": [
            {
                "knowledgeSourceName": KS_NAME,
                "kind": "searchIndex",
                "includeReferences": True,
                "includeReferenceSourceData": True,
            }
        ],
    }
    resp = requests.post(url, headers=_headers(), data=json.dumps(body), timeout=60)
    if resp.status_code >= 300:
        print(f"  ! retrieve HTTP {resp.status_code}\n{resp.text[:2000]}")
        resp.raise_for_status()
    data = resp.json()
    print("  retrieve OK. Top-level keys:", list(data.keys()))
    print(json.dumps(data, ensure_ascii=False, indent=1)[:2500])


def main() -> None:
    create_index()
    upload_docs()
    create_knowledge_source()
    create_knowledge_base()
    smoke_retrieve()
    print("\nDONE. Set in agent.yaml / azd env:")
    print(f"  FOUNDRY_IQ_ENDPOINT={ENDPOINT}")
    print(f"  FOUNDRY_IQ_KNOWLEDGE_BASE={KB_NAME}")


if __name__ == "__main__":
    main()
