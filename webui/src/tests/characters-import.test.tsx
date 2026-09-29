import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CharacterSidebar, listCharacters } from "@/components/Characters";
import i18n from "@/i18n";

const { requestMutation, getToken, switchCharacter } = vi.hoisted(() => ({
  requestMutation: vi.fn(), getToken: () => "test-token", switchCharacter: vi.fn(),
}));
vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ client: { requestMutation }, getToken }),
}));
vi.mock("@/lib/characters", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/characters")>(), switchCharacter,
}));

const preview = { card: { name: "Dropped", description: "Test character" } };
const cardFile = () => new File(['{"name":"Dropped"}'], "role.json", { type: "application/json" });

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN");
  switchCharacter.mockReset();
  requestMutation.mockReset().mockResolvedValue(preview);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ characters: [] }) }));
  window.history.replaceState({}, "", "/#/new?importCharacter=1");
});
afterEach(() => {
  cleanup(); vi.unstubAllGlobals(); window.history.replaceState({}, "", "/");
});

function openImport() {
  render(<CharacterSidebar />);
  return screen.getByLabelText("导入角色卡", { exact: true, selector: "input" }).closest("label")!;
}

describe("character card drop import", () => {
  it("previews an update before replacing the existing role and returning to it", async () => {
    const id = "a".repeat(32);
    window.history.replaceState({}, "", `/#/new?updateCharacter=${id}`);
    const zone = openImport();
    expect(screen.getByRole("heading", { name: "更新角色卡" })).toBeInTheDocument();
    expect(screen.getByText(/保留聊天、记忆和渠道配置/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "创建 Agent" })).not.toBeInTheDocument();
    fireEvent.drop(zone, { dataTransfer: { files: [cardFile()] } });
    const confirm = await screen.findByRole("button", { name: "确认更新" });
    expect(requestMutation).toHaveBeenCalledWith("characters.update", {
      id, filename: "role.json", data: btoa('{"name":"Dropped"}'), preview: true,
    });
    expect(switchCharacter).not.toHaveBeenCalled();
    fireEvent.click(confirm);
    await waitFor(() => expect(switchCharacter).toHaveBeenCalledWith(id));
    expect(requestMutation).toHaveBeenLastCalledWith("characters.update", {
      id, filename: "role.json", data: btoa('{"name":"Dropped"}'),
    });
  });

  it("localizes the update page and keeps a failed update open for retry", async () => {
    await i18n.changeLanguage("en");
    window.history.replaceState({}, "", `/#/new?updateCharacter=${"b".repeat(32)}`);
    render(<CharacterSidebar />);
    expect(screen.getByRole("heading", { name: "Update character card" })).toBeInTheDocument();
    const input = screen.getByLabelText("Import character card", { selector: "input" });
    fireEvent.change(input, { target: { files: [cardFile()] } });
    const confirm = await screen.findByRole("button", { name: "Confirm update" });
    requestMutation.mockRejectedValueOnce(new Error("Save failed"));
    fireEvent.click(confirm);
    expect(await screen.findByRole("alert")).toHaveTextContent("Save failed");
    expect(switchCharacter).not.toHaveBeenCalled();
    expect(confirm).toBeEnabled();
  });

  it("previews a dropped file and waits for confirmation before importing", async () => {
    const zone = openImport();
    const transfer = { types: ["Files"], files: [cardFile()], dropEffect: "none" };
    expect(fireEvent.dragOver(zone, { dataTransfer: transfer })).toBe(false);
    expect(zone).toHaveClass("border-ring");
    expect(fireEvent.drop(zone, { dataTransfer: transfer })).toBe(false);
    const confirm = await screen.findByRole("button", { name: "确认导入" });
    expect(zone).not.toHaveClass("border-ring");
    expect(requestMutation).toHaveBeenCalledOnce();
    expect(requestMutation).toHaveBeenCalledWith("characters.import", {
      filename: "role.json", data: btoa('{"name":"Dropped"}'), preview: true,
    });
    fireEvent.click(confirm);
    await waitFor(() => expect(requestMutation).toHaveBeenCalledTimes(2));
    expect(requestMutation).toHaveBeenLastCalledWith("characters.import", {
      filename: "role.json", data: btoa('{"name":"Dropped"}'),
    });
  });

  it.each([
    [[new File(["text"], "role.txt")], "请选择 JSON 或 PNG 角色卡"],
    [[cardFile(), cardFile()], "每次只能导入一张角色卡"],
  ])("rejects invalid drops without starting an import", async (files, message) => {
    const zone = openImport();
    fireEvent.drop(zone, { dataTransfer: { files } });
    expect(await screen.findByRole("alert")).toHaveTextContent(message as string);
    expect(requestMutation).not.toHaveBeenCalled();
  });

  it("ignores further drops while the first preview is pending", async () => {
    let resolvePreview!: (value: typeof preview) => void;
    requestMutation.mockReturnValueOnce(new Promise((resolve) => { resolvePreview = resolve; }));
    const zone = openImport();
    fireEvent.drop(zone, { dataTransfer: { files: [cardFile()] } });
    await waitFor(() => expect(requestMutation).toHaveBeenCalledOnce());
    fireEvent.drop(zone, { dataTransfer: { files: [cardFile()] } });
    expect(requestMutation).toHaveBeenCalledOnce();
    resolvePreview(preview);
    expect(await screen.findByRole("button", { name: "确认导入" })).toBeEnabled();
  });
});

describe("character switcher recovery", () => {
  const role = { id: "a".repeat(32), name: "Alice", running: true };

  it("retries parent authentication after a transient failure", async () => {
    window.history.replaceState({}, "", `/?character=${role.id}`);
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new Error("Temporarily offline"))
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ws_path: "/", api_token: "parent" }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ characters: [role] }) });
    vi.stubGlobal("fetch", fetchMock);
    await expect(listCharacters("child")).rejects.toThrow("Temporarily offline");
    await expect(listCharacters("child")).resolves.toEqual([role]);
    expect(fetchMock.mock.calls[2][0]).toBe(`${window.location.origin}/api/characters`);
    expect(fetchMock.mock.calls[2][1].headers.Authorization).toBe("Bearer parent");
  });

  it("obtains fresh parent credentials on later refreshes", async () => {
    window.history.replaceState({}, "", `/?character=${role.id}`);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ws_path: "/", api_token: "old" }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ characters: [role] }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ws_path: "/", api_token: "new" }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ characters: [role] }) });
    vi.stubGlobal("fetch", fetchMock);
    await listCharacters("child");
    await expect(listCharacters("child")).resolves.toEqual([role]);
    expect(fetchMock.mock.calls[3][1].headers.Authorization).toBe("Bearer new");
  });

  it("reloads missing roles when the menu opens and preserves them if a refresh fails", async () => {
    window.history.replaceState({}, "", `/?character=${role.id}`);
    let offline = true;
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("/webui/bootstrap")) {
        return { ok: true, json: async () => ({ ws_path: "/", api_token: "parent" }) };
      }
      if (offline) throw new Error("Temporarily offline");
      return { ok: true, json: async () => ({ characters: [role] }) };
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<CharacterSidebar />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    offline = false;
    fireEvent.keyDown(screen.getByRole("button", { name: "当前角色" }), { key: "ArrowDown" });
    expect(await screen.findByRole("menuitemradio", { name: "Alice" })).toBeInTheDocument();
    expect(switchCharacter).not.toHaveBeenCalled();
    offline = true;
    fireEvent(window, new Event("nanobot:characters-changed"));
    const retry = await screen.findByRole("menuitem", { name: "角色列表加载失败，点击重试" });
    expect(screen.getByRole("menuitemradio", { name: "Alice" })).toBeInTheDocument();
    offline = false;
    fireEvent.click(retry);
    await waitFor(() => expect(screen.queryByText("角色列表加载失败，点击重试")).not.toBeInTheDocument());
    expect(screen.getByRole("menuitemradio", { name: "Alice" })).toBeInTheDocument();
  });
});
