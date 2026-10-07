"""Parent-side routing checks of the character proxy, without starting gateways."""

from unittest.mock import MagicMock

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request

from nanobot.roleplay.proxy import CharacterProxy

ROLE = "0123456789abcdef0123456789abcdef"


@pytest.mark.asyncio
async def test_websocket_upgrade_uses_the_parent_allow_list():
    manager = MagicMock()
    proxy = CharacterProxy(manager, MagicMock())
    try:
        request = Request(f"/_characters/{ROLE}/?client_id=intruder&token=t",
                          Headers({"Upgrade": "websocket"}))
        response = await proxy.dispatch(MagicMock(), request, lambda client_id: client_id == "owner")
        assert response is not None and response.status_code == 403
        manager.endpoint.assert_not_called()
    finally:
        await proxy.client.aclose()
