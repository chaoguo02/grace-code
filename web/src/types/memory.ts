export type MemoryType = "user" | "feedback" | "project" | "reference";
export type MemoryStatus = "active" | "deprecated";
export type MemoryScope = "session" | "project" | "global";
export type MemoryLayer = "project" | "global" | "archive";

export interface MemoryItem {
  name: string;
  description: string;
  type: MemoryType;
  status: MemoryStatus;
  scope: MemoryScope;
  layer: MemoryLayer;
  confidence: number;
  updated_at: string;
  validated_at?: string;
  ttl_seconds?: number | null;
  expires_at?: string;
  access_count: number;
  anchors_count: number;
  preview?: string;
}

export interface MemoryOverview {
  enabled: boolean;
  preview: boolean;
  total: number;
  active: number;
  deprecated: number;
  archived: number;
  expiring: number;
  by_type: Record<MemoryType, number>;
  by_scope: Record<MemoryScope, number>;
  by_layer: Record<MemoryLayer, number>;
}

export interface MemoryResponse {
  overview: MemoryOverview;
  items: MemoryItem[];
}
