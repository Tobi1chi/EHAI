import { EhaiApiClient, EhaiApiError, EhaiWorkspaceManagerClient } from "../../../src/index.js";

export * from "../../../src/index.js";

/** The page is served by ehai-manager (or proxied to it in development), so it is same-origin. */
export const manager = new EhaiWorkspaceManagerClient(window.location.origin);

const cores = new Map<string, EhaiApiClient>();

/** Client bound to one workspace core through the manager's scoped forwarding. */
export function core(workspaceId: string): EhaiApiClient {
  let client = cores.get(workspaceId);
  if (client === undefined) {
    client = manager.workspace(workspaceId);
    cores.set(workspaceId, client);
  }
  return client;
}

function randomId(): string {
  // randomUUID needs a secure context; a LAN address over plain HTTP is not one.
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function newIdempotencyKey(scope: string): string {
  return `ui-${scope}-${randomId()}`;
}

/** Whether the request may have reached the core, so a retry must reuse its idempotency key. */
export function outcomeUnknown(error: unknown): boolean {
  if (error instanceof EhaiApiError) return error.status >= 500;
  return true;
}

export function errorStatus(error: unknown): number | null {
  return error instanceof EhaiApiError ? error.status : null;
}

export function describeError(error: unknown): string {
  if (error instanceof EhaiApiError) {
    const code = error.detail?.error.code;
    // The reason first; the status and code stay for anyone reporting the problem.
    return `${error.message}（${error.status}${code ? ` ${code}` : ""}）`;
  }
  if (error instanceof TypeError) return "连不上 EHAI，看看服务有没有在运行。";
  if (error instanceof Error) return error.message;
  return String(error);
}
