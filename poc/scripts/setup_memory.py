# Copyright (c) Microsoft. All rights reserved.
"""Provision the live **Foundry managed Memory** store for the Hisense POC.

Creates (idempotently) the memory store that backs ``tools/memory.py`` in *live*
mode — the cross-session viewer-preference personalization feature (Feature 5).

The store uses the project's chat model to *extract* structured user-profile
memories from each note and the embedding model to make them semantically
recallable. Both must already be deployed in the project:

* chat model     — ``AZURE_AI_MODEL_DEPLOYMENT_NAME`` (e.g. ``gpt-4.1-mini``)
* embedding model — ``MEMORY_EMBEDDING_DEPLOYMENT`` (e.g. ``text-embedding-3-small``)

Prerequisites the agent also needs at runtime: the project's system-assigned
managed identity granted the Foundry memory data role on the AI Services
resource (so the deployed container can call the memory APIs).

Run::

    FOUNDRY_PROJECT_ENDPOINT=https://<project>.services.ai.azure.com/api/projects/<project> \
    AZURE_AI_MODEL_DEPLOYMENT_NAME=gpt-4.1-mini \
    MEMORY_EMBEDDING_DEPLOYMENT=text-embedding-3-small \
    MEMORY_STORE_NAME=hisense-viewer-memory \
    python scripts/setup_memory.py
"""

from __future__ import annotations

import os
import sys

ENDPOINT = (os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or "").rstrip("/")
STORE_NAME = os.environ.get("MEMORY_STORE_NAME", "hisense-viewer-memory")
CHAT_MODEL = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4.1-mini")
EMBEDDING_MODEL = os.environ.get("MEMORY_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")


def main() -> int:
    if not ENDPOINT:
        sys.exit("ERROR: set FOUNDRY_PROJECT_ENDPOINT.")

    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import (
        MemoryStoreDefaultDefinition,
        MemoryStoreDefaultOptions,
    )
    from azure.identity import DefaultAzureCredential

    client = AIProjectClient(endpoint=ENDPOINT, credential=DefaultAzureCredential())
    stores = client.beta.memory_stores

    # Idempotent: reuse the store if it already exists.
    try:
        existing = stores.get(STORE_NAME)
        print(f"Memory store '{STORE_NAME}' already exists (id={existing.id}); reusing.")
        return 0
    except Exception:  # noqa: BLE001 — not-found (or transient); fall through to create
        pass

    definition = MemoryStoreDefaultDefinition(
        chat_model=CHAT_MODEL,
        embedding_model=EMBEDDING_MODEL,
        options=MemoryStoreDefaultOptions(
            # Personalization profile is the heart of the scenario; keep chat
            # summaries on for richer recall, procedural memory off (not needed
            # for a remote-key TV assistant).
            user_profile_enabled=True,
            chat_summary_enabled=True,
            procedural_memory_enabled=False,
            default_ttl_seconds=0,  # preferences don't expire
        ),
    )

    details = stores.create(
        name=STORE_NAME,
        definition=definition,
        description="Hisense TV assistant — cross-session viewer-preference memory (POC Feature 5).",
        metadata={"scenario": "hisense-tv", "feature": "viewer-memory"},
    )
    print(
        f"Created memory store '{details.name}' (id={details.id})\n"
        f"  chat_model={CHAT_MODEL}  embedding_model={EMBEDDING_MODEL}\n"
        f"Set MEMORY_STORE_NAME={details.name} on the agent (already in agent.yaml)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
