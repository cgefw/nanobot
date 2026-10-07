import { afterEach, describe, expect, it, vi } from "vitest";
import { listCharacters } from "@/components/Characters";

afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState({}, "", "/"); });

describe("character list on a character page", () => {
  const role = { id: "a".repeat(32), name: "Alice", running: true };

  it("reuses the parent API token until the parent rejects it", async () => {
    window.history.replaceState({}, "", `/?character=${role.id}`);
    let token = "first";
    let revoked = false;
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/webui/bootstrap")) {
        return { ok: true, json: async () => ({ ws_path: "/", api_token: token, expires_in: 300 }) };
      }
      const auth = (init?.headers as Record<string, string>).Authorization;
      if (revoked && auth === "Bearer first") return { ok: false, status: 401, json: async () => ({}) };
      return { ok: true, json: async () => ({ characters: [role] }) };
    });
    vi.stubGlobal("fetch", fetchMock);
    const bootstraps = () => fetchMock.mock.calls.filter(([url]) => url.endsWith("/webui/bootstrap")).length;

    await listCharacters("child");
    await expect(listCharacters("child")).resolves.toEqual([role]);
    expect(bootstraps()).toBe(1);

    // The parent restarted and no longer knows the cached token.
    revoked = true;
    token = "second";
    await expect(listCharacters("child")).resolves.toEqual([role]);
    expect(bootstraps()).toBe(2);
    expect((fetchMock.mock.calls.at(-1)?.[1]?.headers as Record<string, string>).Authorization)
      .toBe("Bearer second");
  });
});
