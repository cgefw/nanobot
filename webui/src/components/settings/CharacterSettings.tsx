import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { listCharacters, type Character } from "@/components/Characters";
import { SettingsGroup, SettingsRow, SettingsSectionTitle, StatusPill } from "@/components/settings/shared/SettingsControls";
import { Button } from "@/components/ui/button";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { characterId, switchCharacter } from "@/lib/characters";
import { useClient } from "@/providers/ClientProvider";

export function CharacterSettings() {
  const { t } = useTranslation();
  const { client, getToken } = useClient();
  const [characters, setCharacters] = useState<Character[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [deleting, setDeleting] = useState<Character | null>(null);
  useEffect(() => {
    // Management belongs to the main gateway; leave the role socket before stopping it.
    if (characterId()) { switchCharacter("", "/settings?section=characters"); return; }
    let active = true;
    void listCharacters(getToken()).then((items) => { if (active) setCharacters(items); })
      .catch((err: unknown) => { if (active) setError(err instanceof Error ? err.message : String(err)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [getToken]);

  async function changeCharacter(action: "stop" | "delete", id: string) {
    if (busy) return;
    setBusy(id); setError("");
    try {
      const result = await client.requestMutation<{ characters: Character[] }>(`characters.${action}`, { id }, 60_000);
      setCharacters(result.characters);
      if (action === "delete") setDeleting(null);
      window.dispatchEvent(new Event("nanobot:characters-changed"));
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(""); }
  }

  return <section className="settings-stack">
    <div className="settings-section-heading">
      <SettingsSectionTitle>{t("characters.manage")}</SettingsSectionTitle>
      <p className="w-full text-[13px] leading-6 text-muted-foreground">{t("characters.manageHelp")}</p>
    </div>
    {loading ? <p role="status" className="settings-list-inset text-[13px] text-muted-foreground">{t("characters.loading")}</p>
      : characters.length > 0 ? <SettingsGroup>
        {characters.map((role) => <SettingsRow key={role.id} title={<span className="break-words">{role.name}</span>}>
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill tone={role.running ? "success" : "neutral"}>{role.running ? t("characters.running") : t("characters.stopped")}</StatusPill>
            <Button size="sm" variant="outline" className="rounded-full font-normal" disabled={!!busy}
              onClick={() => switchCharacter(role.id)}>{t("settings.actions.open")}</Button>
            {role.running && <Button size="sm" variant="ghost" className="rounded-full font-normal" disabled={!!busy}
              onClick={() => void changeCharacter("stop", role.id)}>{busy === role.id && !deleting ? t("characters.stopping") : t("characters.stop")}</Button>}
            <Button size="sm" variant="ghost" className="rounded-full font-normal text-destructive hover:bg-destructive/10 hover:text-destructive"
              disabled={!!busy} onClick={() => { setError(""); setDeleting(role); }}>{t("settings.actions.delete")}</Button>
          </div>
        </SettingsRow>)}
      </SettingsGroup> : !error && <p className="settings-list-inset text-[13px] text-muted-foreground">{t("characters.empty")}</p>}
    {error && !deleting && <p role="alert" className="settings-list-inset text-[13px] text-destructive">{error}</p>}
    <AlertDialog open={!!deleting} onOpenChange={(open) => { if (!open && !busy) setDeleting(null); }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle className="break-words">{t("characters.deleteTitle", { name: deleting?.name })}</AlertDialogTitle>
          <AlertDialogDescription>
            {t("characters.deleteHelp")}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error && <p role="alert" className="break-words text-[13px] text-destructive">{error}</p>}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={!!busy}>{t("settings.actions.cancel")}</AlertDialogCancel>
          <AlertDialogAction disabled={!!busy} className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            onClick={(event) => { event.preventDefault(); if (deleting) void changeCharacter("delete", deleting.id); }}>
            {busy ? t("settings.actions.deleting") : t("deleteConfirm.confirm")}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </section>;
}
