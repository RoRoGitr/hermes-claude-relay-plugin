import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "claude_code_relay_plugin", ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


def test_parse_preserves_prompt_flags_and_quotes():
    parsed = plugin._parse_claude_args('--new --project ERE-Software --model fable audit --dry-run "quoted"')
    assert parsed["new"] is True
    assert parsed["project"] == "ERE-Software"
    assert parsed["model"] == "fable"
    assert parsed["model_given"] is True
    assert parsed["prompt"] == 'audit --dry-run "quoted"'


def test_model_aliases():
    assert plugin._normalize_model("fable") == "claude-fable-5"
    assert plugin._normalize_model("opus") == "claude-opus-4-8"
    assert plugin._normalize_model("claude-custom") == "claude-custom"


@pytest.mark.asyncio
async def test_claudemodel_sets_per_session_state(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(plugin, "_session_key", lambda event=None, gateway=None: "telegram:chat:user")
    result = await plugin._handle_claudemodel_async("fable")
    assert "claude-fable-5" in result
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["telegram:chat:user"]["model"] == "claude-fable-5"


@pytest.mark.asyncio
async def test_claude_relay_persists_session(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(plugin, "_session_key", lambda event=None, gateway=None: "telegram:chat:user")

    def fake_relay_to_claude(**kwargs):
        return json.dumps({
            "success": True,
            "result": "done",
            "session_id": "sid-123",
            "workdir": "C:/Projects/Demo",
            "model": kwargs["model"],
        })

    monkeypatch.setattr(plugin, "relay_to_claude", fake_relay_to_claude)
    result = await plugin._handle_claude_async("--project Demo --model fable fix tests")
    assert "done" in result
    data = json.loads((tmp_path / "state.json").read_text())
    entry = data["telegram:chat:user"]
    assert entry["session_id"] == "sid-123"
    assert entry["model"] == "fable"
    assert entry["active"] is True


@pytest.mark.asyncio
async def test_claude_relay_sends_bare_progress_ticks_while_running(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(plugin, "_session_key", lambda event=None, gateway=None: "telegram:chat:user")
    monkeypatch.setattr(plugin, "_CLAUDE_PROGRESS_INTERVAL_SECONDS", 0.01)

    class FakeAdapter:
        def __init__(self):
            self.statuses = []

        async def send_or_update_status(self, chat_id, status_key, content, metadata=None):
            self.statuses.append((chat_id, status_key, content, metadata))
            return SimpleNamespace(success=True, message_id="status-1")

    adapter = FakeAdapter()
    source = SimpleNamespace(platform="telegram", chat_id="chat", user_id="user")
    monkeypatch.setattr(plugin, "_CURRENT_EVENT", SimpleNamespace(source=source))
    monkeypatch.setattr(
        plugin,
        "_CURRENT_GATEWAY",
        SimpleNamespace(
            adapters={"telegram": adapter},
            _reply_anchor_for_event=lambda event: None,
            _thread_metadata_for_source=lambda source, reply_anchor=None: None,
        ),
    )

    def fake_relay_to_claude(**kwargs):
        time.sleep(0.04)
        return json.dumps({"success": True, "result": "done", "session_id": "sid-123"})

    monkeypatch.setattr(plugin, "relay_to_claude", fake_relay_to_claude)

    result = await plugin._handle_claude_async("long task")

    assert "done" in result
    assert adapter.statuses
    chat_id, status_key, content, _metadata = adapter.statuses[-1]
    assert chat_id == "chat"
    assert status_key == "claude-relay:telegram:chat:user"
    assert content == "⏳ Working — 1 min — Claude relay running"


@pytest.mark.asyncio
async def test_endclaude_says_not_active_when_mode_already_inactive(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(plugin, "_session_key", lambda event=None, gateway=None: "telegram:chat:user")
    (tmp_path / "state.json").write_text(json.dumps({"telegram:chat:user": {"active": False, "session_id": "sid-123"}}))

    result = await plugin._handle_endclaude_async()

    assert "Claude mode is not active" in result
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["telegram:chat:user"]["active"] is False


@pytest.mark.asyncio
async def test_endclaude_kills_running_process_and_deactivates_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(plugin, "_session_key", lambda event=None, gateway=None: "telegram:chat:user")
    (tmp_path / "state.json").write_text(json.dumps({"telegram:chat:user": {"active": True, "session_id": "sid-123"}}))

    captured = {}
    def fake_stop(session_key):
        captured["session_key"] = session_key
        return {"success": True, "stopped": True, "pid": 1234}

    monkeypatch.setattr(plugin, "stop_claude_relay_process", fake_stop)

    result = await plugin._handle_endclaude_async()

    assert "Claude relay process stopped" in result
    assert "Claude mode ended" in result
    assert captured["session_key"] == "telegram:chat:user"
    entry = json.loads((tmp_path / "state.json").read_text())["telegram:chat:user"]
    assert entry["active"] is False
    assert "ended_at" in entry


@pytest.mark.asyncio
async def test_stopclaude_kills_running_process_and_keeps_mode_active(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(plugin, "_session_key", lambda event=None, gateway=None: "telegram:chat:user")
    (tmp_path / "state.json").write_text(json.dumps({"telegram:chat:user": {"active": True, "session_id": "sid-123"}}))

    captured = {}
    def fake_stop(session_key):
        captured["session_key"] = session_key
        return {"success": True, "stopped": True, "pid": 1234}

    monkeypatch.setattr(plugin, "stop_claude_relay_process", fake_stop)

    result = await plugin._handle_stopclaude_async()

    assert "stopped" in result.lower()
    assert captured["session_key"] == "telegram:chat:user"
    entry = json.loads((tmp_path / "state.json").read_text())["telegram:chat:user"]
    assert entry["active"] is True
    assert "stopped_at" in entry
    assert "ended_at" not in entry


@pytest.mark.asyncio
async def test_stopclaude_keeps_mode_active_when_no_process_running(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(plugin, "_session_key", lambda event=None, gateway=None: "telegram:chat:user")
    (tmp_path / "state.json").write_text(json.dumps({"telegram:chat:user": {"active": True, "session_id": "sid-123"}}))
    monkeypatch.setattr(
        plugin,
        "stop_claude_relay_process",
        lambda session_key: {"success": False, "stopped": False, "error": "No running Claude relay process for this chat."},
    )

    result = await plugin._handle_stopclaude_async()

    assert "No running Claude relay process" in result
    assert "Claude mode is still active" in result
    entry = json.loads((tmp_path / "state.json").read_text())["telegram:chat:user"]
    assert entry["active"] is True


def test_pre_gateway_dispatch_rewrites_plain_text_when_active(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_state_path", lambda: tmp_path / "state.json")
    (tmp_path / "state.json").write_text(json.dumps({"key": {"active": True}}))
    source = SimpleNamespace(platform="telegram", chat_id="chat", user_id="user")
    event = SimpleNamespace(text="continue", source=source)
    gateway = SimpleNamespace(_session_key_for_source=lambda source: "key")
    result = plugin._remember_event_hook(event=event, gateway=gateway)
    assert result == {"action": "rewrite", "text": "/claude continue"}


def test_register_registers_expected_surfaces():
    calls = {"tools": [], "hooks": [], "commands": []}

    class Ctx:
        def register_tool(self, **kwargs):
            calls["tools"].append(kwargs["name"])
        def register_hook(self, name, handler):
            calls["hooks"].append(name)
        def register_command(self, name, handler, description="", args_hint=""):
            calls["commands"].append(name)

    plugin.register(Ctx())
    assert calls["tools"] == ["relay_to_claude"]
    assert calls["hooks"] == ["pre_gateway_dispatch"]
    assert calls["commands"] == ["claude", "claudemodel", "endclaude", "stopclaude"]



def test_plugin_relay_uses_stream_json_and_records_activity(monkeypatch, tmp_path):
    import relay

    relay.RUNNING_CLAUDE_PROCS.clear()
    relay.CLAUDE_RELAY_STATUSES.clear()
    monkeypatch.setattr(relay, "resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(relay, "resolve_workdir", lambda project: str(tmp_path))

    captured = {}

    class FakePipe:
        def __init__(self, lines):
            self._lines = list(lines)
        def readline(self):
            if self._lines:
                return self._lines.pop(0)
            return ""

    class FakeProc:
        pid = 5432
        returncode = 0
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            self.stdout = FakePipe([
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working"}]}}) + "\n",
                json.dumps({"type": "tool_use", "name": "Bash"}) + "\n",
                json.dumps({"type": "result", "result": "finished", "session_id": "sid-stream", "num_turns": 2}) + "\n",
            ])
            self.stderr = FakePipe([])
        def poll(self):
            return None if self.stdout._lines else self.returncode
        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(relay.subprocess, "Popen", FakeProc)

    result = json.loads(relay.relay_to_claude("do work", session_key="session-stream", timeout=30))

    assert "stream-json" in captured["cmd"]
    assert "--include-partial-messages" in captured["cmd"]
    assert "--include-hook-events" in captured["cmd"]
    assert result["success"] is True
    assert result["result"] == "finished"
    assert result["last_activity_kind"] == "tool_use"
    status = relay.get_claude_relay_status("session-stream")
    assert status["running"] is False
    assert status["last_activity_kind"] == "tool_use"
