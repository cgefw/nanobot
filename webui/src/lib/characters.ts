/** Role selection is a page boundary: socket, queries, and in-memory caches reset together. */
let greetingIndex = 0;
export function selectedGreeting(): number { return greetingIndex; }
export function selectGreeting(index: number): void { greetingIndex = index; }

export function characterId(): string {
  if (typeof window === "undefined") return "";
  const value = new URLSearchParams(window.location.search).get("character") ?? "";
  return /^[a-f0-9]{32}$/.test(value) ? value : "";
}

export function characterUrl(url: string): string {
  const id = characterId();
  return id && /^\/(api|webui)\//.test(url) ? `/_characters/${id}${url}` : url;
}

export function characterStorageKey(key: string): string {
  const id = characterId();
  return id ? `${key}.character.${id}` : key;
}

export function switchCharacter(id: string): void {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set("character", id);
  else url.searchParams.delete("character");
  url.hash = "/new";
  window.location.assign(url);
}
