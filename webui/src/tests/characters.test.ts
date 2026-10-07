import { describe, expect, it, afterEach } from "vitest";
import {
  characterId, characterStorageKey, characterUrl, charactersAvailable, rememberCharacterSupport,
} from "@/lib/characters";
import { toMediaAttachment } from "@/lib/media";

afterEach(() => window.history.replaceState({}, "", "/"));

describe("character isolation", () => {
  const id = "0123456789abcdef0123456789abcdef";
  it("scopes API, bootstrap, media, and stored state to the selected character", () => {
    window.history.replaceState({}, "", `/?character=${id}`);
    expect(characterId()).toBe(id);
    expect(characterUrl("/api/sessions")).toBe(`/_characters/${id}/api/sessions`);
    expect(characterUrl("/webui/bootstrap")).toBe(`/_characters/${id}/webui/bootstrap`);
    expect(toMediaAttachment({url: "/api/media/signed/image", kind: "image"}).url)
      .toBe(`/_characters/${id}/api/media/signed/image`);
    expect(characterStorageKey("queue")).toBe(`queue.character.${id}`);
    expect(characterUrl("https://example.com/api/sessions")).toBe("https://example.com/api/sessions");
    expect(characterUrl(`/_characters/${id}/api/sessions`)).toBe(`/_characters/${id}/api/sessions`);
  });
  it("offers character controls only to hosts that declare them", () => {
    rememberCharacterSupport({ ws_path: "/", terminal: { webui: { capabilities: ["webui.core.v1", "webui.characters.v1"] } } });
    expect(charactersAvailable()).toBe(true);
    // Remote hosts behind the local-client proxy report no WebUI capabilities.
    rememberCharacterSupport({ ws_path: "/", terminal: {} });
    expect(charactersAvailable()).toBe(false);
    rememberCharacterSupport({ ws_path: "/", terminal: { webui: { capabilities: ["webui.core.v1"] } } });
    expect(charactersAvailable()).toBe(false);
  });
  it("rejects unregistered path syntax in the query and preserves the default assistant", () => {
    window.history.replaceState({}, "", "/?character=../config");
    expect(characterId()).toBe("");
    expect(characterUrl("/api/sessions")).toBe("/api/sessions");
    expect(characterStorageKey("queue")).toBe("queue");
  });
});
