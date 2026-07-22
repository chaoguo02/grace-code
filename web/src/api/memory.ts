import { apiGet, apiPost, apiPatch, apiDelete } from "./client";
import type { MemoryItem, MemoryOverview, MemoryResponse } from "../types/memory";

/** Fetch all memories from the API. */
export async function getMemorySnapshot(): Promise<MemoryResponse> {
  try {
    const resp: { items: MemoryItem[]; overview: MemoryOverview } = await apiGet("/api/memory?_expand=true");
    return { items: resp.items, overview: resp.overview };
  } catch {
    return { overview: emptyOverview(), items: [] };
  }
}

/** Semantic search across memories. */
export async function searchMemories(q: string, topK = 5): Promise<Array<{ name: string; content: string; score: number }>> {
  return apiGet(`/api/memory/search?q=${encodeURIComponent(q)}&top_k=${topK}`);
}

/** Fetch a single memory with full content. */
export async function getMemoryDetail(name: string): Promise<Record<string, unknown>> {
  return apiGet(`/api/memory/${encodeURIComponent(name)}`);
}

/** Create a new memory. */
export async function createMemory(data: {
  name: string; description: string; content?: string; type?: string;
}): Promise<{ name: string; status: string }> {
  return apiPost("/api/memory", data);
}

/** Update an existing memory. */
export async function updateMemory(name: string, data: Record<string, unknown>): Promise<{ name: string; status: string }> {
  return apiPatch(`/api/memory/${encodeURIComponent(name)}`, data);
}

/** Delete a memory. */
export async function deleteMemory(name: string): Promise<{ name: string; deleted: boolean }> {
  return apiDelete(`/api/memory/${encodeURIComponent(name)}`);
}

function emptyOverview(): MemoryOverview {
  return {
    enabled: true, preview: false, total: 0, active: 0, deprecated: 0,
    archived: 0, expiring: 0,
    by_type: { user: 0, feedback: 0, project: 0, reference: 0 },
    by_scope: { session: 0, project: 0, global: 0 },
    by_layer: { project: 0, global: 0, archive: 0 },
  };
}
