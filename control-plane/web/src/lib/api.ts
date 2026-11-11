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

/** Short user-facing errors for the home page, without provider text or internal codes. */
export function plainError(error: unknown): string {
  const status = errorStatus(error);
  if (status === 404) return "找不到这项内容，可能已经移除。";
  if (status === 409) return "当前状态已变化，请刷新后再试。";
  if (status === 401 || status === 403) return "暂时无法访问，请检查服务的连接设置。";
  if (status === 422 || status === 400) return "这次提交的内容未被接受，请检查后再试。";
  return "暂时读不到服务，请稍后重试。";
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
