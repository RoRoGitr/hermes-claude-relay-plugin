"""Claude Code relay subprocess runner for the Hermes Claude relay plugin."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Optional

DEFAULT_PROJECT_ROOT = os.getenv(
    "CLAUDE_RELAY_PROJECT_ROOT",
    str(Path.home() / "Claude" / "RoClaude_Code" / "Projects"),
)
DEFAULT_TIMEOUT_SECONDS = 1800
MAX_TIMEOUT_SECONDS = 3600


def resolve_claude_binary() -> Optional[str]:
    """Locate the official Claude Code CLI."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "claude",
        Path.home() / ".local" / "bin" / "claude.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def check_requirements() -> bool:
    return resolve_claude_binary() is not None


def resolve_workdir(project: Optional[str]) -> str:
    """Resolve a bare project name under DEFAULT_PROJECT_ROOT or an absolute path."""
    if not project or not str(project).strip():
        return DEFAULT_PROJECT_ROOT
    project = str(project).strip()
    if os.path.isabs(project):
        return project
    return os.path.join(DEFAULT_PROJECT_ROOT, project)


def relay_to_claude(
    prompt: str,
    project: Optional[str] = None,
    resume_session_id: Optional[str] = None,
    model: str = "claude-opus-4-8",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    agent=None,
    task_id: Optional[str] = None,
) -> str:
    """Run `claude -p` and return a JSON envelope."""
    if not prompt or not prompt.strip():
        return json.dumps({"success": False, "error": "prompt is required."}, ensure_ascii=False)

    binary = resolve_claude_binary()
    if not binary:
        return json.dumps(
            {"success": False, "error": "claude CLI not found on PATH or ~/.local/bin."},
            ensure_ascii=False,
        )

    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    timeout = max(30, min(timeout, MAX_TIMEOUT_SECONDS))

    workdir = resolve_workdir(project)
    if not os.path.isdir(workdir):
        return json.dumps(
            {"success": False, "error": f"Project directory does not exist: {workdir}"},
            ensure_ascii=False,
        )

    cmd = [
        binary,
        "-p", prompt,
        "--model", model,
        "--permission-mode", "bypassPermissions",
        "--output-format", "json",
    ]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]

    env = dict(os.environ)
    env.setdefault("CLAUDE_CONFIG_DIR", os.path.expanduser(r"~\Claude\.claude"))

    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=workdir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    try:
        from tools.environments.base import touch_activity_if_due
    except Exception:
        touch_activity_if_due = None

    hb_state = {"last_touch": 0.0, "start": time.monotonic(), "interval": 5.0}

    def heartbeat():
        while proc.poll() is None:
            if touch_activity_if_due is not None:
                touch_activity_if_due(hb_state, "claude-code-relay: Claude Code running")
            time.sleep(2)

    threading.Thread(target=heartbeat, daemon=True).start()

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass
        return json.dumps(
            {"success": False, "error": f"Claude relay timed out after {timeout}s.", "workdir": workdir},
            ensure_ascii=False,
        )

    elapsed = round(time.monotonic() - start, 1)

    if proc.returncode != 0 and not stdout.strip():
        return json.dumps(
            {
                "success": False,
                "error": f"claude exited {proc.returncode}: {(stderr or '').strip()[:600]}",
                "workdir": workdir,
                "elapsed_s": elapsed,
            },
            ensure_ascii=False,
        )

    payload = None
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    if payload is None:
        return json.dumps(
            {
                "success": True,
                "result": stdout.strip()[:8000],
                "session_id": None,
                "workdir": workdir,
                "model": model,
                "elapsed_s": elapsed,
                "note": "Non-JSON output; returned raw.",
            },
            ensure_ascii=False,
        )

    is_error = bool(payload.get("is_error", False))
    result_text = payload.get("result", "")
    error_text = None
    if is_error:
        error_text = str(result_text or stderr or f"claude exited {proc.returncode}").strip()

    return json.dumps(
        {
            "success": not is_error,
            "is_error": is_error,
            "result": result_text,
            "error": error_text,
            "session_id": payload.get("session_id"),
            "num_turns": payload.get("num_turns"),
            "cost_usd": payload.get("total_cost_usd"),
            "stop_reason": payload.get("stop_reason"),
            "permission_denials": payload.get("permission_denials"),
            "workdir": workdir,
            "model": model,
            "elapsed_s": elapsed,
        },
        ensure_ascii=False,
    )


RELAY_TO_CLAUDE_SCHEMA = {
    "name": "relay_to_claude",
    "description": (
        "Relay a task to the official Claude Code CLI and return Claude's final "
        "answer plus session_id. Use for coding/project work that should run "
        "through Claude Code with its own auth, MCPs, and billing path."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Complete instruction for Claude Code."},
            "project": {"type": "string", "description": "Bare project name under CLAUDE_RELAY_PROJECT_ROOT or absolute path."},
            "resume_session_id": {"type": "string", "description": "Claude Code session_id to resume."},
            "model": {"type": "string", "description": "Claude model. Default claude-opus-4-8."},
            "timeout": {"type": "integer", "description": "Max seconds, default 1800, cap 3600."},
        },
        "required": ["prompt"],
    },
}
