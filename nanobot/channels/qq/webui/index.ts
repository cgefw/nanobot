import type { ChannelUiContribution } from "@/channel-plugins/types";
import { chatAppGuideUrl } from "@/components/settings/channels/catalog";

export default {
  presentation: {
    logoUrl: "https://static-res.qq.com/static-res/imqq/qq-logo.png",
    logoFallbackUrl: "https://im.qq.com/favicon.ico",
    displayName: "QQ",
    initials: "QQ",
    color: "#12B7F5",
    setup: {
      mode: "credentials",
      docsUrl: chatAppGuideUrl("qq"),
      fields: [
        { key: "channels.qq.appId", section: "credentials" },
        { key: "channels.qq.secret", section: "credentials" },
        { key: "channels.qq.allowFrom", section: "access" },
        { key: "channels.qq.msgFormat", section: "behavior" },
        { key: "channels.qq.ackEnabled", section: "behavior" },
        { key: "channels.qq.ackMessage", section: "behavior" },
        { key: "channels.qq.streaming", section: "behavior" },
        { key: "channels.qq.sendProgress", section: "behavior" },
        { key: "channels.qq.sendToolHints", section: "behavior" },
      ],
    },
  },
} satisfies ChannelUiContribution;
