"""Character WebSocket relay over real loopback sockets, without starting gateways."""

import asyncio
from collections.abc import Awaitable, Callable
from unittest.mock import MagicMock

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from nanobot.roleplay.proxy import CharacterProxy


async def _through_proxy(
    child: Callable[[ServerConnection], Awaitable[None]],
    browser: Callable[[ClientConnection], Awaitable[None]],
) -> None:
    proxy = CharacterProxy(MagicMock(), MagicMock())

    async def parent(connection: ServerConnection) -> None:
        proxy.sockets[connection] = await connect(f"ws://127.0.0.1:{child_port}/", proxy=None)
        await proxy.relay(connection)

    try:
        async with serve(child, "127.0.0.1", 0) as child_server:
            child_port = child_server.sockets[0].getsockname()[1]
            async with serve(parent, "127.0.0.1", 0) as parent_server:
                port = parent_server.sockets[0].getsockname()[1]
                async with connect(f"ws://127.0.0.1:{port}/", proxy=None) as connection:
                    await asyncio.wait_for(browser(connection), timeout=5)
    finally:
        await proxy.client.aclose()


async def _closed_by_child(connection: ClientConnection) -> None:
    with pytest.raises(ConnectionClosed):
        await connection.recv()


@pytest.mark.asyncio
async def test_child_close_reason_reaches_the_browser():
    async def child(connection: ServerConnection) -> None:
        await connection.close(4401, "token expired")

    async def browser(connection: ClientConnection) -> None:
        await _closed_by_child(connection)
        assert (connection.close_code, connection.close_reason) == (4401, "token expired")

    await _through_proxy(child, browser)


@pytest.mark.asyncio
async def test_lost_child_is_reported_as_an_internal_error():
    async def child(connection: ServerConnection) -> None:
        connection.transport.abort()

    async def browser(connection: ClientConnection) -> None:
        await _closed_by_child(connection)
        assert connection.close_code == 1011

    await _through_proxy(child, browser)


@pytest.mark.asyncio
async def test_browser_close_reason_reaches_the_child():
    seen: asyncio.Future[tuple[int | None, str | None]] = asyncio.get_running_loop().create_future()

    async def child(connection: ServerConnection) -> None:
        await connection.wait_closed()
        seen.set_result((connection.close_code, connection.close_reason))

    async def browser(connection: ClientConnection) -> None:
        await connection.close(4000, "switching character")
        assert await seen == (4000, "switching character")

    await _through_proxy(child, browser)
