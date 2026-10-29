export function ago(nowIso: string, thenIso: string | null): string {
  if (!thenIso) return "";
  const seconds = Math.max(0, Math.round((Date.parse(nowIso) - Date.parse(thenIso)) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

export function agoShort(nowIso: string, thenIso: string | null): string {
  if (!thenIso) return "";
  const seconds = Math.max(0, Math.round((Date.parse(nowIso) - Date.parse(thenIso)) / 1000));
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h`;
  return `${Math.floor(seconds / 86400)} d`;
}

export function until(nowIso: string, thenIso: string | null): string {
  if (!thenIso) return "";
  const seconds = Math.round((Date.parse(thenIso) - Date.parse(nowIso)) / 1000);
  if (seconds <= 0) return "expired";
  if (seconds < 3600) return `in ${Math.ceil(seconds / 60)} min`;
  return `in ${Math.floor(seconds / 3600)} h ${Math.floor((seconds % 3600) / 60)} min`;
}

export function clock(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function duration(seconds: number | null): string {
  if (seconds === null) return "—";
  const total = Math.round(seconds);
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, "0")}s`;
  const hours = Math.floor(total / 3600);
  return `${hours}h ${String(Math.floor((total % 3600) / 60)).padStart(2, "0")}m`;
}

export function humanize(text: string): string {
  const spaced = text.replace(/_/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  const letters = parts.length === 1 ? parts[0].slice(0, 2) : parts[0][0] + parts[parts.length - 1][0];
  return letters.toUpperCase();
}

export const RANGE_LABEL: Record<string, string> = {
  "24h": "Last 24 hours",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
};
export const RANGE_SHORT: Record<string, string> = { "24h": "24 h", "7d": "7 d", "30d": "30 d" };
