import { create } from "zustand";
import type { SessionSummary, SessionDetail } from "../types";
import * as api from "../api/sessions";

interface SessionState {
  sessions: SessionSummary[];
  activeId: string | null;
  activeDetail: SessionDetail | null;
  isLoading: boolean;
  error: string | null;

  loadSessions: () => Promise<void>;
  openSession: (id: string) => Promise<void>;
  createSession: (agentName?: string, repoPath?: string) => Promise<string | null>;
  deleteSession: (id: string) => Promise<boolean>;
  refreshActive: () => Promise<void>;
}

export const useSessionStore = create<SessionState>((set, get) => ({
  sessions: [],
  activeId: null,
  activeDetail: null,
  isLoading: false,
  error: null,

  loadSessions: async () => {
    set({ isLoading: true, error: null });
    try {
      const sessions = await api.listSessions();
      set({ sessions, isLoading: false });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Failed to load sessions";
      set({ error: msg, isLoading: false });
    }
  },

  openSession: async (id: string) => {
    set({ isLoading: true, error: null });
    try {
      const detail = await api.getSession(id);
      set({ activeId: id, activeDetail: detail, isLoading: false });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Failed to open session";
      set({ error: msg, isLoading: false });
    }
  },

  createSession: async (agentName = "build", repoPath = ".") => {
    set({ isLoading: true, error: null });
    try {
      const resp = await api.createSession(agentName, repoPath);
      await get().loadSessions();
      await get().openSession(resp.session_id);
      return resp.session_id;
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Failed to create session";
      set({ error: msg, isLoading: false });
      return null;
    }
  },

  deleteSession: async (id: string) => {
    try {
      const resp = await api.deleteSession(id);
      if (resp.deleted) {
        const { activeId } = get();
        if (activeId === id) {
          set({ activeId: null, activeDetail: null });
        }
        await get().loadSessions();
        return true;
      }
      return false;
    } catch { return false; }
  },

  refreshActive: async () => {
    const { activeId } = get();
    if (activeId) {
      try {
        const detail = await api.getSession(activeId);
        set({ activeDetail: detail });
      } catch { /* ignore */ }
    }
  },
}));
