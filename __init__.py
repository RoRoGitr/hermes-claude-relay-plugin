"""Hermes plugin: Claude Code relay slash commands and tool."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime
import json
import logging
from pathlib import Path
import shlex
from typing import Any, Optional

try:
    from .relay import (
        DEFAULT_TIMEOUT_SECONDS,
        RELAY_TO_CLAUDE_SCHEMA,
        check_requirements,
        relay_to_claude,
    )
except ImportError:  # Allows pytest to import this plugin root as a top-level __init__.py.
    from relay import (  # type: ignore
        DEFAULT_TIMEOUT_SECONDS,
        RELAY_TO_CLAUDE_SCHEMA,
        check_requirements,
        relay_to_claude,
    )

logger = logging.getLogger(__name__)

_CURRENT_EVENT = None
_CURRENT_GATEWAY = None
_PLUGIN_DIR = Path(__file__).resolve().parent


def _state_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()) / "claude_relay_sessions.json"
    except Exception:
        return _PLUGIN_DIR / "claude_relay_sessions.json"


def _load_state() -> dict:
    path = _state_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        logger.debug("Failed to load Claude relay state: %s", exc)
    return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _event_command(text: str | None) -> str | None:
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return None
    first = raw.split(None, 1)[0].lstrip("/")
    return first.split("@", 1)[0].lower().replace("_", "-")


def _event_args(text: str | None) -> str:
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return ""
    parts = raw.split(None, 1)
    return parts[1] if len(parts) > 1 else ""


def _session_key(event=None, gateway=None) -> str:
    event = event or _CURRENT_EVENT
    gateway = gateway or _CURRENT_GATEWAY
    if event is None:
        return "global"
    if gateway is not None and hasattr(gateway, "_session_key_for_source"):
        try:
            return gateway._session_key_for_source(event.source)
        except Exception:
            pass
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None) or getattr(source, "platform", "unknown")
    chat = getattr(source, "chat_id", "") or ""
    thread = getattr(source, "thread_id", "") or getattr(source, "message_thread_id", "") or ""
    user = getattr(source, "user_id", "") or ""
    return ":".join(str(x) for x in (platform, chat, thread, user) if str(x)) or "global"


def _parse_claude_args(raw_args: str) -> dict:
    raw_args = raw_args or ""
    result = {
        "project": None,
        "project_given": False,
        "new": False,
        "once": False,
        "model": "claude-opus-4-8",
        "model_given": False,
        "timeout": DEFAULT_TIMEOUT_SECONDS,
        "prompt": "",
        "error": "",
    }
    if not raw_args.strip():
        return result
    try:
        parts = shlex.split(raw_args, posix=False)
    except ValueError as exc:
        result["error"] = str(exc)
        result["prompt"] = raw_args.strip()
        return result

    consumed_spans = []
    search_pos = 0
    for token in parts:
        idx = raw_args.find(token, search_pos)
        if idx < 0:
            idx = search_pos
        consumed_spans.append((idx, idx + len(token)))
        search_pos = idx + len(token)

    prompt_start_idx = None
    i = 0
    while i < len(parts):
        token = parts[i]
        if token == "--new":
            result["new"] = True
            i += 1
            continue
        if token == "--once":
            result["once"] = True
            i += 1
            continue
        if token in {"--project", "-p"}:
            if i + 1 >= len(parts):
                result["error"] = f"{token} requires a value"
                return result
            result["project"] = parts[i + 1].strip('"\'')
            result["project_given"] = True
            i += 2
            continue
        if token.startswith("--project="):
            result["project"] = token.split("=", 1)[1].strip('"\'')
            result["project_given"] = True
            i += 1
            continue
        if token == "--model":
            if i + 1 >= len(parts):
                result["error"] = "--model requires a value"
                return result
            result["model"] = token_model = parts[i + 1].strip('"\'')
            result["model_given"] = bool(token_model)
            i += 2
            continue
        if token.startswith("--model="):
            result["model"] = token.split("=", 1)[1].strip('"\'')
            result["model_given"] = True
            i += 1
            continue
        if token == "--timeout":
            if i + 1 >= len(parts):
                result["error"] = "--timeout requires a value"
                return result
            try:
                result["timeout"] = int(parts[i + 1])
            except ValueError:
                result["error"] = "--timeout must be an integer"
                return result
            i += 2
            continue
        if token.startswith("--timeout="):
            try:
                result["timeout"] = int(token.split("=", 1)[1])
            except ValueError:
                result["error"] = "--timeout must be an integer"
                return result
            i += 1
            continue
        prompt_start_idx = consumed_spans[i][0]
        break

    if prompt_start_idx is not None:
        result["prompt"] = raw_args[prompt_start_idx:].strip()
    return result


_MODEL_ALIASES = {
    "fable": "claude-fable-5",
    "fable5": "claude-fable-5",
    "fable-5": "claude-fable-5",
    "opus": "claude-opus-4-8",
    "opus4.8": "claude-opus-4-8",
    "opus-4.8": "claude-opus-4-8",
    "sonnet": "claude-sonnet-5",
    "sonnet5": "claude-sonnet-5",
    "sonnet-5": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
    "haiku4.5": "claude-haiku-4-5-20251001",
    "haiku-4.5": "claude-haiku-4-5-20251001",
}


def _normalize_model(raw: str) -> str:
    cleaned = (raw or "").strip().strip('"').strip("'")
    return _MODEL_ALIASES.get(cleaned.lower(), cleaned)


def _error_text(payload: dict) -> str:
    return str(payload.get("error") or payload.get("result") or "unknown error")


def _run_coro_blocking(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Plugin slash handlers in gateway can be async, but tests/CLI may call sync.
    raise RuntimeError("Cannot block an already running event loop; use the async handler path")


async def _handle_claude_async(raw_args: str) -> str:
    args = _parse_claude_args(raw_args)
    if args.get("error"):
        return f"/claude error: {args['error']}"
    if not args.get("prompt"):
        return (
            "Usage: `/claude [--project DIR] [--new] [--once] [--timeout SEC] <prompt>`\n"
            "Example: `/claude --project ERE-Software fix the failing tests`\n"
            "Use `/endclaude` to leave sticky Claude mode."
        )

    session_key = _session_key()
    state = _load_state()
    prior = state.get(session_key, {}) if isinstance(state.get(session_key, {}), dict) else {}
    project = args.get("project") if args.get("project_given") else prior.get("project")
    resume_session_id = None if args.get("new") else prior.get("session_id")
    model = args.get("model") if args.get("model_given") else (
        prior.get("model") or args.get("model") or "claude-opus-4-8"
    )

    async def run_relay(sid):
        raw = await asyncio.to_thread(
            relay_to_claude,
            prompt=args["prompt"],
            project=project,
            resume_session_id=sid,
            model=model,
            timeout=args.get("timeout") or DEFAULT_TIMEOUT_SECONDS,
        )
        return json.loads(raw) if isinstance(raw, str) else raw

    try:
        payload = await run_relay(resume_session_id)
    except Exception as exc:
        logger.warning("Claude relay failed: %s", exc)
        return f"Claude relay failed: {exc}"

    if not isinstance(payload, dict):
        return f"Claude relay returned an unexpected response: {payload!r}"
    if not payload.get("success"):
        err = _error_text(payload)
        if resume_session_id and "No conversation found with session ID" in err:
            try:
                payload = await run_relay(None)
            except Exception as exc:
                return f"Claude relay failed: {exc}"
            if not isinstance(payload, dict):
                return f"Claude relay returned an unexpected response: {payload!r}"
            if not payload.get("success"):
                return f"Claude relay failed: {_error_text(payload)}"
        else:
            return f"Claude relay failed: {err}"

    entry = dict(prior)
    if payload.get("session_id"):
        entry["session_id"] = payload.get("session_id")
    entry["project"] = project
    entry["workdir"] = payload.get("workdir")
    entry["model"] = payload.get("model") or model or "claude-opus-4-8"
    entry["active"] = not bool(args.get("once"))
    entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
    state[session_key] = entry
    _save_state(state)

    text = str(payload.get("result") or "").strip() or "Claude completed with no text result."
    if entry["active"]:
        text += "\n\n_Claude mode active — plain replies continue this Claude Code session. Use `/endclaude` to exit._"
    return text


async def _handle_endclaude_async(raw_args: str = "") -> str:
    session_key = _session_key()
    state = _load_state()
    entry = dict(state.get(session_key, {}) or {})
    entry["active"] = False
    entry["ended_at"] = datetime.now().isoformat(timespec="seconds")
    state[session_key] = entry
    _save_state(state)
    return "Claude mode ended. Future plain messages will go to Hermes again."


async def _handle_stopclaude_async(raw_args: str = "") -> str:
    state = _load_state()
    entry = state.get(_session_key(), {}) or {}
    if not entry.get("active") or not entry.get("session_id"):
        return "Claude mode is not active for this chat."
    return await _handle_claude_async("--once --timeout 300 /stop")


async def _handle_claudemodel_async(raw_args: str = "") -> str:
    raw = (raw_args or "").strip()
    session_key = _session_key()
    state = _load_state()
    entry = dict(state.get(session_key, {}) or {})
    current = entry.get("model") or "claude-opus-4-8"
    if not raw:
        return (
            f"Claude relay model for this chat: `{current}`\n"
            "Usage: `/claudemodel <model>` — e.g. `/claudemodel fable`, "
            "`/claudemodel claude-fable-5`, `opus`, `sonnet`, `haiku`.\n"
            "This sets the model for this chat's Claude Code relay only. Use `/model` to change Hermes's own model."
        )
    model = _normalize_model(raw)
    if not model:
        return "Usage: `/claudemodel <model>` (e.g. `fable`, `opus`, `claude-fable-5`)."
    entry["model"] = model
    entry["model_updated_at"] = datetime.now().isoformat(timespec="seconds")
    state[session_key] = entry
    _save_state(state)
    tail = " Your next message in this chat will use it." if entry.get("active") else " Start Claude mode with `/claude <prompt>` to use it."
    return f"Claude relay model set to `{model}` for this chat.{tail}"


def _remember_event_hook(event=None, gateway=None, **kwargs):
    """Capture gateway context and rewrite sticky plain-text replies into /claude."""
    global _CURRENT_EVENT, _CURRENT_GATEWAY
    if event is None:
        return None
    _CURRENT_EVENT = event
    _CURRENT_GATEWAY = gateway

    command = _event_command(getattr(event, "text", ""))
    if command:
        return None

    try:
        state = _load_state()
        entry = state.get(_session_key(event, gateway), {}) if isinstance(state, dict) else {}
    except Exception:
        entry = {}

    if isinstance(entry, dict) and entry.get("active"):
        text = getattr(event, "text", "") or ""
        return {"action": "rewrite", "text": f"/claude {text}"}
    return None


def register(ctx):
    """Register the relay tool, slash commands, and sticky-mode hook."""
    ctx.register_tool(
        name="relay_to_claude",
        toolset="relay",
        schema=RELAY_TO_CLAUDE_SCHEMA,
        handler=lambda args, **kw: relay_to_claude(
            prompt=args.get("prompt", ""),
            project=args.get("project"),
            resume_session_id=args.get("resume_session_id"),
            model=args.get("model", "claude-opus-4-8"),
            timeout=args.get("timeout", DEFAULT_TIMEOUT_SECONDS),
            agent=kw.get("agent"),
            task_id=kw.get("task_id"),
        ),
        check_fn=check_requirements,
        requires_env=[],
        emoji="🔗",
    )

    ctx.register_hook("pre_gateway_dispatch", _remember_event_hook)
    ctx.register_command("claude", _handle_claude_async, description="Relay prompt to Claude Code CLI", args_hint="[--project DIR] [--new] [--once] <prompt>")
    ctx.register_command("claudemodel", _handle_claudemodel_async, description="Set this chat's Claude Code relay model", args_hint="<model>")
    ctx.register_command("endclaude", _handle_endclaude_async, description="End sticky Claude Code relay mode")
    ctx.register_command("stopclaude", _handle_stopclaude_async, description="Send /stop to the active Claude Code relay session")
