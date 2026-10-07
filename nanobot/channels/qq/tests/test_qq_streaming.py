"""QQ wire payloads, quiet defaults, and safe streaming fallback (no live account)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.qq.manifest import PLUGIN
from nanobot.channels.qq.runtime import QQChannel, QQConfig
from nanobot.config.schema import Config
from nanobot.webui.settings_system import save_channel_config_values


@pytest.fixture
def channel():
    ch = QQChannel(QQConfig(msg_format="markdown", allow_from=["*"]), MessageBus())
    ch._client = SimpleNamespace(api=SimpleNamespace(
        _http=SimpleNamespace(request=AsyncMock(return_value={"id": "remote"})),
        post_c2c_message=AsyncMock(), post_group_message=AsyncMock(),
    ), close=AsyncMock())
    return ch


def test_webui_saves_qq_behavior_options():
    config = Config()
    values = {"ackEnabled": False, "ackMessage": "Custom ack", "streaming": True,
              "msgFormat": "markdown", "sendToolHints": False, "sendProgress": False}
    save_channel_config_values(config, "qq", values, load_channel_plugin=lambda _: PLUGIN)
    saved = QQConfig.model_validate(config.channels.qq)
    assert saved.msg_format == "markdown"
    assert saved.ack_enabled is False
    assert saved.ack_message == "Custom ack"
    assert saved.streaming is True
    assert saved.send_tool_hints is saved.send_progress is False


async def test_markdown_reaches_the_real_sdk_http_payload(channel):
    from botpy.api import BotAPI

    request = AsyncMock(return_value={"id": "message"})
    channel._client.api = BotAPI(SimpleNamespace(request=request))
    await channel.send(OutboundMessage(channel="qq", chat_id="u", content="# Heading\n\n**bold**"))
    wire = request.call_args.kwargs["json"]
    assert wire["msg_type"] == 2
    assert wire["markdown"] == {"content": "# Heading\n\n**bold**"}
    assert not wire["content"]


async def test_ack_switch_keeps_custom_text_and_processing(channel):
    channel.config.ack_enabled = False
    channel.config.ack_message = "Please wait"
    await channel._on_message(SimpleNamespace(
        id="m1", content="Hello", author=SimpleNamespace(user_openid="u1"),
    ), is_group=False)
    channel._client.api.post_c2c_message.assert_not_awaited()
    assert channel.config.ack_message == "Please wait"
    assert (await channel.bus.consume_inbound()).content == "Hello"


@pytest.mark.parametrize("enabled", [True, False])
async def test_streaming_toggle_controls_agent_stream_request(channel, enabled):
    channel.config.ack_enabled = False
    channel.config.streaming = enabled
    await channel._on_message(SimpleNamespace(
        id="m1", content="Hello", author=SimpleNamespace(user_openid="u1"),
    ), is_group=False)
    message = await channel.bus.consume_inbound()
    assert message.metadata.get("_wants_stream", False) is enabled
    assert message.metadata["message_id"] == "m1"


async def test_quiet_defaults_do_not_hide_the_final_answer(channel):
    # ChannelManager applies these defaults unless sendProgress / sendToolHints are set.
    assert channel.progress_transport_defaults() == (False, False)
    await channel.send(OutboundMessage(channel="qq", chat_id="u", content="**Answer**\n\n- item"))
    payload = channel._client.api.post_c2c_message.call_args.kwargs
    assert payload["msg_type"] == 2
    assert payload["markdown"] == {"content": "**Answer**\n\n- item"}
    assert "content" not in payload


async def test_stream_replaces_growing_text_and_finalizes_once(channel, monkeypatch):
    monkeypatch.setattr("nanobot.channels.qq.runtime.time.monotonic", lambda: 10)
    metadata = {"message_id": "m"}
    await channel.send_delta("u", "**Hello", metadata, stream_id="s")
    await channel.send_delta("u", " world", metadata, stream_id="s")
    assert channel._client.api._http.request.await_count == 1
    await channel.send_delta("u", "**", metadata, stream_id="s", stream_end=True)
    calls = channel._client.api._http.request.call_args_list
    assert calls[0].args[0].url.endswith("/v2/users/u/stream_messages")
    first, last = [call.kwargs["json"] for call in calls]
    assert first == {"input_mode": "replace", "input_state": 1, "index": 0, "msg_seq": 2,
                     "msg_id": "m", "content_type": "markdown", "content_raw": "**Hello"}
    assert last == {**first, "input_state": 10, "index": 1,
                    "stream_msg_id": "remote", "content_raw": "**Hello world**"}
    assert not channel._streams
    channel._client.api.post_c2c_message.assert_not_awaited()


async def test_stream_end_without_new_text_closes_with_the_full_answer(channel):
    metadata = {"message_id": "m"}
    await channel.send_delta("u", "Done.", metadata, stream_id="s")
    await channel.send_delta("u", "", metadata, stream_id="s", stream_end=True)
    first, last = [call.kwargs["json"] for call in channel._client.api._http.request.call_args_list]
    assert first["input_state"] == 1 and last["input_state"] == 10
    assert first["content_raw"] == last["content_raw"] == "Done."
    channel._client.api.post_c2c_message.assert_not_awaited()


async def test_streams_are_isolated_and_merge_boundaries_stay_open(channel):
    for stream_id in ("a", "b"):
        await channel.send_delta("u", stream_id, {"message_id": stream_id}, stream_id=stream_id)
    await channel.send_delta("u", "", {"message_id": "a"}, stream_id="a", stream_end=True, merge_next=True)
    assert len(channel._streams) == 2
    await channel.send_delta("u", "", {"message_id": "a"}, stream_id="a", stream_end=True)
    assert set(channel._streams) == {("u", "b")}
    await channel.stop()
    assert not channel._streams


@pytest.mark.parametrize("group", [False, True])
async def test_unsupported_stream_and_groups_fall_back_to_one_complete_answer(channel, group):
    if group:
        channel._chat_type_cache["u"] = "group"
    channel._client.api._http.request.side_effect = RuntimeError("unsupported")
    await channel.send_delta("u", "Hello", {"message_id": "m"})
    await channel.send_delta("u", " world", {"message_id": "m"}, stream_end=True)
    sender = channel._client.api.post_group_message if group else channel._client.api.post_c2c_message
    sender.assert_awaited_once()
    assert sender.call_args.kwargs["markdown"]["content"] == "Hello world"
    assert not channel._streams
    if group:
        channel._client.api._http.request.assert_not_awaited()


async def test_final_fallback_retry_does_not_append_delta_twice(channel):
    channel._client.api._http.request.side_effect = RuntimeError("unsupported")
    sender = channel._client.api.post_c2c_message
    sender.side_effect = [OSError("offline"), None]
    await channel.send_delta("u", "Hello", {"message_id": "m"})
    with pytest.raises(OSError):
        await channel.send_delta("u", " world", {"message_id": "m"}, stream_end=True)
    await channel.send_delta("u", " world", {"message_id": "m"}, stream_end=True)
    assert sender.call_args.kwargs["markdown"]["content"] == "Hello world"
    assert not channel._streams


async def test_plain_format_is_preserved_in_native_stream(channel):
    channel.config.msg_format = "plain"
    await channel.send_delta("u", "Plain answer", {"message_id": "m"}, stream_end=True)
    assert channel._client.api._http.request.call_args.kwargs["json"]["content_type"] == "text"
