# Copyright (c) Microsoft. All rights reserved.
"""Hosted-agent entrypoint for the Hisense TV Sports AI Assistant.

Serves the LangGraph agent over the Foundry ``responses`` protocol via
``langchain_azure_ai.agents.hosting.ResponsesHostServer``, with OpenTelemetry
auto-tracing to Application Insights enabled when the platform injects
``APPLICATIONINSIGHTS_CONNECTION_STRING`` (Feature 4: Evaluation & Monitoring).

Run locally::

    python poc/src/main.py            # needs full hosting deps installed

For a no-network smoke test of the graph itself, use ``scripts/smoke_local.py``.
"""

from __future__ import annotations

import os


def main() -> None:
    # Auto-tracing → App Insights (no-op/best-effort if not configured).
    try:
        from langchain_azure_ai.callbacks.tracers import enable_auto_tracing

        enable_auto_tracing()
    except Exception as exc:  # noqa: BLE001 — tracing is optional for local runs
        print(f"[tracing] auto-tracing not enabled: {exc}")

    from graph import build_graph

    graph = build_graph()

    from langchain_azure_ai.agents.hosting import ResponsesHostServer

    port = int(os.environ.get("PORT", "8088"))
    print(f"[host] starting ResponsesHostServer on :{port}")
    ResponsesHostServer(graph).run(port=port)


if __name__ == "__main__":
    main()
