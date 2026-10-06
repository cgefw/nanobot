import { lazy, Suspense, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import i18n from "@/i18n";
import { BookOpen, Check, ChevronDown, Plus, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { SettingsGroup, SettingsRow } from "@/components/settings/shared/SettingsControls";
import { SettingsTextEditor } from "@/components/settings/shared/SettingsTextEditor";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useClient } from "@/providers/ClientProvider";
import { fetchBootstrap, loadSavedSecret } from "@/lib/bootstrap";
import { fetchWithTimeout } from "@/lib/http";
import { characterId, selectGreeting, selectedGreeting, switchCharacter } from "@/lib/characters";
import { cn } from "@/lib/utils";

export type Character = { id: string; name: string; running: boolean };
const AGENT_FILES = [
  ["AGENTS.md", "characters.agentRules"],
  ["SOUL.md", "characters.agentSoul"],
  ["USER.md", "characters.agentUser"],
] as const;
const EMPTY_AGENT = { name: "", files: { "AGENTS.md": "", "SOUL.md": "", "USER.md": "" } };
const CharacterMarkdown = lazy(() => import("@/components/MarkdownTextRenderer"));
type CardPreview = {
  kind?: "agent";
  files?: Record<string, string>;
  card: { name: string; description?: string; personality?: string; scenario?: string;
    first_mes?: string; mes_example?: string; creator_notes?: string; system_prompt?: string;
    alternate_greetings?: string[]; character_book?: { entries: unknown[] } };
  avatar?: string | null;
  warnings?: string[];
  greetings?: string[];
};

async function readJson<T>(url: string, token: string): Promise<T> {
  const res = await fetchWithTimeout(url, { headers: { Authorization: `Bearer ${token}` } });
  if (!res.ok) throw new Error(i18n.t("characters.requestError", { status: res.status }));
  return res.json() as Promise<T>;
}

let parentCredentials: ReturnType<typeof fetchBootstrap> | undefined;
export async function listCharacters(token: string): Promise<Character[]> {
  if (characterId()) {
    // Share concurrent lookups, but never retain failed or expired parent tokens.
    parentCredentials ??= fetchBootstrap(window.location.origin, loadSavedSecret())
      .finally(() => { parentCredentials = undefined; });
    token = (await parentCredentials).api_token ?? "";
  }
  const result = await readJson<{ characters: Character[] }>(
    `${window.location.origin}/api/characters`, token,
  );
  return result.characters;
}

function CardContent({ preview }: { preview: CardPreview }) {
  const { t } = useTranslation();
  return <div className="min-w-0 space-y-4 text-[13px] leading-6">
    {preview.avatar && <img src={`data:image/png;base64,${preview.avatar}`} alt={preview.card.name}
      className="h-32 w-32 rounded-control object-cover" />}
    <p className="text-base font-medium">{preview.card.name}</p>
    {[[t("characters.systemPrompt"), preview.card.system_prompt], [t("characters.description"), preview.card.description], [t("characters.personality"), preview.card.personality],
      [t("characters.scenario"), preview.card.scenario], [t("characters.greeting"), preview.card.first_mes],
      [t("characters.examples"), preview.card.mes_example], [t("characters.notes"), preview.card.creator_notes],
    ].map(([label, content]) => content && <div key={label}>
      <p className="mb-1 font-medium text-muted-foreground">{label}</p>
      <p className="whitespace-pre-wrap break-words">{content}</p>
    </div>)}
    {!!preview.card.alternate_greetings?.length && <p className="text-muted-foreground">
      {t("characters.alternates", { count: preview.card.alternate_greetings.length })}
    </p>}
    {preview.card.character_book && <p className="text-muted-foreground">
      {t("characters.lore", { count: preview.card.character_book.entries.length })}
    </p>}
    {preview.warnings?.map((warning) => <p key={warning} className="text-amber-700 dark:text-amber-300">
      {t(`characters.warnings.${warning}`, { defaultValue: warning })}
    </p>)}
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

/** Localize a character error code from the gateway; other messages are shown as sent. */
export function characterErrorMessage(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err);
  return /^[a-z][a-z0-9_]*$/.test(message)
    ? i18n.t(`characters.errors.${message}`, { defaultValue: message })
    : message;
}

export function CharacterSidebar() {
  const { t } = useTranslation();
  const { client, getToken } = useClient();
  const [characters, setCharacters] = useState<Character[]>([]);
  const [listRevision, setListRevision] = useState(0);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState("");
  const [updateId, setUpdateId] = useState(() =>
    new URLSearchParams(window.location.hash.split("?")[1]).get("updateCharacter") ?? "");
  const [mode, setMode] = useState<"import" | "create" | null>(() =>
    updateId || /^#\/new\?(import|create)Character=1$/.test(window.location.hash) ? "import" : null);
  const [draft, setDraft] = useState(EMPTY_AGENT);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<CardPreview | null>(null);
  const upload = useRef<{ data: string; filename: string } | null>(null);
  const pickerId = useId();
  const createFormId = useId();
  const current = characterId();
  useEffect(() => {
    if (/^#\/new\?(?:(import|create)Character=1|updateCharacter=[a-f0-9]{32})$/.test(window.location.hash)) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#/new`);
    }
    let active = true;
    const refresh = () => {
      setListLoading(true); setListError("");
      void listCharacters(getToken()).then((items) => { if (active) setCharacters(items); })
        .catch((err: unknown) => { if (active) setListError(err instanceof Error ? err.message : String(err)); })
        .finally(() => { if (active) setListLoading(false); });
    };
    refresh();
    window.addEventListener("nanobot:characters-changed", refresh);
    return () => { active = false; window.removeEventListener("nanobot:characters-changed", refresh); };
  }, [getToken, updateId, listRevision]);

  function openSheet() {
    if (busy) return;
    if (current) { switchCharacter("", "/new?createCharacter=1"); return; }
    setUpdateId(""); setError(""); setMode("import");
  }

  async function createCharacter() {
    if (busy || !draft.name.trim()) return;
    setBusy(true); setError("");
    try {
      const role = await client.requestMutation<{ id: string }>("characters.create", draft);
      setDraft(EMPTY_AGENT); setMode(null);
      switchCharacter(role.id);
    } catch (err) { setError(characterErrorMessage(err)); }
    finally { setBusy(false); }
  }

  async function chooseFile(file?: File) {
    if (!file || busy) return;
    setMode("import"); setError(""); setPreview(null); upload.current = null;
    if (!/\.(json|png)$/i.test(file.name)) { setError(t("characters.invalidFile")); return; }
    if (file.size > 8 * 1024 * 1024) { setError(t("characters.fileTooLarge")); return; }
    setBusy(true);
    try {
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(new Error(t("characters.readError")));
        reader.readAsDataURL(file);
      });
      const payload = { filename: file.name, data: dataUrl.split(",")[1] };
      const result = await client.requestMutation<CardPreview>(updateId ? "characters.update" : "characters.import", {
        ...payload, ...(updateId ? { id: updateId } : {}), preview: true,
      });
      upload.current = payload; setPreview(result);
    } catch (err) { setError(characterErrorMessage(err)); }
    finally { setBusy(false); }
  }

  async function importCard() {
    if (!upload.current) return;
    setBusy(true); setError("");
    try {
      await client.requestMutation(updateId ? "characters.update" : "characters.import", {
        ...upload.current, ...(updateId ? { id: updateId } : {}),
      });
      setCharacters(await listCharacters(getToken()));
      setPreview(null); upload.current = null;
      if (updateId) switchCharacter(updateId);
    } catch (err) { setError(characterErrorMessage(err)); }
    finally { setBusy(false); }
  }

  return <div className="space-y-1 px-2 pb-2">
    <label className="block px-2 py-1 text-xs text-muted-foreground" htmlFor={pickerId}>{t("characters.current")}</label>
    <DropdownMenu modal={false} onOpenChange={(open) => { if (open) setListRevision((value) => value + 1); }}>
      <DropdownMenuTrigger asChild>
        <Button id={pickerId} variant="outline" aria-label={t("characters.current")} className="h-9 w-full justify-between rounded-full px-3 text-[13px] font-normal shadow-none settings-hover">
          <span className="truncate">{current ? characters.find((role) => role.id === current)?.name ?? t("characters.loading") : t("characters.default")}</span>
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-[var(--radix-dropdown-menu-trigger-width)]">
        {listLoading && <DropdownMenuItem disabled>{t("characters.loading")}</DropdownMenuItem>}
        {listError && <DropdownMenuItem className="whitespace-normal text-destructive" title={listError}
          onSelect={(event) => { event.preventDefault(); setListRevision((value) => value + 1); }}>
          {t("characters.retryList")}
        </DropdownMenuItem>}
        {[{ id: "", name: t("characters.default") }, ...characters].map((role) => <DropdownMenuItem key={role.id}
          role="menuitemradio" aria-checked={current === role.id}
          className={current === role.id ? "bg-muted" : undefined}
          onSelect={() => { if (current !== role.id) switchCharacter(role.id); }}>
          <span className="min-w-0 flex-1 truncate">{role.name}</span>
          {current === role.id && <Check aria-hidden />}
        </DropdownMenuItem>)}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={openSheet}>
          <Plus aria-hidden />{t("characters.create")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
    <Sheet open={mode !== null} onOpenChange={(open) => { if (!open && !busy) setMode(null); }}>
      <CharacterSheet title={updateId ? t("characters.update") : t("characters.create")} description={updateId
        ? t("characters.updateHelp")
        : t("characters.createHelp")}
        footer={mode === "create"
          ? <Button type="submit" form={createFormId} className="h-9 w-full rounded-full sm:w-auto"
            disabled={busy || !draft.name.trim()}>{t("characters.createOpen")}</Button>
          : preview && <Button className="h-9 w-full rounded-full sm:w-auto" disabled={busy} onClick={() => void importCard()}>{updateId ? t("characters.confirmUpdate") : t("characters.confirmImport")}</Button>}
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
              setError(t("characters.oneFile")); setPreview(null); upload.current = null; return;
            }
            void chooseFile(event.dataTransfer.files[0]);
          }}>
          <Upload className="h-4 w-4 shrink-0" /><span>{t("characters.drop")}</span>
          <input aria-label={t("characters.import")} type="file" accept=".json,.png" className="sr-only" disabled={busy}
            onChange={(event) => { void chooseFile(event.target.files?.[0]); event.target.value = ""; }} />
        </label>
        <p className="text-xs leading-5 text-muted-foreground">{t("characters.support")}</p>
        {preview && <CardContent preview={preview} />}
        {!updateId && <div className="border-t border-border/45 pt-5">
          {mode === "create" ? <form id={createFormId} className="space-y-5"
            onSubmit={(event) => { event.preventDefault(); void createCharacter(); }}>
            <label className="block space-y-2 text-[13px]">
              <span className="font-medium">{t("characters.agentName")}</span>
              <Input required maxLength={256} value={draft.name} disabled={busy} placeholder={t("characters.agentPlaceholder")}
                onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
            </label>
            <p className="text-xs leading-5 text-muted-foreground">{t("characters.agentHelp")}</p>
            {AGENT_FILES.map(([name, description]) => <label key={name} className="block space-y-2 text-[13px]">
              <span className="font-medium">{name}</span>
              <span className="block text-xs text-muted-foreground">{t(description)}</span>
              <Textarea rows={6} value={draft.files[name]} disabled={busy} placeholder={t(description)}
                className="font-mono text-[13px]"
                onChange={(event) => setDraft({ ...draft, files: { ...draft.files, [name]: event.target.value } })} />
            </label>)}
          </form> : <Button variant="outline" className="w-full rounded-full font-normal" disabled={busy}
            onClick={() => { setError(""); setPreview(null); upload.current = null; setMode("create"); }}>
            <Plus className="mr-2 h-4 w-4" aria-hidden />{t("characters.createAgent")}
          </Button>}
        </div>}
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        {busy && <p role="status" className="text-sm text-muted-foreground">{t("characters.busy")}</p>}
      </CharacterSheet>
    </Sheet>
  </div>;
}

function CharacterDetailsContent() {
  const { t } = useTranslation();
  const { client, getToken } = useClient();
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<CardPreview | null>(null);
  const [error, setError] = useState("");
  async function show() {
    setOpen(true); setError("");
    try { setPreview(await readJson<CardPreview>("/api/characters/current", getToken())); }
    catch (err) { setError(characterErrorMessage(err)); }
  }
  return <><Button variant="ghost" size="icon" aria-label={t("characters.profile")} title={t("characters.profile")} onClick={() => void show()}>
    <BookOpen className="h-4 w-4" />
  </Button><Sheet open={open} onOpenChange={setOpen}>
    <CharacterSheet title={preview?.kind === "agent" ? t("characters.agentProfile") : t("characters.cardProfile")}
      description={preview?.kind === "agent" ? t("characters.agentProfileHelp") : t("characters.cardProfileHelp")}>
      {preview?.kind === "agent" ? <>
        <p className="text-base font-medium">{preview.card.name}</p>
        <SettingsGroup>{AGENT_FILES.map(([name, description]) => <SettingsRow key={name} title={name} description={t(description)}>
          <SettingsTextEditor title={name} description={t(description)} value={preview.files?.[name] ?? ""}
            onSave={async (content) => {
              try {
                setPreview(await client.requestMutation<CardPreview>("agent.profile.update", { filename: name, content }));
              } catch (err) { throw new Error(characterErrorMessage(err), { cause: err }); }
            }} />
        </SettingsRow>)}</SettingsGroup>
      </> : preview && <>
        <CardContent preview={preview} />
        <Button variant="outline" className="w-full rounded-full font-normal"
          onClick={() => switchCharacter("", `/new?updateCharacter=${characterId()}`)}>
          <Upload className="mr-2 h-4 w-4" aria-hidden />{t("characters.update")}
        </Button>
      </>}
      {error && <p role="alert">{error}</p>}
    </CharacterSheet>
  </Sheet></>;
}

export function CharacterDetails() {
  return characterId() ? <CharacterDetailsContent /> : null;
}

export function CharacterWelcome() {
  const { t } = useTranslation();
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
    <h2 className="text-xl font-medium">{preview?.card.name ?? t("characters.loading")}</h2>
    <div className="break-words text-sm leading-7">
      <Suspense fallback={<p className="whitespace-pre-wrap">{preview?.greetings?.[index] ?? preview?.greetings?.[0]}</p>}>
        <CharacterMarkdown>{preview?.greetings?.[index] ?? preview?.greetings?.[0] ?? ""}</CharacterMarkdown>
      </Suspense>
    </div>
    {(preview?.greetings?.length ?? 0) > 1 && <Select value={String(index)}
      onValueChange={(value) => { const next = Number(value); setIndex(next); selectGreeting(next); }}>
      <SelectTrigger aria-label={t("characters.chooseGreeting")} className="w-fit rounded-full font-normal shadow-none settings-hover"><SelectValue /></SelectTrigger>
      <SelectContent>
        {preview?.greetings?.map((_, i) => <SelectItem key={i} value={String(i)}>{t("characters.greetingNumber", { number: i + 1 })}</SelectItem>)}
      </SelectContent>
    </Select>}
  </div>;
}
