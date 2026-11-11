import { useEffect, useSyncExternalStore } from "react";
import { manager } from "./api";
import { getPrefs, setPrefs, type LifeBinding } from "./prefs";

type State = { binding: LifeBinding | null; loading: boolean; error: unknown };
let state: State = { binding: null, loading: true, error: undefined };
const listeners = new Set<() => void>();
let initial: Promise<void> | undefined;
function publish(update: State) {
  state = update;
  for (const listener of listeners) listener();
}
export async function setLifeBinding(binding: LifeBinding | null): Promise<void> {
  const result = await manager.setLifeBinding({ binding: binding ? { workspace_id: binding.workspaceId, project_id: binding.projectId } : null });
  const saved = result.data.binding;
  publish({ binding: saved ? { workspaceId: saved.workspace_id, projectId: saved.project_id } : null, loading: false, error: undefined });
  // This field is retained only as a migration source, never as the active binding.
  setPrefs({ life: null });
}
async function load() {
  try {
    const { data } = await manager.getLifeBinding();
    const legacy = getPrefs().life;
    if (data.binding === null && legacy !== null) await setLifeBinding(legacy);
    else {
      const b = data.binding;
      publish({ binding: b ? { workspaceId: b.workspace_id, projectId: b.project_id } : null, loading: false, error: undefined });
      setPrefs({ life: null });
    }
  } catch (error) { publish({ ...state, loading: false, error }); }
}
export function useLifeBinding() {
  useEffect(() => { initial ??= load(); }, []);
  return useSyncExternalStore((listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; }, () => state);
}
