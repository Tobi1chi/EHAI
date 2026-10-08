const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

export function hm(date: Date): string {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
  );
}

function addDays(date: Date, days: number): Date {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

export function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

/** "10 月 6 日 周二" */
export function longDate(date: Date): string {
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日 ${WEEKDAYS[date.getDay()]}`;
}

/** Absolute local time, year only when it differs: "10 月 6 日 09:00". */
export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const now = new Date();
  const year = date.getFullYear() === now.getFullYear() ? "" : `${date.getFullYear()} 年 `;
  return `${year}${date.getMonth() + 1} 月 ${date.getDate()} 日 ${hm(date)}`;
}

/** Relative description for recent moments: "5 分钟前", "3 天前". */
export function ago(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const seconds = Math.round((now.getTime() - then) / 1000);
  if (seconds < 45) return "刚刚";
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} 小时前`;
  if (seconds < 86400 * 30) return `${Math.round(seconds / 86400)} 天前`;
  return dateTime(iso);
}

/** How long something took: "42 秒", "5 分钟", "1 小时 12 分". An open end counts up to now. */
export function duration(startIso: string, endIso: string | null | undefined, now: Date = new Date()): string {
  const end = endIso ? new Date(endIso).getTime() : now.getTime();
  const seconds = Math.max(0, Math.round((end - new Date(startIso).getTime()) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`;
  const minutes = Math.round(seconds / 60);
  return `${Math.floor(minutes / 60)} 小时${minutes % 60 ? ` ${minutes % 60} 分` : ""}`;
}

/** Due/next-run wording relative to today: "今天 18:00", "明天", "10/7 周三 10:30". */
export function when(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "";
  const date = new Date(iso);
  const time = date.getHours() === 0 && date.getMinutes() === 0 ? "" : ` ${hm(date)}`;
  if (sameDay(date, now)) return `今天${time}`;
  if (sameDay(date, addDays(now, 1))) return `明天${time}`;
  if (sameDay(date, addDays(now, -1))) return `昨天${time}`;
  const year = date.getFullYear() === now.getFullYear() ? "" : `${date.getFullYear()}/`;
  return `${year}${date.getMonth() + 1}/${date.getDate()} ${WEEKDAYS[date.getDay()]}${time}`;
}

export type DueBucket = "overdue" | "today" | "later" | "none";

export function dueBucket(iso: string | null, now: Date = new Date()): DueBucket {
  if (!iso) return "none";
  const due = new Date(iso);
  if (sameDay(due, now)) {
    // A date without a time (local midnight) stays "today" for the whole day.
    const timed = due.getHours() !== 0 || due.getMinutes() !== 0;
    return timed && due.getTime() < now.getTime() ? "overdue" : "today";
  }
  return due.getTime() < now.getTime() ? "overdue" : "later";
}

export type IntervalUnit = "minutes" | "hours" | "days";

export const UNIT_SECONDS: Record<IntervalUnit, number> = { minutes: 60, hours: 3600, days: 86400 };
export const UNIT_LABEL: Record<IntervalUnit, string> = { minutes: "分钟", hours: "小时", days: "天" };

export function splitInterval(seconds: number): { amount: number; unit: IntervalUnit } {
  if (seconds % 86400 === 0) return { amount: seconds / 86400, unit: "days" };
  if (seconds % 3600 === 0) return { amount: seconds / 3600, unit: "hours" };
  return { amount: Math.max(1, Math.round(seconds / 60)), unit: "minutes" };
}

export function intervalText(seconds: number): string {
  const { amount, unit } = splitInterval(seconds);
  return amount === 1 ? `每${unit === "minutes" ? "分钟" : UNIT_LABEL[unit]}` : `每 ${amount} ${UNIT_LABEL[unit]}`;
}

/** Value for <input type="datetime-local"> in local time. */
export function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${hm(d)}`;
}

/** Local date/time inputs to an aware ISO timestamp, as the core requires an offset. */
export function fromLocalInput(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

export function localDateInput(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function timeZoneLabel(): string {
  const offset = -new Date().getTimezoneOffset();
  const sign = offset >= 0 ? "+" : "-";
  const abs = Math.abs(offset);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return `${zone}（UTC${sign}${Math.floor(abs / 60)}${abs % 60 ? ":" + pad(abs % 60) : ""}）`;
}

export function shortId(id: string | null | undefined): string {
  return id ? id.replace(/-/g, "").slice(0, 8) : "—";
}

export function json(value: unknown): string {
  return JSON.stringify(value, null, 2);
}
