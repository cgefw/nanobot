import base64
import io
import json
import struct
import zlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image, PngImagePlugin

from nanobot.agent.autocompact import AutoCompact
from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.roleplay.cards import MAX_CARD_BYTES, CharacterProfile, parse_card
from nanobot.roleplay.manager import CharacterManager
from nanobot.webui.settings_services import WebUISettingsConfig


def encoded_card(name="Alice", **fields):
    return json.dumps({"name": name, **fields}).encode()


def test_png_metadata_avatar_and_v2(tmp_path):
    data = {"spec": "chara_card_v2", "data": {"name": "Alice", "first_mes": "Hello {{user}}"}}
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("chara", base64.b64encode(json.dumps(data).encode()).decode())
    raw = io.BytesIO()
    Image.new("RGB", (1024, 768)).save(raw, "PNG", pnginfo=metadata)
    imported = parse_card(raw.getvalue(), "alice.png")
    assert imported.card.name == "Alice"
    avatar = Image.open(io.BytesIO(imported.avatar))
    assert max(avatar.size) == 512
    assert "chara" not in avatar.info
    assert json.loads(imported.source) == data


@pytest.mark.parametrize("keyword", ["chara", "ccv3"])
def test_png_card_metadata_after_image_data(keyword):
    raw = io.BytesIO()
    Image.new("RGB", (8, 8)).save(raw, "PNG")
    source = encoded_card()
    payload = keyword.encode() + b"\0" + base64.b64encode(source)
    chunk = struct.pack(">I", len(payload)) + b"tEXt" + payload
    chunk += struct.pack(">I", zlib.crc32(b"tEXt" + payload))
    png = raw.getvalue()
    imported = parse_card(png[:-12] + chunk + png[-12:], "late.png")
    assert imported.source == source
    assert imported.card.name == "Alice"
    assert imported.avatar


def test_plain_png_explains_missing_character_data():
    raw = io.BytesIO()
    Image.new("RGB", (8, 8)).save(raw, "PNG")
    with pytest.raises(ValueError, match="PNG 中未找到角色卡数据"):
        parse_card(raw.getvalue(), "portrait.png")


def test_aicc_fields_survive_import_and_reload(tmp_path):
    data = {"name": "Alice", "general_description": "Description", "appearance": "Appearance",
            "background_history": "Background", "world_setting_context": "Scenario",
            "personality": {"core": "Kind", "behavior_rules": ["Patient"],
                            "speech_style": {"tone": "Gentle", "patterns": ["Hello"]}},
            "dialogue": {"greetings": ["Hi {{user}}", "Welcome"], "dialogue_examples": ["Example"]},
            "prompts": {"system_prompt": "Stay in character", "post_history_instructions": "After"},
            "world": {"worldbook_entries": [{"keys": ["tea"], "content": "Likes tea"}]},
            "metadata": {"creator": "Author", "notes": "Notes", "tags": ["friendly"]}}
    source = json.dumps({"spec": "aicc_card", "spec_version": "1.0", "data": data}).encode()
    imported = parse_card(source)
    path = tmp_path / "card.json"
    path.write_bytes(imported.source)
    assert imported.source == source
    profile = CharacterProfile(path)
    assert all(text in profile.identity() for text in (
        "Description", "Appearance", "Background", "Scenario", "Kind", "Patient", "Gentle", "Example",
    ))
    assert profile.greetings() == ["Hi 用户", "Welcome"]
    assert profile.post_history() == "After"
    assert profile.lore([], "tea")[1] == "Likes tea"
    assert profile.card.creator_notes == "Notes"
    assert profile.card.creator == "Author"
    assert profile.card.tags == ["friendly"]
    assert any("AICC" in warning for warning in imported.warnings)


@pytest.mark.parametrize("data", [None, [], {"name": "A", "personality": "invalid"},
                                 {"name": "A", "dialogue": {"greetings": [123]}}])
def test_invalid_aicc_fields_are_rejected(data):
    with pytest.raises(ValueError):
        parse_card(json.dumps({"spec": "aicc_card", "data": data}).encode())


@pytest.mark.parametrize("raw,filename", [
    (b"{}", "x.png"), (b"[]", "x.json"), (b"broken", "x.json"),
    (encoded_card() + b" " * MAX_CARD_BYTES, "x.json"),
    (b'{"spec":"unknown","data":{"name":"A"}}', "x.json"),
])
def test_invalid_import_is_rejected(raw, filename):
    with pytest.raises(ValueError):
        parse_card(raw, filename)


def test_v3_preserves_extra_fields_and_warns():
    raw = json.dumps({"spec": "chara_card_v3", "data": {"name": "A", "assets": [],
        "extensions": {"script": "do not execute"}}}).encode()
    parsed = parse_card(raw)
    assert parsed.source == raw
    assert len(parsed.warnings) == 2


def test_cached_card_lore_and_fixed_identity(tmp_path, monkeypatch):
    card = tmp_path / "card.json"
    card.write_bytes(encoded_card(description="{{char}} knows {{user}}", first_mes="Hi {{user}}",
        alternate_greetings=["Welcome"], post_history_instructions="Stay {{char}}",
        character_book={"scan_depth": 1, "token_budget": 500, "entries": [
            {"keys": ["tea"], "content": "{{char}} loves tea", "position": "before_char"},
            {"keys": ["ancient"], "content": "OLD MATCH"},
            {"keys": ["tea"], "secondary_keys": ["milk"], "selective": True, "content": "WITH MILK"},
            {"constant": True, "content": "ALWAYS"},
        ]}))
    profile = CharacterProfile(card, "Bob")
    first = profile.card
    monkeypatch.setattr(Path, "read_bytes", lambda _: pytest.fail("unchanged card was reparsed"))
    assert profile.card is first
    before, after = profile.lore([{"role": "user", "content": "ancient"},
                                 {"role": "assistant", "content": "tea"}], "milk")
    assert before == "Alice loves tea"
    assert "WITH MILK" in after and "ALWAYS" in after and "OLD MATCH" not in after
    assert profile.greetings() == ["Hi Bob", "Welcome"]
    assert "Alice knows Bob" in profile.identity()


def test_context_preserves_tools_but_replaces_soul(tmp_path):
    card = tmp_path / "card.json"
    card.write_bytes(encoded_card(description="FIXED ROLE", system_prompt="{{original}}\nCustom role",
                                  post_history_instructions="After history"))
    (tmp_path / "SOUL.md").write_text("WRONG SOUL")
    context = ContextBuilder(tmp_path, character_card=str(card))
    messages = context.build_messages([], "hi")
    assert "FIXED ROLE" in messages[0]["content"]
    assert "WRONG SOUL" not in messages[0]["content"]
    assert "Custom role" in messages[0]["content"]
    assert context.skills is not None
    assert messages[-1] == {"role": "system", "content": "After history"}


@pytest.mark.asyncio
async def test_dream_cannot_rewrite_character_or_skills(tmp_path):
    store = MemoryStore(tmp_path, fixed_character=True)
    store.soul_file.write_text("Fixed soul")
    tools = store.build_dream_tools()
    for path in ["SOUL.md", "../card.json", "skills/demo/SKILL.md", "memory/.cursor",
                 "memory/history.jsonl"]:
        result = await tools.execute("write_file", {"path": path, "content": "changed"})
        assert "Error" in result
    result = await tools.execute("write_file", {"path": "USER.md", "content": "Name: Bob"})
    assert "Successfully" in result
    assert store.soul_file.read_text() == "Fixed soul"
    assert "fixed" in store._dream_template().lower()


def test_idle_disabled_does_not_scan_sessions():
    sessions = MagicMock()
    compact = AutoCompact(sessions, MagicMock())
    compact.check_expired(MagicMock(), MagicMock())
    sessions.list_sessions.assert_not_called()


@pytest.mark.parametrize("idle_minutes", [0, 15, 30])
def test_import_is_lazy_and_isolated(tmp_path, idle_minutes):
    settings = WebUISettingsConfig(tmp_path / "config.json")
    settings.update(lambda c: setattr(c.channels, "telegram", {"enabled": True, "token": "parent-only"}))
    settings.update(lambda c: setattr(c.agents.defaults, "session_ttl_minutes", idle_minutes))
    manager = CharacterManager(settings)
    ids = [manager.import_card({"data": base64.b64encode(encoded_card(name)).decode()})["id"]
           for name in ("Alice", "Bob")]
    assert ids[0] != ids[1]
    assert not manager._leases
    assert settings.load().agents.defaults.session_ttl_minutes == idle_minutes
    for role_id in ids:
        raw = json.loads((manager.directory(role_id) / "config.json").read_text())
        assert raw["channels"]["telegram"]["enabled"] is False
        assert raw["gateway"]["heartbeat"]["enabled"] is False
        assert role_id in raw["agents"]["defaults"]["workspace"]
        assert raw["agents"]["defaults"]["idleCompactAfterMinutes"] == 0
    with pytest.raises(ValueError):
        manager.directory("../config.json")
    assert not list((tmp_path / "characters").glob(".import-*"))


def test_mcp_callback_keeps_character_routing():
    from nanobot.webui.mcp_oauth_api import McpOAuthError, validate_mcp_oauth_redirect_uri
    prefix = "/_characters/0123456789abcdef0123456789abcdef"
    url = f"https://chat.example{prefix}/auth/mcp/callback"
    assert validate_mcp_oauth_redirect_uri(url) == url
    for path in ["/_characters/../auth/mcp/callback", prefix + "/elsewhere"]:
        with pytest.raises(McpOAuthError):
            validate_mcp_oauth_redirect_uri("https://chat.example" + path)
