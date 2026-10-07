"""QQ role lifecycle without contacting QQ or starting external processes."""

import asyncio
import base64
import io
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image, PngImagePlugin

from nanobot.roleplay.cards import CharacterProfile
from nanobot.roleplay.manager import CharacterManager
from nanobot.webui.settings_services import WebUISettingsConfig


@pytest.fixture
def manager(tmp_path):
    return CharacterManager(WebUISettingsConfig(tmp_path / "config.json"))


async def test_update_card_preserves_instance_and_reloads_cached_profile(manager):
    role_id = manager.import_card({"data": base64.b64encode(b'{"name":"Before"}').decode()})["id"]
    directory = manager.directory(role_id)
    history = directory / "workspace" / "sessions" / "chat.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text("existing conversation")
    memory = directory / "workspace" / "MEMORY.md"
    memory.write_text("existing memory")
    (directory / "avatar.png").write_bytes(b"existing avatar")
    before = (directory / "config.json").read_bytes()
    profile = CharacterProfile(directory / "card.json")
    assert profile.card.name == "Before"
    payload = {"id": role_id, "data": base64.b64encode(b'{"name":"After","first_mes":"Hi"}').decode()}
    preview = await manager.mutate("characters.update", {**payload, "preview": True})
    assert preview["card"]["name"] == "After"
    assert profile.card.name == "Before"
    await manager.mutate("characters.update", payload)
    assert profile.card.name == "After"
    assert profile.greetings() == ["Hi"]
    assert len(manager.entries()) == 1
    assert manager.entries()[role_id]["name"] == "After"
    assert history.read_text() == "existing conversation"
    assert memory.read_text() == "existing memory"
    assert (directory / "config.json").read_bytes() == before
    assert (directory / "avatar.png").read_bytes() == b"existing avatar"


async def test_update_png_replaces_avatar_in_the_same_instance(manager):
    role_id = manager.import_card({"data": base64.b64encode(b'{"name":"Before"}').decode()})["id"]
    directory = manager.directory(role_id)
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("chara", base64.b64encode(b'{"name":"After"}').decode())
    raw = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(raw, "PNG", pnginfo=metadata)
    await manager.mutate("characters.update", {
        "id": role_id, "filename": "update.png", "data": base64.b64encode(raw.getvalue()).decode(),
    })
    assert CharacterProfile(directory / "card.json").card.name == "After"
    with Image.open(directory / "avatar.png") as avatar:
        assert avatar.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
        assert "chara" not in avatar.info
    assert not list(manager.root.glob(".import-*"))


def test_concurrent_startup_scan_never_exposes_a_partial_registry(manager, monkeypatch):
    ids = {manager.import_card({"data": base64.b64encode(b'{"name":"Alice"}').decode()})["id"]
           for _ in range(3)}
    fresh = CharacterManager(manager.settings)
    started = threading.Event()
    profile = CharacterProfile

    def slow_profile(path, *args):
        # The restore thread is still parsing cards when a request needs the registry.
        if not started.is_set():
            started.set()
            time.sleep(0.2)
        return profile(path, *args)

    monkeypatch.setattr("nanobot.roleplay.manager.CharacterProfile", slow_profile)
    restore = threading.Thread(target=fresh.entries)
    restore.start()
    assert started.wait(5)
    assert set(fresh.entries()) == ids
    restore.join()


@pytest.mark.parametrize("role_id", [None, "../escape", 12])
async def test_update_requires_an_existing_card(manager, role_id):
    with pytest.raises(ValueError):
        await manager.mutate("characters.update", {"id": role_id, "data": base64.b64encode(b'{"name":"Bad"}').decode()})
    assert not manager.entries()


async def test_update_rejects_agent_and_invalid_card_without_changing_files(manager):
    agent = manager.create_agent({"name": "Native"})
    with pytest.raises(ValueError):
        await manager.mutate("characters.update", {"id": agent["id"], "data": base64.b64encode(b'{"name":"Bad"}').decode()})
    role = manager.import_card({"data": base64.b64encode(b'{"name":"Good"}').decode()})
    path = manager.directory(role["id"]) / "card.json"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        await manager.mutate("characters.update", {"id": role["id"], "data": base64.b64encode(b'{"name":""}').decode()})
    assert path.read_bytes() == before


@pytest.fixture
def leases(monkeypatch):
    instances = []

    def make_lease(runtime, **_kwargs):
        lease = MagicMock(runtime=runtime)

        def start(_options):
            runtime.status.return_value = SimpleNamespace(running=True)
            return SimpleNamespace(status=runtime.status())

        lease.ensure_on_demand_gateway.side_effect = start
        instances.append(lease)
        return lease

    monkeypatch.setattr("nanobot.roleplay.manager.GatewayRuntime", lambda **_: MagicMock(
        status=MagicMock(return_value=SimpleNamespace(running=False)),
    ))
    monkeypatch.setattr("nanobot.roleplay.manager.GatewayClientLease", make_lease)
    writer = MagicMock(wait_closed=AsyncMock())
    monkeypatch.setattr("nanobot.roleplay.manager.asyncio.open_connection", AsyncMock(
        return_value=(None, writer),
    ))
    return instances


def add_role(manager, *, enabled=True, auto_start=True):
    result = manager.import_card({
        "data": base64.b64encode(b'{"name":"Alice"}').decode(),
    })
    role_id = result["id"]
    settings = WebUISettingsConfig(manager.directory(role_id) / "config.json")

    def configure(config):
        config.roleplay.auto_start = auto_start
        config.channels.qq = {
            "enabled": enabled, "appId": role_id, "secret": "${ROLE_QQ_SECRET}",
            "allowFrom": ["owner"],
        }

    settings.update(configure)
    return role_id, settings


def test_new_role_does_not_inherit_qq_account_or_shared_session(manager):
    def configure_parent(config):
        config.channels.qq = {
            "enabled": True, "appId": "parent", "secret": "private", "mediaDir": "/parent/media",
        }
        config.roleplay.auto_start = False
        config.agents.defaults.unified_session = True

    manager.settings.update(configure_parent)
    parent_before = manager.settings.path.read_bytes()
    role_id = manager.import_card({"data": base64.b64encode(b'{"name":"Bob"}').decode()})["id"]
    child = WebUISettingsConfig(manager.directory(role_id) / "config.json").load()
    assert child.channels.qq["enabled"] is False
    assert child.channels.qq["appId"] == child.channels.qq["secret"] == ""
    assert child.channels.qq["mediaDir"] == ""
    assert child.roleplay.auto_start is True
    assert child.agents.defaults.unified_session is False
    assert child.agents.defaults.session_ttl_minutes == 0
    assert manager.settings.path.read_bytes() == parent_before


async def test_restore_filters_roles_and_isolates_failures(manager, leases, monkeypatch):
    broken, broken_settings = add_role(manager)
    broken_settings.path.write_text("{broken")
    failed, _ = add_role(manager)
    enabled, enabled_settings = add_role(manager)
    disabled, _ = add_role(manager, enabled=False)
    paused, _ = add_role(manager, auto_start=False)
    original = manager.ensure_started

    async def start(role_id, **kwargs):
        if role_id == failed:
            raise OSError("startup failed")
        return await original(role_id, **kwargs)

    monkeypatch.setattr(manager, "ensure_started", start)
    manager.start()
    task = manager._restore_task
    manager.start()
    assert manager._restore_task is task
    await task
    assert manager.endpoint(enabled) is not None
    assert all(manager.endpoint(role_id) is None for role_id in (broken, failed, disabled, paused))
    assert len(leases) == 1
    assert enabled_settings.load().channels.qq["secret"] == "${ROLE_QQ_SECRET}"
    await manager.close()


async def test_manual_stop_survives_restart_and_open_resumes(manager, leases):
    role_id, settings = add_role(manager)
    before = settings.load().channels.qq
    endpoints = await asyncio.gather(manager.ensure_started(role_id), manager.ensure_started(role_id))
    assert endpoints[0] == endpoints[1] and endpoints[0] is not None
    assert len(leases) == 1
    await manager.mutate("characters.stop", {"id": role_id})
    assert settings.load().roleplay.auto_start is False
    leases[0].release.assert_called_once()
    await manager.close()

    restarted = CharacterManager(manager.settings)
    restarted.start()
    await restarted._restore_task
    assert restarted.endpoint(role_id) is None
    await restarted.mutate("characters.start", {"id": role_id})
    assert restarted.endpoint(role_id) is not None
    assert settings.load().roleplay.auto_start is True
    await restarted.close()
    assert settings.load().roleplay.auto_start is True

    restored = CharacterManager(manager.settings)
    restored.start()
    await restored._restore_task
    assert restored.endpoint(role_id) is not None
    assert settings.load().channels.qq == before
    await restored.close()
    assert all(lease.release.call_count == 1 for lease in leases)


async def test_queued_restore_cannot_override_manual_stop(manager, leases):
    role_id, settings = add_role(manager)
    # Both actions queue behind an in-flight operation; stop wins the lock first.
    await manager._lock.acquire()
    stopped = asyncio.create_task(manager.stop(role_id))
    restored = asyncio.create_task(manager.ensure_started(role_id, automatic=True))
    await asyncio.sleep(0)
    manager._lock.release()
    await stopped
    assert await restored is None
    assert settings.load().roleplay.auto_start is False
    assert not leases
    await manager.close()


async def test_shutdown_waits_for_current_start_and_releases_lease(manager, leases, monkeypatch):
    role_id, settings = add_role(manager)
    add_role(manager)
    waiting = asyncio.Event()
    ready = asyncio.Event()

    async def connect(*_args):
        waiting.set()
        await ready.wait()
        return None, MagicMock(wait_closed=AsyncMock())

    monkeypatch.setattr("nanobot.roleplay.manager.asyncio.open_connection", connect)
    manager.start()
    await asyncio.wait_for(waiting.wait(), timeout=5)
    closing = asyncio.create_task(manager.close())
    await asyncio.sleep(0)
    assert not closing.done()
    ready.set()
    await asyncio.wait_for(closing, timeout=5)
    assert len(leases) == 1
    leases[0].release.assert_called_once()
    assert not manager._endpoints
    assert settings.load().roleplay.auto_start is True
    with pytest.raises(ValueError, match="manager_shutting_down"):
        await manager.ensure_started(role_id)
    assert json.loads(settings.path.read_text())["roleplay"]["autoStart"] is True
