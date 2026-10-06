import { useSyncExternalStore, type AnchorHTMLAttributes, type MouseEvent } from "react";

/** Every page lives under /ui; in-app paths below omit that prefix. */
export const BASE = "/ui";

const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  window.addEventListener("popstate", listener);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("popstate", listener);
  };
}

function currentPath(): string {
  const path = window.location.pathname;
  const inner = path.startsWith(BASE) ? path.slice(BASE.length) : path;
  return inner === "" ? "/" : inner.replace(/\/+$/, "") || "/";
}

export function usePath(): string {
  return useSyncExternalStore(subscribe, currentPath);
}

export function href(to: string): string {
  return BASE + (to.startsWith("/") ? to : "/" + to);
}

export function navigate(to: string, options: { replace?: boolean } = {}): void {
  const target = href(to);
  if (options.replace) window.history.replaceState(null, "", target);
  else window.history.pushState(null, "", target);
  window.scrollTo(0, 0);
  for (const listener of listeners) listener();
}

/** Match "/work/w/:ws/runs/:run" style patterns; returns decoded params or null. */
export function match(pattern: string, path: string): Record<string, string> | null {
  const want = pattern.split("/").filter(Boolean);
  const have = path.split("/").filter(Boolean);
  if (want.length !== have.length) return null;
  const params: Record<string, string> = {};
  for (let i = 0; i < want.length; i += 1) {
    const w = want[i] as string;
    const h = have[i] as string;
    if (w.startsWith(":")) params[w.slice(1)] = decodeURIComponent(h);
    else if (w !== h) return null;
  }
  return params;
}

type LinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & { to: string };

export function Link({ to, onClick, ...rest }: LinkProps) {
  function handle(event: MouseEvent<HTMLAnchorElement>) {
    onClick?.(event);
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    navigate(to);
  }
  return <a href={href(to)} onClick={handle} {...rest} />;
}
