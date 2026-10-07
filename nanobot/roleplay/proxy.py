"""Same-origin forwarding; child gateways retain authorization and workspace scoping."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.http11 import Request, Response

from nanobot.roleplay.manager import CharacterManager
from nanobot.webui.http_utils import (
    http_error,
    http_json_response,
    http_response,
    is_trusted_proxy_authenticated_request,
    parse_request_path,
    query_first,
)

if TYPE_CHECKING:
    from nanobot.webui.ws_http import GatewayHTTPHandler

_ROUTE = re.compile(r"^/_characters/([a-f0-9]{32})(/.*)$")
_HOP_HEADERS = {"connection", "upgrade", "host", "content-length", "transfer-encoding",
                "accept-encoding", "content-encoding", "proxy-authorization"}


class CharacterProxy:
    def __init__(self, manager: CharacterManager, http: GatewayHTTPHandler) -> None:
        self.manager = manager
        self.http = http
        self.client = httpx.AsyncClient(trust_env=False, timeout=20)
        self.sockets: dict[ServerConnection, ClientConnection] = {}

    async def dispatch(
        self, connection: ServerConnection, request: Request, is_allowed: Callable[[str], bool],
    ) -> Response | None:
        match = _ROUTE.fullmatch(request.path)
        if not match:
            return http_error(404, "Character route not found")
        role_id, path = match.groups()
        upgrade = "websocket" in request.headers.get("Upgrade", "").lower()
        bootstrap = path.split("?", 1)[0] == "/webui/bootstrap"
        if upgrade:
            # Characters accept any client id; the parent's current allow-list applies here.
            client_id = query_first(parse_request_path(path)[1], "client_id") or ""
            if not is_allowed(client_id[:128]):
                return http_error(403, "Forbidden")
        try:
            self.manager.directory(role_id)
            if bootstrap:
                auth = self.http.character_bootstrap_auth(connection, request)
                if auth.status_code != 200:
                    return auth
                endpoint = await self.manager.ensure_started(role_id)
            else:
                endpoint = self.manager.endpoint(role_id)
            if endpoint is None:
                return http_error(409, "Character is stopped; open it again from Characters")
            assertion = endpoint.assertion_header.lower()
            headers = {
                key.lower(): value for key, value in request.headers.raw_items()
                if key.lower() not in _HOP_HEADERS
                and not key.lower().startswith("sec-websocket-")
                and key.lower() != assertion
            }
            # Append the actual peer; a forged loopback header cannot grant full access.
            peer = connection.remote_address
            peer_ip = str(peer[0]) if isinstance(peer, tuple) and isinstance(peer[0], str) else "127.0.0.1"
            prior = request.headers.get("X-Forwarded-For", "")
            headers["x-forwarded-for"] = f"{prior}, {peer_ip}" if prior else peer_ip
            headers["x-forwarded-host"] = request.headers.get("Host", "")
            if is_trusted_proxy_authenticated_request(connection, request.headers, self.http.config):
                headers[assertion] = "authenticated"
            if bootstrap:
                # The parent authorized this bootstrap above; vouch for it with the child's secret.
                headers.pop("x-nanobot-auth", None)
                headers["authorization"] = f"Bearer {endpoint.bootstrap_secret}"
            upstream = f"127.0.0.1:{endpoint.port}"
            if upgrade:
                socket = await connect(
                    f"ws://{upstream}{path}", additional_headers=headers, proxy=None,
                    max_size=self.http.config.max_message_bytes,
                )
                self.sockets[connection] = socket
                return None
            headers["Host"] = request.headers.get("Host", upstream)
            response = await self.client.get(f"http://{upstream}{path}", headers=headers)
            if bootstrap and response.status_code == 200:
                payload = response.json()
                prefix = f"/_characters/{role_id}"
                public = urlsplit(self.http.character_public_ws_url(request))
                payload["ws_path"] = prefix + "/"
                payload["ws_url"] = urlunsplit((public.scheme, public.netloc, prefix + "/", "", ""))
                return http_json_response(payload, extra_headers=[("Cache-Control", "no-store")])
            return http_response(
                response.content, status=response.status_code,
                content_type=response.headers.get("Content-Type", "application/octet-stream"),
                extra_headers=[(k, v) for k, v in response.headers.items()
                               if k.lower() not in _HOP_HEADERS | {"content-type"}],
            )
        except InvalidStatus as exc:
            return http_error(exc.response.status_code, "Character connection unauthorized")
        except ValueError as exc:
            return http_error(400, str(exc))
        except (httpx.HTTPError, OSError, TimeoutError):
            return http_error(503, "Character gateway unavailable; stop and reopen it")

    async def relay(self, connection: ServerConnection) -> bool:
        upstream = self.sockets.pop(connection, None)
        if upstream is None:
            return False

        async def forward(source: ServerConnection | ClientConnection,
                          target: ServerConnection | ClientConnection) -> None:
            try:
                async for message in source:
                    await target.send(message)
            except ConnectionClosed:
                pass

        tasks = [asyncio.create_task(forward(connection, upstream)),
                 asyncio.create_task(forward(upstream, connection))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await upstream.close()
            await connection.close()
        return True

    async def close(self) -> None:
        for socket in self.sockets.values():
            await socket.close()
        self.sockets.clear()
        await self.client.aclose()
        await self.manager.close()
