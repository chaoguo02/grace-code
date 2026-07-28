import { apiGet, apiPost, apiPatch, apiDelete } from "./client";
import type { MemoryItem, MemoryOverview, MemoryRecallResponse, MemoryResponse } from "../types/memory";

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
  name: string; description: string; content?: string; type?: string; ttl_seconds?: number;
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

export async function getSessionMemoryRecalls(sessionId: string): Promise<MemoryRecallResponse> {
  return apiGet(`/api/memory/sessions/${encodeURIComponent(sessionId)}/recalls`);
}

export async function previewSessionMemoryRecall(sessionId: string, query: string, topK = 8): Promise<MemoryRecallResponse> {
  return apiPost(`/api/memory/sessions/${encodeURIComponent(sessionId)}/preview-recall`, { query, top_k: topK });
}

export async function getSessionGeneratedMemories(sessionId: string): Promise<{ session_id: string; items: MemoryItem[] }> {
  return apiGet(`/api/memory/sessions/${encodeURIComponent(sessionId)}/generated`);
}

export async function setSessionMemoryOverride(
  sessionId: string,
  memoryName: string,
  action: "pin" | "disable" | "unpin" | "enable",
): Promise<{ session_id: string; memory_name: string; action: string }> {
  return apiPost(`/api/memory/sessions/${encodeURIComponent(sessionId)}/overrides`, {
    memory_name: memoryName,
    action,
  });
}

/** Fetch entity links for a memory. */
export async function getMemoryEdges(name: string): Promise<Array<{ source_name: string; target_name: string; relation_type: string; confidence: number; evidence: string }>> {
  return apiGet(`/api/memory/${encodeURIComponent(name)}/edges`);
}

/** Fetch revision history for a memory. */
export async function getMemoryRevisions(name: string): Promise<Array<{ revision: number; content_hash: string; payload: Record<string, unknown>; created_at: string }>> {
  return apiGet(`/api/memory/${encodeURIComponent(name)}/revisions`);
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
