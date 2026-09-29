# Build a QQ AI Agent with nanobot

This guide connects nanobot to QQ through the official `qq` channel. The
official channel uses the botpy SDK for private messages and group mentions.
For OneBot v11 workflows, use the Napcat section in the full chat-apps reference.

## What this guide builds

- a QQ bot application
- the `qq` channel enabled in nanobot
- one pairing-approved QQ private sender
- a running nanobot gateway

## Prerequisites

- A working local nanobot reply:

```bash
nanobot agent -m "Hello!"
```

- Access to the QQ Open Platform.
- A QQ account added to the bot sandbox for testing.

## Install nanobot

```bash
python -m pip install nanobot-ai
nanobot onboard --wizard
```

## Enable the QQ channel

Install the optional channel dependency:

```bash
nanobot plugins enable qq
```

In the QQ Open Platform, create a bot application and copy the AppID and
AppSecret. Add your QQ account to the sandbox test members, then merge this
snippet into `~/.nanobot/config.json`:

```json
{
  "channels": {
    "qq": {
      "enabled": true,
      "appId": "YOUR_APP_ID",
      "secret": "YOUR_APP_SECRET",
      "msgFormat": "markdown",
      "ackEnabled": false,
      "streaming": true
    }
  }
}
```

Omitting `allowFrom` enables pairing-only mode. A new private sender should get
a pairing code before normal agent access.

## Run nanobot gateway

```bash
nanobot channels status
nanobot gateway
```

## Test a message

Send the QQ bot a private message from a sandbox account. It should return a
pairing code. Approve it from a trusted local surface:

```bash
nanobot agent -m "/pairing approve ABCD-EFGH"
```

Send the message again after approval.

## Reply display

In **Chat Apps → QQ**, disable **Send acknowledgement** to suppress the
processing message without deleting its text. Progress messages and tool hints
default to off; reasoning is not sent to QQ.

Choose **Markdown** for formatted replies. Each character saves its own QQ
settings. QQ supports a [documented Markdown subset](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/type/markdown.html),
so rendering can differ from the web chat.

Streaming defaults to on for private messages and uses the official
[C2C stream API](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_stream_messages.post.html).
Group replies and disabled or unavailable streaming use a complete final
message. A failure after streaming has started can leave a partial stream
alongside the complete fallback reply.

## Security notes

- Prefer pairing-only mode for first setup. Add `allowFrom` only when you want a
  static allowlist.
- Keep sandbox testing separate from production publishing.
- Store QQ AppSecret through environment variables for deployed services.
- Use Napcat only when you intentionally need a QQ account bridge.

## Troubleshooting

- If private messages do not arrive, confirm the sender is in the QQ bot sandbox
  and the gateway is running.
- For formatted replies, select `msgFormat: "markdown"` and use QQ's supported
  Markdown syntax. Select `"plain"` only when plain text is desired.
- If a first private message returns a pairing code, approve it before testing
  normal replies.
- For group replies, confirm group permissions and mention the bot.

## Next: memory, automations, MCP tools

- [Chat Apps reference](../chat-apps.md)
- [Pairing](../configuration.md#pairing)
- [AI Agent Memory](./ai-agent-memory.md)
- [Configure MCP tools](./configure-mcp-tools.md)
