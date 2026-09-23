import base64
import io
import json
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


def test_import_is_lazy_and_isolated(tmp_path):
    settings = WebUISettingsConfig(tmp_path / "config.json")
    settings.update(lambda c: setattr(c.channels, "telegram", {"enabled": True, "token": "parent-only"}))
    manager = CharacterManager(settings)
    ids = [manager.import_card({"data": base64.b64encode(encoded_card(name)).decode()})["id"]
           for name in ("Alice", "Bob")]
    assert ids[0] != ids[1]
    assert not manager._leases
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
