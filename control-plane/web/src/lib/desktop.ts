/**
 * What the page can ask of the desktop shell when it runs inside it, with browser fallbacks.
 * The shell grants daemon pages only a folder picker and notifications; nothing else is reached
 * through here.
 */

type TauriGlobal = {
  readonly dialog?: {
    open(options: { directory: boolean; multiple: boolean; title?: string }): Promise<string | string[] | null>;
  };
  readonly notification?: {
    isPermissionGranted(): Promise<boolean>;
    requestPermission(): Promise<NotificationPermission>;
    sendNotification(options: { title: string; body?: string }): void;
  };
};

function tauri(): TauriGlobal | undefined {
  return (window as unknown as { __TAURI__?: TauriGlobal }).__TAURI__;
}

/** True inside the desktop shell, where a native folder picker is available. */
export function canPickFolder(): boolean {
  return tauri()?.dialog !== undefined;
}

/** Native folder picker; null when cancelled or outside the desktop shell. */
export async function pickFolder(title: string): Promise<string | null> {
  const dialog = tauri()?.dialog;
  if (dialog === undefined) return null;
  const chosen = await dialog.open({ directory: true, multiple: false, title });
  return typeof chosen === "string" ? chosen : null;
}

export type NotifyPermission = "granted" | "denied" | "default" | "unsupported";

export async function notificationPermission(): Promise<NotifyPermission> {
  const native = tauri()?.notification;
  if (native !== undefined) return (await native.isPermissionGranted()) ? "granted" : "default";
  if (typeof Notification === "undefined") return "unsupported";
  return Notification.permission;
}

/** Must run from a user gesture in browsers. */
export async function requestNotifications(): Promise<NotifyPermission> {
  const native = tauri()?.notification;
  if (native !== undefined) return (await native.requestPermission()) === "granted" ? "granted" : "denied";
  if (typeof Notification === "undefined") return "unsupported";
  return Notification.requestPermission();
}

/** Post a system notification if permitted; clicking a browser notification focuses the page. */
export function notify(title: string, body: string, onClick?: () => void): void {
  const native = tauri()?.notification;
  if (native !== undefined) {
    void native.isPermissionGranted().then((granted) => {
      if (granted) native.sendNotification({ title, body });
    });
    return;
  }
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  const notification = new Notification(title, { body });
  notification.onclick = () => {
    window.focus();
    onClick?.();
    notification.close();
  };
}
