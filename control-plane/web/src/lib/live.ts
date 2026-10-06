import { core } from "./api";

export type LiveStatus = "connecting" | "live" | "down";

/**
 * Find the newest durable event so the stream starts at the head instead of replaying
 * the whole history. Events are only a "something changed, re-read" hint for the page.
 */
async function headCursor(workspaceId: string, signal: AbortSignal): Promise<string | undefined> {
  let cursor: string | undefined;
  for (let page = 0; page < 1000 && !signal.aborted; page += 1) {
    const { data } = await core(workspaceId).listEvents(cursor, 1000);
    if (data.next_after_event_id) cursor = data.next_after_event_id;
    if (!data.has_more) break;
  }
  return cursor;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      resolve();
    });
  });
}

/**
 * Follow one workspace's SSE stream. The core names each frame after its event type, so a
 * plain EventSource would need every type registered; reading the stream directly keeps
 * one handler and lets the cursor survive reconnects.
 */
export async function followWorkspace(
  workspaceId: string,
  signal: AbortSignal,
  onChange: () => void,
  onStatus: (status: LiveStatus) => void,
): Promise<void> {
  let cursor: string | undefined;
  let delay = 1000;
  let located = false;
  while (!signal.aborted) {
    onStatus("connecting");
    try {
      if (!located) {
        cursor = await headCursor(workspaceId, signal);
        located = true;
      }
      const response = await fetch(core(workspaceId).eventStreamUrl(cursor), {
        signal,
        headers: { accept: "text/event-stream" },
      });
      if (!response.ok || response.body === null) throw new Error(String(response.status));
      onStatus("live");
      delay = 1000;
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      let frameId: string | undefined;
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += value;
        let newline = buffer.indexOf("\n");
        while (newline >= 0) {
          const line = buffer.slice(0, newline).replace(/\r$/, "");
          buffer = buffer.slice(newline + 1);
          if (line === "") {
            if (frameId !== undefined) {
              cursor = frameId;
              frameId = undefined;
              onChange();
            }
          } else if (line.startsWith("id:")) {
            frameId = line.slice(3).trim();
          }
          newline = buffer.indexOf("\n");
        }
      }
    } catch {
      if (signal.aborted) return;
    }
    if (signal.aborted) return;
    onStatus("down");
    await sleep(delay, signal);
    delay = Math.min(delay * 2, 30000);
  }
}
