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

export function formatRuntime(createdAt?: string | null): string {
  if (!createdAt) return "00:00";
  const start = new Date(createdAt).getTime();
  if (Number.isNaN(start)) return "00:00";
  const deltaSec = Math.max(0, Math.floor((Date.now() - start) / 1000));
  const min = Math.floor(deltaSec / 60);
  const sec = deltaSec % 60;
  return `${String(min).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

export function runtimeSeconds(createdAt?: string | null): number {
  if (!createdAt) return 0;
  const start = new Date(createdAt).getTime();
  if (Number.isNaN(start)) return 0;
  return Math.max(0, Math.floor((Date.now() - start) / 1000));
}

export function formatValue(v: unknown): string {
  if (typeof v === "string") return v.length > 120 ? v.slice(0, 120) + "…" : v;
  if (typeof v === "number") return String(v);
  if (typeof v === "boolean") return v ? "true" : "false";
  return JSON.stringify(v).slice(0, 120);
}
