import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CharacterSidebar } from "@/components/Characters";

const { requestMutation, getToken } = vi.hoisted(() => ({
  requestMutation: vi.fn(), getToken: () => "test-token",
}));
vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ client: { requestMutation }, getToken }),
}));

const preview = { card: { name: "Dropped", description: "Test character" } };
const cardFile = () => new File(['{"name":"Dropped"}'], "role.json", { type: "application/json" });

beforeEach(() => {
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
