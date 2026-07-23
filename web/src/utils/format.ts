/**
 * Pure utility functions for formatting values in UI components.
 *
 * Zero side effects — no store, DOM, or window access.
 */
export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatRuntime(createdAt?: string | null, completedAt?: string | null): string {
  if (!createdAt) return "—";
  const start = new Date(createdAt).getTime();
  if (Number.isNaN(start)) return "—";
  // For completed sessions, use the actual duration
  const end = completedAt ? new Date(completedAt).getTime() : Date.now();
  if (Number.isNaN(end)) return "—";
  const deltaSec = Math.max(0, Math.floor((end - start) / 1000));
  const min = Math.floor(deltaSec / 60);
  const sec = deltaSec % 60;
  if (min >= 60) {
    const h = Math.floor(min / 60);
    const m = min % 60;
    return `${h}h ${m}m`;
  }
  return `${String(min).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

export function runtimeSeconds(createdAt?: string | null, completedAt?: string | null): number {
  if (!createdAt) return 0;
  const start = new Date(createdAt).getTime();
  if (Number.isNaN(start)) return 0;
  const end = completedAt ? new Date(completedAt).getTime() : Date.now();
  if (Number.isNaN(end)) return 0;
  return Math.max(0, Math.floor((end - start) / 1000));
}

export function formatValue(v: unknown): string {
  if (typeof v === "string") return v.length > 120 ? v.slice(0, 120) + "…" : v;
  if (typeof v === "number") return String(v);
  if (typeof v === "boolean") return v ? "true" : "false";
  return JSON.stringify(v).slice(0, 120);
}
