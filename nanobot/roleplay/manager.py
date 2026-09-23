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

from nanobot.config.loader import save_config
from nanobot.config.schema import Config
from nanobot.gateway.runtime import GatewayClientLease, GatewayInstance, GatewayRuntime
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

    def entries(self) -> dict[str, dict[str, str]]:
        if self._entries is None:
            self._entries = {}
            if self.root.exists():
                for path in sorted(self.root.glob("*/card.json")):
                    if len(self._entries) >= MAX_CHARACTERS:
                        break
                    try:
                        uuid.UUID(hex=path.parent.name)
                        card = CharacterProfile(path).card
                        self._entries[path.parent.name] = {"id": path.parent.name, "name": card.name}
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
        return CharacterProfile(directory / "card.json").preview()

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
        })
        child.gateway.host = "127.0.0.1"
        return child

    async def ensure_started(self, role_id: str) -> int:
        async with self._lock:
            directory = self.directory(role_id)
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

            def start() -> int:
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
                port = await asyncio.to_thread(start)
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

    async def stop(self, role_id: str) -> None:
        async with self._lock:
            self.directory(role_id)
            lease = self._leases.pop(role_id, None)
            self._ports.pop(role_id, None)
            if lease:
                await asyncio.to_thread(lease.release)

    async def close(self) -> None:
        for role_id in list(self._leases):
            await self.stop(role_id)

    async def mutate(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "characters.import":
            async with self._lock:
                return await asyncio.to_thread(self.import_card, payload)
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
