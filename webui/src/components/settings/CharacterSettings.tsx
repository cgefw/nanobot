import { useEffect, useState } from "react";
import { listCharacters, type Character } from "@/components/Characters";
import { SettingsGroup, SettingsRow, SettingsSectionTitle, StatusPill } from "@/components/settings/shared/SettingsControls";
import { Button } from "@/components/ui/button";
import { characterId, switchCharacter } from "@/lib/characters";
import { useClient } from "@/providers/ClientProvider";

export function CharacterSettings() {
  const { client, getToken } = useClient();
  const [characters, setCharacters] = useState<Character[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    // Management belongs to the main gateway; leave the role socket before stopping it.
    if (characterId()) { switchCharacter("", "/settings?section=characters"); return; }
    let active = true;
    void listCharacters(getToken()).then((items) => { if (active) setCharacters(items); })
      .catch((err: unknown) => { if (active) setError(err instanceof Error ? err.message : String(err)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [getToken]);

  async function stopCharacter(id: string) {
    setBusy(id); setError("");
    try {
      await client.requestMutation("characters.stop", { id }, 30_000);
      setCharacters(await listCharacters(getToken()));
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(""); }
  }

  return <section className="settings-stack">
    <div className="settings-section-heading">
      <SettingsSectionTitle>角色管理</SettingsSectionTitle>
      <p className="w-full text-[13px] leading-6 text-muted-foreground">每个角色拥有独立的聊天和记忆。启用 QQ 后随主服务自动上线，关闭网页不影响连接。手动停止后保留所有记录，再次打开才恢复上线。</p>
    </div>
    {loading ? <p role="status" className="settings-list-inset text-[13px] text-muted-foreground">载入角色…</p>
      : characters.length > 0 ? <SettingsGroup>
        {characters.map((role) => <SettingsRow key={role.id} title={<span className="break-words">{role.name}</span>}>
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill tone={role.running ? "success" : "neutral"}>{role.running ? "运行中" : "已停止"}</StatusPill>
            <Button size="sm" variant="outline" className="rounded-full font-normal" disabled={!!busy}
              onClick={() => switchCharacter(role.id)}>打开</Button>
            {role.running && <Button size="sm" variant="ghost" className="rounded-full font-normal" disabled={!!busy}
              onClick={() => void stopCharacter(role.id)}>{busy === role.id ? "停止中…" : "停止"}</Button>}
          </div>
        </SettingsRow>)}
      </SettingsGroup> : !error && <p className="settings-list-inset text-[13px] text-muted-foreground">暂无角色。可从侧栏的角色下拉菜单导入角色卡。</p>}
    {error && <p role="alert" className="settings-list-inset text-[13px] text-destructive">{error}</p>}
  </section>;
}
