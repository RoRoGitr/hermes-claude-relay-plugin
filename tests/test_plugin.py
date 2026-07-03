import asyncio
import importlib.util
import json
import sys
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
async def test_endclaude_says_not_active_when_mode_already_inactive(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(plugin, "_session_key", lambda event=None, gateway=None: "telegram:chat:user")
    (tmp_path / "state.json").write_text(json.dumps({"telegram:chat:user": {"active": False, "session_id": "sid-123"}}))

    result = await plugin._handle_endclaude_async()

    assert "Claude mode is not active" in result
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["telegram:chat:user"]["active"] is False


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
