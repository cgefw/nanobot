import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { BookOpen, Upload, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { useClient } from "@/providers/ClientProvider";
import { fetchBootstrap, loadSavedSecret } from "@/lib/bootstrap";
import { fetchWithTimeout } from "@/lib/http";
import { characterId, selectGreeting, selectedGreeting, switchCharacter } from "@/lib/characters";

type Character = { id: string; name: string; running: boolean };
const CharacterMarkdown = lazy(() => import("@/components/MarkdownTextRenderer"));
type CardPreview = {
  card: { name: string; description: string; personality: string; scenario: string;
    first_mes: string; mes_example: string; creator_notes: string;
    alternate_greetings?: string[]; character_book?: { entries: unknown[] } };
  avatar?: string | null;
  warnings?: string[];
  greetings?: string[];
};

async function readJson<T>(url: string, token: string): Promise<T> {
  const res = await fetchWithTimeout(url, { headers: { Authorization: `Bearer ${token}` } });
  if (!res.ok) throw new Error(`请求失败 (${res.status})`);
  return res.json() as Promise<T>;
}

let parentCredentials: ReturnType<typeof fetchBootstrap> | undefined;
async function listCharacters(token: string): Promise<Character[]> {
  if (characterId()) {
    parentCredentials ??= fetchBootstrap(window.location.origin, loadSavedSecret());
    token = (await parentCredentials).api_token ?? "";
  }
  const result = await readJson<{ characters: Character[] }>(
    `${window.location.origin}/api/characters`, token,
  );
  return result.characters;
}

function CardContent({ preview }: { preview: CardPreview }) {
  return <div className="space-y-4 text-sm">
    {preview.avatar && <img src={`data:image/png;base64,${preview.avatar}`} alt={preview.card.name}
      className="h-32 w-32 rounded-xl object-cover" />}
    <p className="text-lg font-medium">{preview.card.name}</p>
    {[["角色描述", preview.card.description], ["性格", preview.card.personality],
      ["场景", preview.card.scenario], ["开场白", preview.card.first_mes],
      ["对话示例", preview.card.mes_example], ["作者说明", preview.card.creator_notes],
    ].map(([label, content]) => content && <div key={label}>
      <p className="mb-1 font-medium text-muted-foreground">{label}</p>
      <p className="whitespace-pre-wrap break-words">{content}</p>
    </div>)}
    {!!preview.card.alternate_greetings?.length && <p className="text-muted-foreground">
      包含 {preview.card.alternate_greetings.length} 条备选开场白
    </p>}
    {preview.card.character_book && <p className="text-muted-foreground">
      世界书：{preview.card.character_book.entries.length} 条
    </p>}
    {preview.warnings?.map((warning) => <p key={warning} className="text-amber-700 dark:text-amber-400">{warning}</p>)}
  </div>;
}

export function CharacterSidebar() {
  const { client, getToken } = useClient();
  const [characters, setCharacters] = useState<Character[]>([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<CardPreview | null>(null);
  const upload = useRef<{ data: string; filename: string } | null>(null);
  const current = characterId();
  useEffect(() => {
    let active = true;
    void listCharacters(getToken()).then((items) => { if (active) setCharacters(items); })
      .catch(() => { /* The base chat remains usable on older gateways. */ });
    return () => { active = false; };
  }, [getToken]);

  async function chooseFile(file?: File) {
    if (!file) return;
    setError(""); setPreview(null); upload.current = null;
    if (file.size > 8 * 1024 * 1024) { setError("角色卡不能超过 8 MiB"); return; }
    setBusy(true);
    try {
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(new Error("无法读取文件"));
        reader.readAsDataURL(file);
      });
      const payload = { filename: file.name, data: dataUrl.split(",")[1] };
      const result = await client.requestMutation<CardPreview>("characters.import", { ...payload, preview: true });
      upload.current = payload; setPreview(result);
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  }

  async function importCard() {
    if (!upload.current) return;
    setBusy(true); setError("");
    try {
      await client.requestMutation("characters.import", upload.current);
      setCharacters(await listCharacters(getToken()));
      setPreview(null); upload.current = null;
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  }

  async function stopCharacter(id: string) {
    setBusy(true); setError("");
    try {
      await client.requestMutation("characters.stop", { id }, 30_000);
      setCharacters(await listCharacters(getToken()));
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  }

  return <div className="px-3 pb-3 space-y-2">
    <label className="text-xs text-muted-foreground" htmlFor="character-picker">当前角色</label>
    <select id="character-picker" aria-label="当前角色" value={current}
      className="w-full rounded-lg border bg-background px-2 py-2 text-sm"
      onChange={(event) => switchCharacter(event.target.value)}>
      <option value="">默认助手</option>
      {characters.map((role) => <option key={role.id} value={role.id}>{role.name}</option>)}
    </select>
    <Button variant="ghost" className="w-full justify-start gap-2 text-xs" onClick={() => {
      if (current) switchCharacter(""); else setOpen(true);
    }}><Users className="h-4 w-4" />{current ? "返回角色管理 / 系统设置" : "角色管理 · 导入角色卡"}</Button>
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetContent className="overflow-y-auto sm:max-w-lg">
        <div><SheetTitle>角色管理</SheetTitle></div>
        <p className="text-sm text-muted-foreground">每个角色拥有独立的聊天和记忆。打开角色后开始运行，停止后保留所有记录。</p>
        <div className="space-y-2">
          {characters.map((role) => <div key={role.id} className="flex items-center gap-2 rounded-lg border p-3">
            <span className="min-w-0 flex-1 truncate">{role.name}</span>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => switchCharacter(role.id)}>打开</Button>
            {role.running && <Button size="sm" variant="ghost" disabled={busy} onClick={() => void stopCharacter(role.id)}>停止</Button>}
          </div>)}
        </div>
        <label className="flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-dashed p-5 text-sm">
          <Upload className="h-4 w-4" />导入 JSON / PNG 角色卡
          <input aria-label="导入角色卡" type="file" accept=".json,.png" className="sr-only" disabled={busy}
            onChange={(event) => { void chooseFile(event.target.files?.[0]); event.target.value = ""; }} />
        </label>
        <p className="text-xs text-muted-foreground">支持 V1 / V2 和 V3 基础字段，世界书支持关键词匹配；不执行角色卡脚本。</p>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        {busy && <p role="status" className="text-sm text-muted-foreground">处理中…</p>}
        {preview && <><CardContent preview={preview} /><Button disabled={busy} onClick={() => void importCard()}>确认导入</Button></>}
      </SheetContent>
    </Sheet>
  </div>;
}

function CharacterDetailsContent() {
  const { getToken } = useClient();
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<CardPreview | null>(null);
  const [error, setError] = useState("");
  async function show() {
    setOpen(true); setError("");
    try { setPreview(await readJson<CardPreview>("/api/characters/current", getToken())); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  }
  return <><Button variant="ghost" size="icon" aria-label="角色设定" title="角色设定" onClick={() => void show()}>
    <BookOpen className="h-4 w-4" />
  </Button><Sheet open={open} onOpenChange={setOpen}>
    <SheetContent className="overflow-y-auto sm:max-w-lg">
      <div><SheetTitle>角色设定</SheetTitle></div>
      {preview && <CardContent preview={preview} />}
      {error && <p role="alert">{error}</p>}
    </SheetContent>
  </Sheet></>;
}

export function CharacterDetails() {
  return characterId() ? <CharacterDetailsContent /> : null;
}

export function CharacterWelcome() {
  const { getToken } = useClient();
  const [preview, setPreview] = useState<CardPreview | null>(null);
  const [index, setIndex] = useState(selectedGreeting);
  useEffect(() => {
    let active = true;
    void readJson<CardPreview>("/api/characters/current", getToken())
      .then((card) => { if (active) setPreview(card); }).catch(() => {});
    return () => { active = false; };
  }, [getToken]);
  return <div className="max-w-xl space-y-4 p-4 text-left">
    {preview?.avatar && <img src={`data:image/png;base64,${preview.avatar}`} alt=""
      className="h-16 w-16 rounded-full object-cover" />}
    <h2 className="text-xl font-medium">{preview?.card.name ?? "载入角色…"}</h2>
    <div className="break-words text-sm leading-7">
      <Suspense fallback={<p className="whitespace-pre-wrap">{preview?.greetings?.[index] ?? preview?.greetings?.[0]}</p>}>
        <CharacterMarkdown>{preview?.greetings?.[index] ?? preview?.greetings?.[0] ?? ""}</CharacterMarkdown>
      </Suspense>
    </div>
    {(preview?.greetings?.length ?? 0) > 1 && <select aria-label="选择开场白"
      className="rounded-lg border bg-background p-2 text-sm" value={index}
      onChange={(event) => { const next = Number(event.target.value); setIndex(next); selectGreeting(next); }}>
      {preview?.greetings?.map((_, i) => <option key={i} value={i}>开场白 {i + 1}</option>)}
    </select>}
  </div>;
}
