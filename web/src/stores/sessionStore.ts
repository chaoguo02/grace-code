import { create } from "zustand";
import type { SessionSummary, SessionDetail } from "../types";
import * as api from "../api/sessions";
import { ApiError } from "../api/client";
import { registerSessionMissingHandler, useChatStore } from "./chatStore";

interface SessionState {
  sessions: SessionSummary[];
  activeId: string | null;
  activeDetail: SessionDetail | null;
  isLoading: boolean;
  error: string | null;
  sessionTree: api.SessionTreeNode | null;
  detailById: Record<string, SessionDetail>;
  treeById: Record<string, api.SessionTreeNode>;

  loadSessions: () => Promise<void>;
  openSession: (id: string) => Promise<void>;
  createSession: (agentName?: string, repoPath?: string, initialPlanFile?: string) => Promise<string | null>;
  deleteSession: (id: string) => Promise<boolean>;
  fetchSessionTree: (id: string) => Promise<void>;
  deleteSessionsBatch: (ids: string[]) => Promise<number>;
  refreshActive: () => Promise<void>;
  invalidateSessionLocally: (id: string) => void;
}

function pruneCachesBySessionIds(
  detailById: Record<string, SessionDetail>,
  treeById: Record<string, api.SessionTreeNode>,
  validIds: Set<string>,
) {
  return {
    detailById: Object.fromEntries(
      Object.entries(detailById).filter(([id]) => validIds.has(id)),
    ),
    treeById: Object.fromEntries(
      Object.entries(treeById).filter(([id]) => validIds.has(id)),
    ),
  };
}

export const useSessionStore = create<SessionState>((set, get) => ({
  sessions: [],
  activeId: null,
  activeDetail: null,
  isLoading: false,
  error: null,
  sessionTree: null,
  detailById: {},
  treeById: {},

  invalidateSessionLocally: (id) => {
    set((state) => {
      const nextDetails = { ...state.detailById };
      const nextTrees = { ...state.treeById };
      delete nextDetails[id];
      delete nextTrees[id];
      const isActive = state.activeId === id;
      return {
        activeId: isActive ? null : state.activeId,
        activeDetail: isActive ? null : state.activeDetail,
        sessionTree: isActive ? null : state.sessionTree,
        detailById: nextDetails,
        treeById: nextTrees,
      };
    });
  },

  loadSessions: async () => {
    set({ isLoading: true, error: null });
    try {
      const sessions = await api.listSessions();
      set((state) => {
        const validIds = new Set(sessions.map((session) => session.id));
        const pruned = pruneCachesBySessionIds(state.detailById, state.treeById, validIds);
        const activeStillExists = state.activeId ? validIds.has(state.activeId) : false;
        useChatStore.getState().pruneSessions(Array.from(validIds));
        return {
          sessions,
          isLoading: false,
          activeId: activeStillExists ? state.activeId : null,
          activeDetail: activeStillExists && state.activeId
            ? pruned.detailById[state.activeId] ?? state.activeDetail
            : null,
          sessionTree: activeStillExists && state.activeId
            ? pruned.treeById[state.activeId] ?? state.sessionTree
            : null,
          detailById: pruned.detailById,
          treeById: pruned.treeById,
        };
      });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Failed to load sessions";
      set({ error: msg, isLoading: false });
    }
  },

  openSession: async (id: string) => {
    const cachedDetail = get().detailById[id] || null;
    const cachedTree = get().treeById[id] || null;
    set({
      activeId: id,
      activeDetail: cachedDetail,
      sessionTree: cachedTree,
      isLoading: true,
      error: null,
    });
    try {
      const detail = await api.getSession(id);
      set((state) => ({
        detailById: { ...state.detailById, [id]: detail },
        activeId: state.activeId === id ? id : state.activeId,
        activeDetail: state.activeId === id ? detail : state.activeDetail,
        sessionTree: state.activeId === id ? (state.treeById[id] || null) : state.sessionTree,
        isLoading: state.activeId === id ? false : state.isLoading,
      }));
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Failed to open session";
      if (e instanceof ApiError && e.status === 404) {
        get().invalidateSessionLocally(id);
        useChatStore.getState().forgetSession(id);
        set({ error: msg, isLoading: false });
        void get().loadSessions();
        return;
      }
      set((state) => ({
        error: state.activeId === id ? msg : state.error,
        isLoading: state.activeId === id ? false : state.isLoading,
      }));
    }
  },

  createSession: async (agentName = "build", repoPath = ".", initialPlanFile) => {
    set({ isLoading: true, error: null });
    try {
      const resp = await api.createSession(agentName, repoPath, initialPlanFile);
      await get().openSession(resp.session_id);
      void get().loadSessions();
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
        const { activeId, detailById, treeById } = get();
        const nextDetails = { ...detailById };
        const nextTrees = { ...treeById };
        delete nextDetails[id];
        delete nextTrees[id];
        if (activeId === id) {
          set({ activeId: null, activeDetail: null, sessionTree: null, detailById: nextDetails, treeById: nextTrees });
        } else {
          set({ detailById: nextDetails, treeById: nextTrees });
        }
        await get().loadSessions();
        return true;
      }
      return false;
    } catch { return false; }
  },

  deleteSessionsBatch: async (ids: string[]) => {
    if (!ids.length) return 0;
    try {
      const resp = await api.deleteSessionsBatch(ids);
      if (resp.deleted_count > 0) {
        await get().loadSessions();
      }
      return resp.deleted_count;
    } catch { return 0; }
  },

  refreshActive: async () => {
    const requestedId = get().activeId;
    if (requestedId) {
      try {
        const detail = await api.getSession(requestedId);
        set((state) => ({
          activeDetail: state.activeId === requestedId ? detail : state.activeDetail,
          detailById: { ...state.detailById, [requestedId]: detail },
        }));
      } catch (e: unknown) {
        if (e instanceof ApiError && e.status === 404) {
          if (get().activeId === requestedId) {
            get().invalidateSessionLocally(requestedId);
            useChatStore.getState().forgetSession(requestedId);
            void get().loadSessions();
          }
        }
      }
    }
  },

  fetchSessionTree: async (id: string) => {
    try {
      const tree = await api.fetchSessionTree(id);
      set((state) => ({
        sessionTree: state.activeId === id ? tree : state.sessionTree,
        treeById: { ...state.treeById, [id]: tree },
      }));
    } catch (e: unknown) {
      if (e instanceof ApiError && e.status === 404) {
        get().invalidateSessionLocally(id);
        useChatStore.getState().forgetSession(id);
        void get().loadSessions();
        return;
      }
      set((state) => ({
        sessionTree: state.activeId === id ? null : state.sessionTree,
      }));
    }
  },
}));

registerSessionMissingHandler((sessionId) => {
  useSessionStore.getState().invalidateSessionLocally(sessionId);
});
