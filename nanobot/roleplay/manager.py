"""Small single-user registry backed by the existing gateway process runtime."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import shutil
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.channels.contracts import channel_default_config, channel_instance_specs
from nanobot.channels.qq.manifest import PLUGIN as QQ_PLUGIN
from nanobot.config.loader import save_config
from nanobot.config.schema import Config
from nanobot.gateway.runtime import GatewayClientLease, GatewayInstance, GatewayRuntime
from nanobot.roleplay.agents import AgentProfile, agent_files
from nanobot.roleplay.cards import MAX_UPLOAD_BYTES, CharacterProfile, parse_card
from nanobot.webui.settings_services import WebUISettingsConfig

if TYPE_CHECKING:
    from nanobot.channels.websocket.runtime import WebSocketConfig

MAX_CHARACTERS = 100


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def issue_listener_credentials(ws: WebSocketConfig) -> None:
    """Give a character listener fresh credentials that only the parent proxy holds.

    The parent applies its current bootstrap policy and allow-list before it
    forwards, so the child never stores the parent's token or secret and a
    parent restart cannot leave it with stale ones. Trusted-proxy auth checks
    only that its header is present, so the header name carries the secret.
    """
    from nanobot.channels.websocket.runtime import TrustedProxyAuthConfig

    ws.token = ""
    ws.token_issue_secret = secrets.token_urlsafe(32)
    ws.websocket_requires_token = True
    ws.allow_from = ["*"]
    ws.trusted_proxy_auth = TrustedProxyAuthConfig(
        trusted_peer_cidrs=["127.0.0.1/32"],
        assertion_header=f"X-Nanobot-Character-{secrets.token_hex(16)}",
    )


@dataclass(frozen=True)
class CharacterEndpoint:
    """Loopback listener of a running character and the credentials its proxy presents."""

    port: int
    assertion_header: str
    bootstrap_secret: str

    @classmethod
    def of(cls, ws: WebSocketConfig) -> CharacterEndpoint:
        if ws.trusted_proxy_auth is None:
            raise ValueError("Character gateway is running without proxy credentials; stop and reopen it")
        return cls(ws.port, ws.trusted_proxy_auth.assertion_header, ws.token_issue_secret or ws.token)


class CharacterManager:
    def __init__(self, settings: WebUISettingsConfig) -> None:
        self.settings = settings
        self.root = settings.path.parent / "characters"
        self._entries: dict[str, dict[str, str]] | None = None
        self._leases: dict[str, GatewayClientLease] = {}
        self._endpoints: dict[str, CharacterEndpoint] = {}
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
            {**entry, "running": role_id in self._endpoints}
            for role_id, entry in self.entries().items()
        ]}

    def endpoint(self, role_id: str) -> CharacterEndpoint | None:
        return self._endpoints.get(role_id)

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
        existing_id = payload.get("id")
        if existing_id is not None and not isinstance(existing_id, str):
            raise ValueError("Invalid character id")
        target = self.directory(existing_id) if isinstance(existing_id, str) else None
        if target is not None and (
            target.is_symlink() or target.resolve().parent != self.root.resolve()
            or not (target / "card.json").is_file()
            or any((target / name).is_symlink() for name in ("card.json", "avatar.png", "config.json"))
        ):
            raise ValueError("Only an existing character card in this instance can be updated")
        if payload.get("preview", False):
            return imported.preview()
        if target is None and len(self.entries()) >= MAX_CHARACTERS:
            raise ValueError("At most 100 characters are supported")
        role_id = existing_id if isinstance(existing_id, str) else uuid.uuid4().hex
        updating = target is not None
        target = target or self.root / role_id
        staging = self.root / f".import-{uuid.uuid4().hex}"
        staging.mkdir(parents=True, mode=0o700)
        try:
            (staging / "card.json").write_bytes(imported.source)
            if imported.avatar:
                (staging / "avatar.png").write_bytes(imported.avatar)
            # Validate the fixed definition before it can consume an entire model context.
            parent = (WebUISettingsConfig(target / "config.json").load()
                      if updating else self.settings.load())
            budget = max(512, parent.resolve_preset().context_window_tokens // 4)
            CharacterProfile(staging / "card.json", parent.roleplay.user_name).bounded_identity(budget)
            if updating:
                # The running profile reloads card.json by mtime. Leave config,
                # workspace, sessions, and memory intact; JSON updates keep the avatar.
                if imported.avatar:
                    (staging / "avatar.png").replace(target / "avatar.png")
                (staging / "card.json").replace(target / "card.json")
                staging.rmdir()
            else:
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
        from nanobot.channels.websocket.runtime import WebSocketConfig

        ws = WebSocketConfig.model_validate((parent.channels.model_extra or {}).get("websocket", {}))
        ws.enabled = True
        ws.host, ws.port, ws.path = "127.0.0.1", free_port(), "/"
        ws.unix_socket_path = ws.public_ws_url = ws.ssl_certfile = ws.ssl_keyfile = ""
        issue_listener_credentials(ws)
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

    async def ensure_started(
        self, role_id: str, *, automatic: bool = False,
    ) -> CharacterEndpoint | None:
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
            if role_id in self._endpoints:
                lease = self._leases[role_id]
                status = await asyncio.to_thread(lease.runtime.status)
                if status.running:
                    return self._endpoints[role_id]
                await asyncio.to_thread(lease.release)
                self._leases.pop(role_id)
                self._endpoints.pop(role_id)
            instance = GatewayInstance.resolve(config_path=directory / "config.json")
            runtime = GatewayRuntime(paths=instance.paths)
            lease = GatewayClientLease(runtime, kind="character-manager")

            def start(_path: Path) -> CharacterEndpoint:
                # Refresh only listener credentials; per-character model/tool settings persist.
                raw = json.loads(instance.config_path.read_text())
                child = Config.model_validate(raw)
                from nanobot.channels.websocket.runtime import WebSocketConfig

                ws = WebSocketConfig.model_validate((child.channels.model_extra or {})["websocket"])
                lease.acquire()
                # A primary-gateway restart may reattach before the orphan monitor stops it.
                if runtime.status().running:
                    return CharacterEndpoint.of(ws)
                issue_listener_credentials(ws)
                ws.port = free_port()
                child.channels = type(child.channels).model_validate({
                    **child.channels.model_dump(), "websocket": ws.model_dump(by_alias=True),
                })
                save_config(child, instance.config_path)
                result = lease.ensure_on_demand_gateway(instance.start_options(port=free_port()))
                if not result.status.running:
                    lease.release()
                    raise ValueError(f"Character failed to start: {result.message}")
                return CharacterEndpoint.of(ws)

            try:
                endpoint = await asyncio.to_thread(settings.run_serialized, start)
            except BaseException:
                await asyncio.to_thread(lease.release)
                raise
            self._leases[role_id] = lease
            self._endpoints[role_id] = endpoint
            try:
                async with asyncio.timeout(15):
                    while True:
                        try:
                            _, writer = await asyncio.open_connection("127.0.0.1", endpoint.port)
                            writer.close()
                            await writer.wait_closed()
                            break
                        except OSError:
                            await asyncio.sleep(0.1)
            except BaseException:
                self._endpoints.pop(role_id, None)
                self._leases.pop(role_id, None)
                await asyncio.to_thread(lease.release)
                raise
            return endpoint

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
            self._endpoints.pop(role_id, None)
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

    async def delete(self, role_id: str) -> None:
        async with self._lock:
            directory = self.directory(role_id)
            if directory.is_symlink() or directory.resolve().parent != self.root.resolve():
                raise ValueError("只能删除当前实例目录中的角色")
            config_path = directory / "config.json"
            if config_path.is_symlink():
                raise ValueError("不能删除配置文件指向其他位置的角色")
            lease = self._leases.get(role_id)
            instance = GatewayInstance.resolve(config_path=config_path)
            runtime = lease.runtime if lease else GatewayRuntime(paths=instance.paths)
            # Releasing our lease alone can leave a process held by another client alive.
            result = await asyncio.to_thread(runtime.stop)
            if result.status.running:
                raise ValueError("角色进程未能停止，未删除数据，请稍后重试")
            if lease:
                await asyncio.to_thread(lease.release)
            self._leases.pop(role_id, None)
            self._endpoints.pop(role_id, None)
            # Only remove this instance directory; do not follow configured workspace paths.
            if directory.exists():
                await asyncio.to_thread(shutil.rmtree, directory)
            self.entries().pop(role_id, None)

    async def mutate(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action in {"characters.import", "characters.create", "characters.update"}:
            if action == "characters.update" and not isinstance(payload.get("id"), str):
                raise ValueError("Character id is required")
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
        elif action == "characters.delete":
            await self.delete(role_id)
        else:
            raise ValueError("Unknown character action")
        return self.listing()
