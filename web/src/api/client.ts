const BASE = "";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await r.json().catch(() => null);
  if (!r.ok) {
    throw new ApiError(r.status, data?.detail || `${r.status} ${r.statusText}`);
  }
  return data as T;
}

export function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, signal ? { signal } : undefined);
}

export function apiPost<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body != null ? JSON.stringify(body) : "{}",
    ...(signal ? { signal } : {}),
  });
}

export function apiDelete<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, signal ? { method: "DELETE", signal } : { method: "DELETE" });
}

export function apiPatch<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(path, {
    method: "PATCH",
    body: body != null ? JSON.stringify(body) : "{}",
    ...(signal ? { signal } : {}),
  });
}

/**
 * Multipart upload helper. Unlike the JSON helpers, no Content-Type header is
 * set — the browser generates the multipart boundary itself.
 */
export async function apiUpload<T>(
  path: string,
  formData: FormData,
  signal?: AbortSignal,
): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    method: "POST",
    body: formData,
    ...(signal ? { signal } : {}),
  });
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    throw new ApiError(resp.status, data?.detail || `${resp.status} ${resp.statusText}`);
  }
  return data as T;
}
