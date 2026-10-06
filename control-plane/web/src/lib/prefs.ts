import { useSyncExternalStore } from "react";

/**
 * Per-viewer conveniences only. The core has no zone tag yet, so which Project is "life"
 * is remembered in this browser; nothing here is business state.
 */
export type LifeBinding = { readonly workspaceId: string; readonly projectId: string };
export type Zone = "life" | "work";
export type Theme = "system" | "light" | "dark";

type Prefs = {
  readonly life: LifeBinding | null;
  readonly actor: string;
  readonly zone: Zone;
  readonly theme: Theme;
  readonly labs: Readonly<Record<string, string>>;
};

const KEY = "ehai.ui.prefs.v1";
const DEFAULTS: Prefs = { life: null, actor: "", zone: "life", theme: "system", labs: {} };
const listeners = new Set<() => void>();

function read(): Prefs {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return DEFAULTS;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return DEFAULTS;
    return { ...DEFAULTS, ...(parsed as Partial<Prefs>) };
  } catch {
    return DEFAULTS;
  }
}

let snapshot = read();

export function getPrefs(): Prefs {
  return snapshot;
}

export function setPrefs(update: Partial<Prefs>): void {
  snapshot = { ...snapshot, ...update };
  try {
    window.localStorage.setItem(KEY, JSON.stringify(snapshot));
  } catch {
    // Storage may be unavailable (private window); the in-memory value still applies.
  }
  applyTheme(snapshot.theme);
  for (const listener of listeners) listener();
}

export function usePrefs(): Prefs {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => snapshot,
  );
}

/** Who decisions are recorded as; the core requires a non-empty actor. */
export function actorName(prefs: Prefs): string {
  return prefs.actor.trim() || "web";
}

export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}
