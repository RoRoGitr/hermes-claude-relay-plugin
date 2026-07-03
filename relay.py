"""Claude Code relay subprocess runner for the Hermes Claude relay plugin."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
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

RUNNING_CLAUDE_PROCS: dict[str, subprocess.Popen] = {}
RUNNING_CLAUDE_PROCS_LOCK = threading.RLock()

CLAUDE_RELAY_STATUSES: dict[str, dict] = {}
CLAUDE_RELAY_STATUSES_LOCK = threading.RLock()


def set_claude_relay_status(session_key: Optional[str], **fields) -> None:
    if not session_key:
        return
    with CLAUDE_RELAY_STATUSES_LOCK:
        entry = dict(CLAUDE_RELAY_STATUSES.get(session_key, {}))
        entry.update(fields)
        CLAUDE_RELAY_STATUSES[session_key] = entry


def get_claude_relay_status(session_key: str) -> dict:
    """Return live/last activity metadata for a relayed Claude Code run."""
    with CLAUDE_RELAY_STATUSES_LOCK:
        entry = dict(CLAUDE_RELAY_STATUSES.get(session_key, {}))
    now = time.monotonic()
    if entry.get("started_at_monotonic") is not None:
        entry["elapsed_s"] = round(now - float(entry["started_at_monotonic"]), 1)
    if entry.get("last_activity_at_monotonic") is not None:
        entry["last_activity_age_s"] = round(now - float(entry["last_activity_at_monotonic"]), 1)
    return entry


def activity_from_claude_stream_event(event: dict) -> Optional[dict]:
    """Summarize a Claude stream-json event as user-visible liveness metadata."""
    if not isinstance(event, dict):
        return None
    kind = str(event.get("type") or event.get("event") or "event")
    if kind == "result":
        return None
    summary = kind
    if kind in {"tool_use", "tool_result"}:
        name = event.get("name") or event.get("tool_name")
        if name:
            summary = f"{kind}: {name}"
    elif kind == "assistant":
        summary = "assistant output"
    elif kind == "user":
        summary = "tool/result update"
    elif "hook" in kind:
        summary = kind.replace("_", " ")
    return {"last_activity_kind": kind, "last_activity_summary": summary}


def reader_thread(pipe, stream_name: str, out_queue) -> None:
    try:
        while True:
            line = pipe.readline()
            if not line:
                break
            out_queue.put((stream_name, line))
    finally:
        out_queue.put((stream_name, None))



def register_running_proc(session_key: Optional[str], proc: subprocess.Popen) -> None:
    if not session_key:
        return
    with RUNNING_CLAUDE_PROCS_LOCK:
        RUNNING_CLAUDE_PROCS[session_key] = proc


def clear_running_proc(session_key: Optional[str], proc: subprocess.Popen) -> None:
    if not session_key:
        return
    with RUNNING_CLAUDE_PROCS_LOCK:
        if RUNNING_CLAUDE_PROCS.get(session_key) is proc:
            RUNNING_CLAUDE_PROCS.pop(session_key, None)


def kill_process_tree(proc: subprocess.Popen) -> bool:
    if proc.poll() is not None:
        return False
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return True
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return True


def stop_claude_relay_process(session_key: str) -> dict:
    with RUNNING_CLAUDE_PROCS_LOCK:
        proc = RUNNING_CLAUDE_PROCS.pop(session_key, None)
    if proc is None or proc.poll() is not None:
        return {
            "success": False,
            "stopped": False,
            "error": "No running Claude relay process for this chat.",
        }
    pid = proc.pid
    stopped = kill_process_tree(proc)
    return {"success": bool(stopped), "stopped": bool(stopped), "pid": pid}


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
    session_key: Optional[str] = None,
) -> str:
    """Relay a task to the official Claude Code CLI and return its result.

    Uses Claude Code's stream-json mode so Hermes can track real activity
    (assistant chunks, tool/hook events) instead of only knowing that the
    subprocess is still alive. The hard timeout remains an absolute wall-clock
    ceiling; silent periods are surfaced via get_claude_relay_status().
    """
    if not prompt or not prompt.strip():
        return json.dumps({"success": False, "error": "prompt is required."},
                          ensure_ascii=False)

    binary = resolve_claude_binary()
    if not binary:
        return json.dumps(
            {"success": False,
             "error": "claude CLI not found on PATH or ~/.local/bin."},
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
            {"success": False,
             "error": f"Project directory does not exist: {workdir}"},
            ensure_ascii=False,
        )

    cmd = [
        binary,
        "-p", prompt,
        "--model", model,
        "--permission-mode", "bypassPermissions",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--include-hook-events",
    ]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]

    env = dict(os.environ)
    env.setdefault("CLAUDE_CONFIG_DIR", os.path.expanduser(r"~\Claude\.claude"))

    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    import queue
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
        **popen_kwargs,
    )
    register_running_proc(session_key, proc)
    set_claude_relay_status(
        session_key,
        running=True,
        pid=proc.pid,
        started_at_monotonic=start,
        last_activity_at_monotonic=start,
        last_activity_kind="started",
        last_activity_summary="Claude Code process started",
        stalled=False,
    )

    try:
        from tools.environments.base import touch_activity_if_due
    except Exception:
        touch_activity_if_due = None

    hb_state = {"last_touch": 0.0, "start": time.monotonic(), "interval": 5.0}
    events = queue.Queue()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    stdout_done = False
    stderr_done = False
    payload = None

    if proc.stdout is not None:
        threading.Thread(target=reader_thread, args=(proc.stdout, "stdout", events), daemon=True).start()
    else:
        stdout_done = True
    if proc.stderr is not None:
        threading.Thread(target=reader_thread, args=(proc.stderr, "stderr", events), daemon=True).start()
    else:
        stderr_done = True

    try:
        while True:
            if time.monotonic() - start > timeout:
                kill_process_tree(proc)
                set_claude_relay_status(session_key, running=False, timed_out=True, stalled=False)
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                return json.dumps(
                    {"success": False,
                     "error": f"Claude relay timed out after {timeout}s.",
                     "workdir": workdir,
                     **get_claude_relay_status(session_key or "")},
                    ensure_ascii=False,
                )

            if touch_activity_if_due is not None:
                touch_activity_if_due(hb_state, "claude-code-relay: Claude Code running")

            try:
                stream_name, line = events.get(timeout=0.25)
            except queue.Empty:
                if proc.poll() is not None and stdout_done and stderr_done:
                    break
                continue

            if line is None:
                if stream_name == "stdout":
                    stdout_done = True
                elif stream_name == "stderr":
                    stderr_done = True
                if proc.poll() is not None and stdout_done and stderr_done:
                    break
                continue

            if stream_name == "stderr":
                stderr_lines.append(line)
                continue

            stdout_lines.append(line)
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                payload = event
                set_claude_relay_status(session_key, final_event_seen=True)
                continue
            activity = activity_from_claude_stream_event(event)
            if activity:
                set_claude_relay_status(
                    session_key,
                    last_activity_at_monotonic=time.monotonic(),
                    stalled=False,
                    **activity,
                )

        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    finally:
        clear_running_proc(session_key, proc)
        set_claude_relay_status(session_key, running=False)

    elapsed = round(time.monotonic() - start, 1)
    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)

    if payload is None:
        for line in reversed(stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if candidate.get("type") == "result" or "result" in candidate:
                    payload = candidate
                    break

    if proc.returncode != 0 and payload is None:
        return json.dumps(
            {"success": False,
             "error": f"claude exited {proc.returncode}: {(stderr or '').strip()[:600]}",
             "workdir": workdir,
             "elapsed_s": elapsed,
             **get_claude_relay_status(session_key or "")},
            ensure_ascii=False,
        )

    if payload is None:
        return json.dumps(
            {"success": True,
             "result": stdout.strip()[:8000],
             "session_id": None,
             "workdir": workdir,
             "model": model,
             "elapsed_s": elapsed,
             "note": "Non-JSON output; returned raw.",
             **get_claude_relay_status(session_key or "")},
            ensure_ascii=False,
        )

    is_error = bool(payload.get("is_error", False))
    result_text = payload.get("result", "")
    error_text = None
    if is_error:
        error_text = str(result_text or stderr or f"claude exited {proc.returncode}").strip()
    status = get_claude_relay_status(session_key or "")
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
            **status,
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
