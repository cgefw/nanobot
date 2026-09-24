import { lazy, Suspense, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { BookOpen, Check, ChevronDown, Plus, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useClient } from "@/providers/ClientProvider";
import { fetchBootstrap, loadSavedSecret } from "@/lib/bootstrap";
import { fetchWithTimeout } from "@/lib/http";
import { characterId, selectGreeting, selectedGreeting, switchCharacter } from "@/lib/characters";
import { cn } from "@/lib/utils";

export type Character = { id: string; name: string; running: boolean };
const CharacterMarkdown = lazy(() => import("@/components/MarkdownTextRenderer"));
type CardPreview = {
  card: { name: string; description: string; personality: string; scenario: string;
    first_mes: string; mes_example: string; creator_notes: string; system_prompt?: string;
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
export async function listCharacters(token: string): Promise<Character[]> {
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
  return <div className="min-w-0 space-y-4 text-[13px] leading-6">
    {preview.avatar && <img src={`data:image/png;base64,${preview.avatar}`} alt={preview.card.name}
      className="h-32 w-32 rounded-control object-cover" />}
    <p className="text-base font-medium">{preview.card.name}</p>
    {[["Agent 提示词", preview.card.system_prompt], ["角色描述", preview.card.description], ["性格", preview.card.personality],
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
    {preview.warnings?.map((warning) => <p key={warning} className="text-amber-700 dark:text-amber-300">{warning}</p>)}
  </div>;
}

function CharacterSheet({ title, description, children, footer }: {
  title: string;
  description: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return <SheetContent
    side="right"
    closeButtonClassName="right-2 top-2 inline-flex h-10 w-10 items-center justify-center rounded-full opacity-100 text-muted-foreground settings-hover hover:text-foreground sm:right-3 sm:top-3"
    className="w-full max-w-none gap-0 overflow-hidden border-l-0 p-0 sm:w-[min(34rem,calc(100vw-1rem))] sm:max-w-none sm:border-l"
  >
    <div className="shrink-0 border-b border-border/45 px-4 py-4 pr-14 sm:px-5 sm:py-5 sm:pr-16">
      <SheetTitle className="text-[19px] font-semibold sm:text-[20px]">{title}</SheetTitle>
      <SheetDescription className="mt-1 text-[13px] leading-6">{description}</SheetDescription>
    </div>
    <div className="min-h-0 min-w-0 flex-1 space-y-5 overflow-y-auto px-4 py-5 pb-[max(1.25rem,env(safe-area-inset-bottom))] sm:px-5">
      {children}
    </div>
    {footer && <div className="flex shrink-0 justify-end border-t border-border/45 px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:px-5">
      {footer}
    </div>}
  </SheetContent>;
}

export function CharacterSidebar() {
  const { client, getToken } = useClient();
  const [characters, setCharacters] = useState<Character[]>([]);
  const [mode, setMode] = useState<"import" | "create" | null>(() =>
    /^#\/new\?(import|create)Character=1$/.test(window.location.hash) ? "import" : null);
  const [draft, setDraft] = useState({ name: "", system_prompt: "", first_mes: "" });
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<CardPreview | null>(null);
  const upload = useRef<{ data: string; filename: string } | null>(null);
  const pickerId = useId();
  const createFormId = useId();
  const current = characterId();
  useEffect(() => {
    if (/^#\/new\?(import|create)Character=1$/.test(window.location.hash)) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#/new`);
    }
    let active = true;
    void listCharacters(getToken()).then((items) => { if (active) setCharacters(items); })
      .catch(() => { /* The base chat remains usable on older gateways. */ });
    return () => { active = false; };
  }, [getToken]);

  function openSheet() {
    if (busy) return;
    if (current) { switchCharacter("", "/new?createCharacter=1"); return; }
    setError(""); setMode("import");
  }

  async function createCharacter() {
    if (busy || !draft.name.trim() || !draft.system_prompt.trim()) return;
    setBusy(true); setError("");
    try {
      const role = await client.requestMutation<{ id: string }>("characters.create", draft);
      setDraft({ name: "", system_prompt: "", first_mes: "" }); setMode(null);
      switchCharacter(role.id);
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  }

  async function chooseFile(file?: File) {
    if (!file || busy) return;
    setMode("import"); setError(""); setPreview(null); upload.current = null;
    if (!/\.(json|png)$/i.test(file.name)) { setError("请选择 JSON 或 PNG 角色卡"); return; }
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

  return <div className="space-y-1 px-2 pb-2">
    <label className="block px-2 py-1 text-xs text-muted-foreground" htmlFor={pickerId}>当前角色</label>
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button id={pickerId} variant="outline" aria-label="当前角色" className="h-9 w-full justify-between rounded-full px-3 text-[13px] font-normal shadow-none settings-hover">
          <span className="truncate">{current ? characters.find((role) => role.id === current)?.name ?? "载入角色…" : "默认助手"}</span>
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-[var(--radix-dropdown-menu-trigger-width)]">
        {[{ id: "", name: "默认助手" }, ...characters].map((role) => <DropdownMenuItem key={role.id}
          role="menuitemradio" aria-checked={current === role.id}
          className={current === role.id ? "bg-muted" : undefined}
          onSelect={() => { if (current !== role.id) switchCharacter(role.id); }}>
          <span className="min-w-0 flex-1 truncate">{role.name}</span>
          {current === role.id && <Check aria-hidden />}
        </DropdownMenuItem>)}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={openSheet}>
          <Plus aria-hidden />创建角色
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
    <Sheet open={mode !== null} onOpenChange={(open) => { if (!open && !busy) setMode(null); }}>
      <CharacterSheet title="创建角色" description="导入 JSON / PNG 角色卡，或在下方通过自定义提示词创建角色。"
        footer={mode === "create"
          ? <Button type="submit" form={createFormId} className="h-9 w-full rounded-full sm:w-auto"
            disabled={busy || !draft.name.trim() || !draft.system_prompt.trim()}>创建并打开</Button>
          : preview && <Button className="h-9 w-full rounded-full sm:w-auto" disabled={busy} onClick={() => void importCard()}>确认导入</Button>}
      >
        <label className={cn("flex min-h-24 cursor-pointer items-center justify-center gap-2 rounded-control border border-dashed border-border px-4 py-5 text-[13px] settings-hover focus-within:ring-2 focus-within:ring-ring has-[:disabled]:opacity-50", dragging && "border-ring bg-muted")}
          onDragOver={(event) => {
            event.preventDefault(); event.stopPropagation();
            event.dataTransfer.dropEffect = busy ? "none" : "copy";
            setDragging(!busy && event.dataTransfer.types.includes("Files"));
          }}
          onDragLeave={(event) => {
            if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) setDragging(false);
          }}
          onDrop={(event) => {
            event.preventDefault(); event.stopPropagation(); setDragging(false);
            if (busy) return;
            if (event.dataTransfer.files.length > 1) {
              setError("每次只能导入一张角色卡"); setPreview(null); upload.current = null; return;
            }
            void chooseFile(event.dataTransfer.files[0]);
          }}>
          <Upload className="h-4 w-4 shrink-0" /><span>点击选择或拖入 JSON / PNG 角色卡</span>
          <input aria-label="导入角色卡" type="file" accept=".json,.png" className="sr-only" disabled={busy}
            onChange={(event) => { void chooseFile(event.target.files?.[0]); event.target.value = ""; }} />
        </label>
        <p className="text-xs leading-5 text-muted-foreground">支持 V1 / V2、V3 和 AICC 基础字段，世界书支持关键词匹配；不执行角色卡脚本。</p>
        {preview && <CardContent preview={preview} />}
        <div className="border-t border-border/45 pt-5">
          {mode === "create" ? <form id={createFormId} className="space-y-5"
            onSubmit={(event) => { event.preventDefault(); void createCharacter(); }}>
            <label className="block space-y-2 text-[13px]">
              <span className="font-medium">角色名称</span>
              <Input required maxLength={256} value={draft.name} disabled={busy} placeholder="例如：晚晴"
                onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
            </label>
            <label className="block space-y-2 text-[13px]">
              <span className="font-medium">Agent 提示词</span>
              <Textarea required rows={10} value={draft.system_prompt} disabled={busy}
                placeholder="描述角色的身份、性格、说话方式，以及你希望遵循的行为规则。"
                onChange={(event) => setDraft({ ...draft, system_prompt: event.target.value })} />
            </label>
            <label className="block space-y-2 text-[13px]">
              <span className="font-medium">开场白<span className="ml-2 font-normal text-muted-foreground">可选</span></span>
              <Textarea rows={3} value={draft.first_mes} disabled={busy} placeholder="开始新聊天时显示的第一句话"
                onChange={(event) => setDraft({ ...draft, first_mes: event.target.value })} />
            </label>
          </form> : <Button variant="outline" className="w-full rounded-full font-normal" disabled={busy}
            onClick={() => { setError(""); setPreview(null); upload.current = null; setMode("create"); }}>
            <Plus className="mr-2 h-4 w-4" aria-hidden />创建角色
          </Button>}
        </div>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        {busy && <p role="status" className="text-sm text-muted-foreground">处理中…</p>}
      </CharacterSheet>
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
    <CharacterSheet title="角色设定" description="查看当前角色的提示词、背景、性格与开场白。">
      {preview && <CardContent preview={preview} />}
      {error && <p role="alert">{error}</p>}
    </CharacterSheet>
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
    {(preview?.greetings?.length ?? 0) > 1 && <Select value={String(index)}
      onValueChange={(value) => { const next = Number(value); setIndex(next); selectGreeting(next); }}>
      <SelectTrigger aria-label="选择开场白" className="w-fit rounded-full font-normal shadow-none settings-hover"><SelectValue /></SelectTrigger>
      <SelectContent>
        {preview?.greetings?.map((_, i) => <SelectItem key={i} value={String(i)}>开场白 {i + 1}</SelectItem>)}
      </SelectContent>
    </Select>}
  </div>;
}
