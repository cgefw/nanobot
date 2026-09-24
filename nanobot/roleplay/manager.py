"""Small single-user registry backed by the existing gateway process runtime."""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import socket
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.channels.contracts import channel_default_config, channel_instance_specs
from nanobot.channels.qq.manifest import PLUGIN as QQ_PLUGIN
from nanobot.config.loader import save_config
from nanobot.config.schema import Config
from nanobot.gateway.runtime import GatewayClientLease, GatewayInstance, GatewayRuntime
from nanobot.roleplay.agents import AgentProfile, agent_files
from nanobot.roleplay.cards import MAX_UPLOAD_BYTES, CharacterProfile, parse_card
from nanobot.webui.settings_services import WebUISettingsConfig

PROXY_HEADER = "X-Nanobot-Character-Proxy"
MAX_CHARACTERS = 100


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class CharacterManager:
    def __init__(self, settings: WebUISettingsConfig) -> None:
        self.settings = settings
        self.root = settings.path.parent / "characters"
        self._entries: dict[str, dict[str, str]] | None = None
        self._leases: dict[str, GatewayClientLease] = {}
        self._ports: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._restore_task: asyncio.Task[None] | None = None
        self._closing = False

    def start(self) -> None:
        """Restore once after the main listener is ready, without delaying the WebUI."""
        if self._restore_task is None and not self._closing:
            self._restore_task = asyncio.create_task(self._restore_enabled())

    async def _restore_enabled(self) -> None:
        for role_id in list(await asyncio.to_thread(self.entries)):
            if self._closing:
                break
            try:
                await self.ensure_started(role_id, automatic=True)
            except Exception as exc:
                # Validation errors may contain credential-bearing input values.
                logger.warning("Character {} could not be restored ({})", role_id, type(exc).__name__)

    def entries(self) -> dict[str, dict[str, str]]:
        if self._entries is None:
            self._entries = {}
            if self.root.exists():
                for path in sorted(self.root.glob("*/config.json")):
                    if len(self._entries) >= MAX_CHARACTERS:
                        break
                    try:
                        uuid.UUID(hex=path.parent.name)
                        card_path = path.with_name("card.json")
                        name = (CharacterProfile(card_path).card.name if card_path.exists()
                                else Config.model_validate_json(path.read_text()).roleplay.agent_name)
                        if name:
                            self._entries[path.parent.name] = {"id": path.parent.name, "name": name}
                    except (ValueError, OSError):
                        continue
        return self._entries

    def directory(self, role_id: str) -> Path:
        if role_id not in self.entries():
            raise ValueError("Character not found")
        return self.root / role_id

    def listing(self) -> dict[str, Any]:
        return {"characters": [
            {**entry, "running": role_id in self._ports}
            for role_id, entry in self.entries().items()
        ]}

    def port(self, role_id: str) -> int | None:
        return self._ports.get(role_id)

    def details(self, role_id: str) -> dict[str, Any]:
        directory = self.directory(role_id)
        if (directory / "card.json").exists():
            return CharacterProfile(directory / "card.json").preview()
        return AgentProfile(Config.model_validate_json((directory / "config.json").read_text())).preview()

    def create_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 256:
            raise ValueError("请填写 1–256 字的 Agent 名称")
        if len(self.entries()) >= MAX_CHARACTERS:
            raise ValueError("At most 100 characters are supported")
        files = AgentProfile.defaults(agent_files(payload.get("files", {})))
        role_id = uuid.uuid4().hex
        target, staging = self.root / role_id, self.root / f".import-{role_id}"
        child = self.child_config(self.settings.load(), target)
        child.agents.defaults.character_card = None
        child.roleplay.agent_name = name.strip()
        AgentProfile(child).validate(files)
        staging.mkdir(parents=True, mode=0o700)
        try:
            workspace = staging / "workspace"
            workspace.mkdir()
            for filename, content in files.items():
                (workspace / filename).write_text(content, encoding="utf-8")
            save_config(child, staging / "config.json")
            staging.rename(target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        entry = {"id": role_id, "name": child.roleplay.agent_name}
        self.entries()[role_id] = entry
        return entry

    def import_card(self, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = payload.get("data")
        filename = payload.get("filename", "card.json")
        if not isinstance(encoded, str) or len(encoded) > MAX_UPLOAD_BYTES * 4 // 3 + 4:
            raise ValueError("Character upload exceeds 8 MiB")
        if not isinstance(filename, str):
            raise ValueError("Invalid filename")
        imported = parse_card(base64.b64decode(encoded, validate=True), filename)
        if payload.get("preview", False):
            return imported.preview()
        if len(self.entries()) >= MAX_CHARACTERS:
            raise ValueError("At most 100 characters are supported")
        role_id = uuid.uuid4().hex
        target = self.root / role_id
        staging = self.root / f".import-{role_id}"
        staging.mkdir(parents=True, mode=0o700)
        try:
            (staging / "card.json").write_bytes(imported.source)
            if imported.avatar:
                (staging / "avatar.png").write_bytes(imported.avatar)
            # Validate the fixed definition before it can consume an entire model context.
            parent = self.settings.load()
            budget = max(512, parent.resolve_preset().context_window_tokens // 4)
            CharacterProfile(staging / "card.json", parent.roleplay.user_name).bounded_identity(budget)
            child = self.child_config(parent, target)
            save_config(child, staging / "config.json")
            staging.rename(target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self.entries()[role_id] = {"id": role_id, "name": imported.card.name}
        return {"id": role_id, "name": imported.card.name, "warnings": list(imported.warnings)}

    @staticmethod
    def child_config(parent: Config, directory: Path) -> Config:
        child = parent.model_copy(deep=True)
        child.agents.defaults.workspace = str(directory / "workspace")
        child.agents.defaults.character_card = str(directory / "card.json")
        child.agents.defaults.session_ttl_minutes = 0
        child.agents.defaults.unified_session = False
        child.roleplay.auto_start = True
        child.roleplay.agent_name = ""
        child.gateway.heartbeat.enabled = False
        for channel in (child.channels.model_extra or {}).values():
            if isinstance(channel, dict):
                channel["enabled"] = False
        from nanobot.channels.websocket.runtime import TrustedProxyAuthConfig, WebSocketConfig

        ws = WebSocketConfig.model_validate((parent.channels.model_extra or {}).get("websocket", {}))
        ws.enabled = True
        ws.host, ws.port, ws.path = "127.0.0.1", free_port(), "/"
        ws.unix_socket_path = ws.public_ws_url = ws.ssl_certfile = ws.ssl_keyfile = ""
        # The outer gateway verifies any original trusted-proxy assertion first.
        ws.trusted_proxy_auth = TrustedProxyAuthConfig(
            trusted_peer_cidrs=["127.0.0.1/32"], assertion_header=PROXY_HEADER,
        )
        child.channels = type(child.channels).model_validate({
            **child.channels.model_dump(), "websocket": ws.model_dump(by_alias=True),
            "qq": channel_default_config(QQ_PLUGIN),
        })
        child.gateway.host = "127.0.0.1"
        return child

    def _prepare_start(self, path: Path, automatic: bool) -> bool:
        child = Config.model_validate_json(path.read_text())
        if automatic:
            qq = (child.channels.model_extra or {}).get("qq", {})
            return child.roleplay.auto_start and bool(channel_instance_specs(QQ_PLUGIN, qq))
        if not child.roleplay.auto_start:
            child.roleplay.auto_start = True
            save_config(child, path)
        return True

    async def ensure_started(self, role_id: str, *, automatic: bool = False) -> int | None:
        async with self._lock:
            if self._closing:
                raise ValueError("Character manager is shutting down")
            directory = self.directory(role_id)
            settings = WebUISettingsConfig(directory / "config.json")
            # Check inside the same lock as stop so a queued restore cannot undo a manual stop.
            if not await asyncio.to_thread(
                settings.run_serialized, lambda path: self._prepare_start(path, automatic),
            ):
                return None
            if role_id in self._ports:
                lease = self._leases[role_id]
                status = await asyncio.to_thread(lease.runtime.status)
                if status.running:
                    return self._ports[role_id]
                await asyncio.to_thread(lease.release)
                self._leases.pop(role_id)
                self._ports.pop(role_id)
            instance = GatewayInstance.resolve(config_path=directory / "config.json")
            runtime = GatewayRuntime(paths=instance.paths)
            lease = GatewayClientLease(runtime, kind="character-manager")

            def start(_path: Path) -> int:
                # Refresh only listener credentials; per-character model/tool settings persist.
                parent = self.settings.load()
                raw = json.loads(instance.config_path.read_text())
                child = Config.model_validate(raw)
                from nanobot.channels.websocket.runtime import WebSocketConfig

                ws = WebSocketConfig.model_validate((child.channels.model_extra or {})["websocket"])
                lease.acquire()
                # A primary-gateway restart may reattach before the orphan monitor stops it.
                if runtime.status().running:
                    return ws.port
                parent_ws = WebSocketConfig.model_validate((parent.channels.model_extra or {}).get("websocket", {}))
                ws.token = parent_ws.token
                ws.token_issue_secret = parent_ws.token_issue_secret
                ws.websocket_requires_token = parent_ws.websocket_requires_token
                ws.allow_from = list(parent_ws.allow_from)
                ws.port = free_port()
                child.channels = type(child.channels).model_validate({
                    **child.channels.model_dump(), "websocket": ws.model_dump(by_alias=True),
                })
                save_config(child, instance.config_path)
                result = lease.ensure_on_demand_gateway(instance.start_options(port=free_port()))
                if not result.status.running:
                    lease.release()
                    raise ValueError(f"Character failed to start: {result.message}")
                return ws.port

            try:
                port = await asyncio.to_thread(settings.run_serialized, start)
            except BaseException:
                await asyncio.to_thread(lease.release)
                raise
            self._leases[role_id] = lease
            self._ports[role_id] = port
            try:
                async with asyncio.timeout(15):
                    while True:
                        try:
                            _, writer = await asyncio.open_connection("127.0.0.1", port)
                            writer.close()
                            await writer.wait_closed()
                            break
                        except OSError:
                            await asyncio.sleep(0.1)
            except BaseException:
                self._ports.pop(role_id, None)
                self._leases.pop(role_id, None)
                await asyncio.to_thread(lease.release)
                raise
            return port

    async def stop(self, role_id: str, *, persist: bool = True) -> None:
        async with self._lock:
            directory = self.directory(role_id)
            if persist:
                def pause(path: Path) -> None:
                    child = Config.model_validate_json(path.read_text())
                    child.roleplay.auto_start = False
                    save_config(child, path)

                await asyncio.to_thread(
                    WebUISettingsConfig(directory / "config.json").run_serialized, pause,
                )
            lease = self._leases.pop(role_id, None)
            self._ports.pop(role_id, None)
            if lease:
                await asyncio.to_thread(lease.release)

    async def close(self) -> None:
        self._closing = True
        if self._restore_task:
            # A cancelled to_thread startup can finish after its lease was released.
            # Let the current bounded startup finish before releasing children.
            await self._restore_task
        for role_id in list(self._leases):
            await self.stop(role_id, persist=False)

    async def mutate(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action in {"characters.import", "characters.create"}:
            async with self._lock:
                create = self.create_agent if action == "characters.create" else self.import_card
                return await asyncio.to_thread(create, payload)
        role_id = payload.get("id")
        if not isinstance(role_id, str):
            raise ValueError("Character id is required")
        if action == "characters.start":
            await self.ensure_started(role_id)
        elif action == "characters.stop":
            await self.stop(role_id)
        else:
            raise ValueError("Unknown character action")
        return self.listing()
