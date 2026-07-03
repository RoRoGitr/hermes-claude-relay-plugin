"""Tests for Claude Code relay process tracking."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("claude_code_relay_runner", ROOT / "relay.py")
relay = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = relay
spec.loader.exec_module(relay)


def test_stop_claude_relay_process_kills_registered_process(monkeypatch):
    calls = []
    proc = SimpleNamespace(pid=1234, poll=lambda: None)
    relay.RUNNING_CLAUDE_PROCS.clear()
    relay.register_running_proc("session-1", proc)

    monkeypatch.setattr(relay, "kill_process_tree", lambda p: calls.append(p) or True)

    result = relay.stop_claude_relay_process("session-1")

    assert result["success"] is True
    assert result["stopped"] is True
    assert result["pid"] == 1234
    assert calls == [proc]
    assert "session-1" not in relay.RUNNING_CLAUDE_PROCS


def test_stop_claude_relay_process_reports_missing_process():
    relay.RUNNING_CLAUDE_PROCS.clear()

    result = relay.stop_claude_relay_process("missing")

    assert result["success"] is False
    assert result["stopped"] is False
    assert "No running Claude relay process" in result["error"]


def test_clear_running_proc_does_not_remove_newer_process():
    old_proc = SimpleNamespace(pid=1)
    new_proc = SimpleNamespace(pid=2)
    relay.RUNNING_CLAUDE_PROCS.clear()
    relay.register_running_proc("session-1", old_proc)
    relay.register_running_proc("session-1", new_proc)

    relay.clear_running_proc("session-1", old_proc)

    assert relay.RUNNING_CLAUDE_PROCS["session-1"] is new_proc
