import { apiGet } from "./client";

export interface ModelCatalogItem {
  key: string;
  family: string;
  note: string;
}

export function getModelCatalog(signal?: AbortSignal): Promise<ModelCatalogItem[]> {
  return apiGet("/api/config/models", signal);
}

export interface AppDefaults {
  default_agent: string;
}

export function getAppDefaults(signal?: AbortSignal): Promise<AppDefaults> {
  return apiGet("/api/config/defaults", signal);
}
