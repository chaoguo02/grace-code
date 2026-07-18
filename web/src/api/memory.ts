import { apiGet } from "./client";
import type { MemoryItem, MemoryOverview, MemoryResponse } from "../types/memory";

const previewItems: MemoryItem[] = [
  {
    name: "user-preferences",
    description: "Stable user preferences and operating style for Grace Code sessions.",
    type: "user",
    status: "active",
    scope: "global",
    layer: "global",
    confidence: 0.96,
    updated_at: "2026-07-17T18:24:00Z",
    validated_at: "2026-07-17T18:26:00Z",
    access_count: 18,
    anchors_count: 0,
    preview: "User prefers plan-first execution, strong architecture alignment, and explicit verification.",
  },
  {
    name: "review-rules",
    description: "Feedback memories that capture user corrections about subagent and planning behavior.",
    type: "feedback",
    status: "active",
    scope: "global",
    layer: "global",
    confidence: 0.92,
    updated_at: "2026-07-18T00:08:00Z",
    validated_at: "2026-07-18T00:10:00Z",
    access_count: 27,
    anchors_count: 3,
    preview: "Do not invent architecture. Study Claude Code patterns first, then align the local system deliberately.",
  },
  {
    name: "session-runtime-layout",
    description: "Project memory describing runtime, session storage, and repo isolation design.",
    type: "project",
    status: "active",
    scope: "project",
    layer: "project",
    confidence: 0.81,
    updated_at: "2026-07-17T22:41:00Z",
    access_count: 9,
    anchors_count: 6,
    ttl_seconds: 604800,
    expires_at: "2026-07-24T22:41:00Z",
    preview: "SessionRuntime, ReActAgent, ToolRegistry, and per-project state paths cooperate to keep state outside the tracked tree.",
  },
  {
    name: "frontend-ui-mvp",
    description: "Reference notes for the current chat shell, sidebar proportions, and event rail.",
    type: "reference",
    status: "active",
    scope: "project",
    layer: "project",
    confidence: 0.74,
    updated_at: "2026-07-18T01:12:00Z",
    access_count: 4,
    anchors_count: 2,
    ttl_seconds: 259200,
    expires_at: "2026-07-21T01:12:00Z",
    preview: "Latest UI direction favors a three-column workspace, softer glass panels, and richer execution traces.",
  },
  {
    name: "legacy-forge-branding",
    description: "Deprecated migration note for old forge-agent naming and directory conventions.",
    type: "project",
    status: "deprecated",
    scope: "project",
    layer: "archive",
    confidence: 0.58,
    updated_at: "2026-07-15T09:30:00Z",
    access_count: 1,
    anchors_count: 1,
    preview: "Superseded after the rename to Grace Code and .grace-based state folders.",
  },
];

function buildPreviewOverview(items: MemoryItem[]): MemoryOverview {
  const by_type: MemoryOverview["by_type"] = {
    user: 0,
    feedback: 0,
    project: 0,
    reference: 0,
  };
  const by_scope: MemoryOverview["by_scope"] = {
    session: 0,
    project: 0,
    global: 0,
  };
  const by_layer: MemoryOverview["by_layer"] = {
    project: 0,
    global: 0,
    archive: 0,
  };

  let active = 0;
  let deprecated = 0;
  let archived = 0;
  let expiring = 0;
  const now = Date.now();

  for (const item of items) {
    by_type[item.type] += 1;
    by_scope[item.scope] += 1;
    by_layer[item.layer] += 1;
    if (item.status === "active") active += 1;
    if (item.status === "deprecated") deprecated += 1;
    if (item.layer === "archive") archived += 1;
    if (item.expires_at) {
      const ms = new Date(item.expires_at).getTime() - now;
      if (ms > 0 && ms < 1000 * 60 * 60 * 24 * 7) expiring += 1;
    }
  }

  return {
    enabled: true,
    preview: true,
    total: items.length,
    active,
    deprecated,
    archived,
    expiring,
    by_type,
    by_scope,
    by_layer,
  };
}

const previewResponse: MemoryResponse = {
  overview: buildPreviewOverview(previewItems),
  items: previewItems,
};

export async function getMemorySnapshot(): Promise<MemoryResponse> {
  try {
    return await apiGet<MemoryResponse>("/api/memory");
  } catch {
    return previewResponse;
  }
}
