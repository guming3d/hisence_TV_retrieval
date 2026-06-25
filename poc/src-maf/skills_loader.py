# Copyright (c) Microsoft. All rights reserved.
"""Load **Foundry Skills (preview)** into the hosted agent's system prompt.

Two delivery paths are supported, selected at runtime via ``SKILLS_SOURCE``:

* ``toolbox`` — the documented *"Attach skills to a toolbox (preview)"* path.
  At graph-build time the loader calls the project's **Toolbox MCP endpoint**
  (``{endpoint}/toolboxes/{name}/mcp``), lists the skill **resources**, reads
  each ``SKILL.md`` body, and injects it into the model's system prompt. The
  toolbox is the live source of truth, so refreshing a skill's default version
  in the central Foundry store flows through on the next agent restart — no
  image rebuild required.
* ``local`` — bundled local copies. Each skill's ``SKILL.md`` ships under
  ``src/skills/`` (``COPY . user_agent/`` bundles it into the container image),
  and the loader reads them directly. Fully offline-safe; no network call.

``SKILLS_SOURCE=auto`` (the default) uses the toolbox when the agent is live
(project endpoint present, not offline, a toolbox name configured) and the
bundled local copies otherwise. **Any toolbox failure falls back to the local
copies** so the hosted agent never breaks — the bundled ``src/skills/`` files
are the safety net. The resolved source is logged at INFO (visible via
``azd ai agent monitor``) for demo/observability.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_LOG = logging.getLogger("skills_loader")

SKILLS_DIR = Path(__file__).resolve().parent / "skills"

_AUTH_SCOPE = "https://ai.azure.com/.default"
_MCP_PROTOCOL_VERSION = "2025-06-18"

_HEADER_LOCAL = (
    "# 已加载的 Foundry 技能 (Foundry Skills, preview — 本地随包副本)\n"
    "以下行为规范由 Foundry Skills 提供，**优先级高于**一般作答习惯；"
    "在任何工具调用与回答中都请严格遵守。"
)
_HEADER_TOOLBOX = (
    "# 已加载的 Foundry 技能 (Foundry Skills, preview — 运行时来自工具箱 MCP 资源)\n"
    "以下行为规范由 Foundry 技能工具箱（Toolbox）在运行时通过 MCP 资源下发，"
    "**优先级高于**一般作答习惯；在任何工具调用与回答中都请严格遵守。"
)


def _split_front_matter(text: str, fallback_name: str) -> tuple[str, str]:
    """Return ``(name, body)`` from SKILL.md text, stripping YAML front matter."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            fm, body = parts[1], parts[2]
            name = ""
            for line in fm.splitlines():
                line = line.strip()
                if line.startswith("name:"):
                    name = line[len("name:"):].strip()
                    break
            return (name or fallback_name), body.strip()
    return fallback_name, text.strip()


# --------------------------------------------------------------------------- #
# Local (bundled) source
# --------------------------------------------------------------------------- #
def _parse_skill_md(path: Path) -> tuple[str, str]:
    return _split_front_matter(path.read_text(encoding="utf-8"), path.parent.name)


def load_skills_local() -> list[tuple[str, str]]:
    """Discover bundled skills as ``(name, body)`` pairs, sorted by name."""
    if not SKILLS_DIR.is_dir():
        return []
    skills = [_parse_skill_md(p) for p in sorted(SKILLS_DIR.glob("*/SKILL.md"))]
    return [(n, b) for n, b in skills if b]


# --------------------------------------------------------------------------- #
# Toolbox MCP source (preview) — "Attach skills to a toolbox"
# --------------------------------------------------------------------------- #
def _mcp_endpoint(project_endpoint: str, toolbox: str) -> str:
    return f"{project_endpoint.rstrip('/')}/toolboxes/{toolbox}/mcp?api-version=v1"


def _acquire_token() -> str:
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential().get_token(_AUTH_SCOPE).token


def _parse_jsonrpc(resp, req_id):
    """Parse a JSON-RPC result from an MCP response (JSON or SSE)."""
    import json

    ctype = (resp.headers.get("Content-Type") or "").lower()
    msg = None
    if "text/event-stream" in ctype:
        fallback = None
        for line in resp.text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if isinstance(obj, dict):
                if obj.get("id") == req_id:
                    msg = obj
                    break
                fallback = obj
        if msg is None:
            msg = fallback
    else:
        msg = resp.json()

    if not isinstance(msg, dict):
        raise RuntimeError("no JSON-RPC message in MCP response")
    if msg.get("error"):
        raise RuntimeError(f"MCP error: {msg['error']}")
    return msg.get("result")


class _ToolboxMcpClient:
    """Minimal MCP streamable-HTTP client over ``requests`` for skill resources."""

    def __init__(self, url: str, token: str, timeout: int = 30) -> None:
        import requests

        self._url = url
        self._timeout = timeout
        self._session = requests.Session()
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Foundry-Features": "Toolboxes=V1Preview",
        }
        self._session_id: str | None = None
        self._id = 0

    def _send(self, method: str, params=None, notification: bool = False):
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self._id
        if params is not None:
            payload["params"] = params
        headers = dict(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        resp = self._session.post(
            self._url, headers=headers, json=payload, timeout=self._timeout
        )
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        if notification:
            return None
        resp.raise_for_status()
        return _parse_jsonrpc(resp, payload["id"])

    def initialize(self) -> None:
        self._send(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {"resources": {}},
                "clientInfo": {"name": "hisense-tv-assistant", "version": "1.0"},
            },
        )
        try:
            self._send("notifications/initialized", notification=True)
        except Exception:  # noqa: BLE001 — notification is best-effort
            pass

    def list_resources(self) -> list[dict]:
        result = self._send("resources/list") or {}
        return result.get("resources", []) or []

    def read_resource(self, uri: str) -> list[dict]:
        result = self._send("resources/read", {"uri": uri}) or {}
        return result.get("contents", []) or []


def _name_from_uri(uri: str) -> str:
    rest = uri.split("://", 1)[-1]
    head = rest.split("/", 1)[0]
    return head or uri


def load_skills_from_toolbox(project_endpoint: str, toolbox: str) -> list[tuple[str, str]]:
    """Fetch skills from the toolbox MCP endpoint as ``(name, body)`` pairs.

    Raises on any transport/auth error so the caller can fall back to local.
    """
    client = _ToolboxMcpClient(_mcp_endpoint(project_endpoint, toolbox), _acquire_token())
    client.initialize()
    skills: list[tuple[str, str]] = []
    for res in client.list_resources():
        uri = res.get("uri", "") or ""
        if not uri.lower().endswith("skill.md"):
            continue
        text = ""
        for content in client.read_resource(uri):
            if isinstance(content, dict) and content.get("text"):
                text = content["text"]
                break
        if not text:
            continue
        name, body = _split_front_matter(text, _name_from_uri(uri))
        if body:
            skills.append((name, body))
    skills.sort(key=lambda nb: nb[0])
    return skills


# --------------------------------------------------------------------------- #
# Source resolution
# --------------------------------------------------------------------------- #
def resolve_skills() -> tuple[str, list[tuple[str, str]]]:
    """Resolve the active skill source and return ``(source_label, skills)``.

    ``source_label`` is ``"toolbox:<name>"`` or ``"local"`` and reflects where
    the returned skill bodies actually came from after any fallback.
    """
    from config import get_settings

    settings = get_settings()
    source = (settings.skills_source or "auto").strip().lower()
    toolbox = settings.skills_toolbox_name
    endpoint = settings.project_endpoint

    want_toolbox = (
        source in ("toolbox", "auto")
        and not settings.offline
        and bool(endpoint)
        and bool(toolbox)
    )
    if want_toolbox:
        _LOG.info("[skills] resolving from toolbox '%s' via MCP (%s)", toolbox, source)
        try:
            skills = load_skills_from_toolbox(endpoint, toolbox)  # type: ignore[arg-type]
            if skills:
                return (f"toolbox:{toolbox}", skills)
            _LOG.warning(
                "[skills] toolbox '%s' returned no skill resources; "
                "falling back to bundled local copies.",
                toolbox,
            )
        except Exception as exc:  # noqa: BLE001 — never break the agent on fetch error
            _LOG.warning(
                "[skills] toolbox fetch failed (%s: %s); "
                "falling back to bundled local copies.",
                type(exc).__name__,
                exc,
            )
    elif source == "toolbox":
        _LOG.warning(
            "[skills] SKILLS_SOURCE=toolbox but agent is offline or no endpoint/"
            "toolbox configured; using bundled local copies."
        )

    return ("local", load_skills_local())


# Backwards-compatible alias (previously returned local pairs only).
def load_skills() -> list[tuple[str, str]]:
    return resolve_skills()[1]


def compose_system_prompt(base_instruction: str) -> str:
    """Append every resolved skill body to ``base_instruction``.

    Returns the base instruction unchanged when no skills resolve, so the agent
    degrades gracefully if neither the toolbox nor ``src/skills/`` yields any.
    """
    source, skills = resolve_skills()
    print(f"[skills] resolved source='{source}' count={len(skills)}", file=sys.stderr, flush=True)
    if not skills:
        _LOG.info("[skills] no skills resolved; using base instruction unchanged.")
        return base_instruction

    header = _HEADER_TOOLBOX if source.startswith("toolbox") else _HEADER_LOCAL
    blocks = [header]
    for name, body in skills:
        blocks.append(f"== Foundry Skill: {name} ==\n{body}")
    _LOG.info("[skills] injected %d skill(s) from source '%s'.", len(skills), source)
    return base_instruction.rstrip() + "\n\n" + "\n\n".join(blocks) + "\n"
