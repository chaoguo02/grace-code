import { create } from "zustand";
import type { Message, TimelineItem, WsMessage } from "../types";
import * as api from "../api/sessions";
import { ApiError } from "../api/client";

let sessionMissingHandler: ((sessionId: string) => void) | null = null;

export interface PlanApproval {
  planText: string;
  isWaiting: boolean;
  sessionId: string;
  contract?: Record<string, unknown> | null;
  revision?: number;
  maxRevisions?: number;
}

export interface ToolApproval {
  requestId: string;
  toolName: string;
  params: Record<string, unknown>;
  thought?: string;
  decisionReason?: string;
  toolUseId?: string;
  permissionMode?: string;
  riskLevel?: string;
}

export interface BackgroundAgentState {
  childSessionId: string;
  agentName: string;
  status: string;
  toolCount: number;
  lastAction: string;
  _completedAt?: number;
}

export interface SessionUiState {
  timeline: TimelineItem[];
  events: WsMessage[];
  isRunning: boolean;
  steps: number;
  tokens: number;
  error: string | null;
  planApproval: PlanApproval | null;
  toolApprovals: Record<string, ToolApproval>;
  currentMode: string;
  currentModel: string;
  viewingChildSessionId: string | null;
  backgroundAgents: Record<string, BackgroundAgentState>;
  worktreeStates: Record<string, string>;
  /** Per-session draft text — survives tab switches. */
  draft: string;
  /** Accumulated thought_delta text during live streaming. Cleared on full thought. */
  streamingThought: string;
}

interface ChatState {
  sessionStateById: Record<string, SessionUiState>;
  ws: WebSocket | null;
  wsConnected: boolean;
  wsCloseInfo: string;
  _wsSessionId: string | null;
  _wsRetries: number;

  setMessages: (msgs: Message[], sessionId?: string) => void;
  handleWsEvent: (ev: WsMessage) => void;
  clearEvents: () => void;
  clear: (sessionId?: string | null) => void;
  forgetSession: (sessionId: string) => void;
  pruneSessions: (validSessionIds: string[]) => void;
  sendChat: (sessionId: string, prompt: string, intent?: string) => Promise<void>;
  loadMessages: (sessionId: string, signal?: AbortSignal) => Promise<void>;
  loadTraceEvents: (sessionId: string, signal?: AbortSignal) => Promise<void>;
  connectWs: (sessionId: string) => void;
  disconnectWs: () => void;
  approvePlan: (sessionId?: string | null, comment?: string) => Promise<void>;
  rejectPlan: (sessionId?: string | null, reason?: string) => Promise<void>;
  savePlan: (sessionId?: string | null) => Promise<void>;
  abortPlan: (sessionId?: string | null) => Promise<void>;
  clearPlanApproval: () => void;
  resolveToolApproval: (
    requestId: string,
    decision: "allow" | "deny",
    opts?: { note?: string; always?: boolean }
  ) => Promise<void>;
  setDraft: (text: string, sessionId?: string | null) => void;
  setMode: (mode: string, sessionId?: string | null) => void;
  switchModel: (model: string, provider?: string, sessionId?: string | null) => Promise<void>;
  compactSession: (sessionId?: string | null) => Promise<boolean>;
  setViewingChild: (id: string | null, sessionId?: string | null) => void;
  restorePlanFromDetail: (sessionId: string, detail: { agent_name: string; summary?: string; metadata?: Record<string, unknown> }) => void;
}

export function createEmptySessionUiState(): SessionUiState {
  return {
    timeline: [],
    events: [],
    isRunning: false,
    steps: 0,
    tokens: 0,
    error: null,
    planApproval: null,
    toolApprovals: {},
    currentMode: "build",
    currentModel: "",
    viewingChildSessionId: null,
    backgroundAgents: {},
    worktreeStates: {},
    draft: "",
    streamingThought: "",
  };
}

const EMPTY_SESSION_UI_STATE = createEmptySessionUiState();

function getSessionUiSnapshot(
  state: Pick<ChatState, "sessionStateById">,
  sessionId?: string | null,
): SessionUiState {
  if (!sessionId) return EMPTY_SESSION_UI_STATE;
  return state.sessionStateById[sessionId] ?? EMPTY_SESSION_UI_STATE;
}

export function selectSessionUi(
  state: ChatState,
  sessionId?: string | null,
): SessionUiState {
  return getSessionUiSnapshot(state, sessionId);
}

export function selectCurrentSessionUi(state: ChatState): SessionUiState {
  return getSessionUiSnapshot(state, state._wsSessionId);
}

export function registerSessionMissingHandler(
  handler: ((sessionId: string) => void) | null,
): void {
  sessionMissingHandler = handler;
}

export const useChatStore = create<ChatState>((set, get) => {
  const resolveSessionId = (sessionId?: string | null): string | null => {
    if (sessionId) return sessionId;
    return get()._wsSessionId;
  };

  const ensureSession = (sessionId: string): SessionUiState => {
    const existing = get().sessionStateById[sessionId];
    if (existing) return existing;
    const fresh = createEmptySessionUiState();
    set((state) => ({
      sessionStateById: { ...state.sessionStateById, [sessionId]: fresh },
    }));
    return fresh;
  };

  const patchSession = (
    sessionId: string,
    updater: (prev: SessionUiState) => SessionUiState,
  ) => {
    set((state) => {
      const prev = state.sessionStateById[sessionId] ?? createEmptySessionUiState();
      return {
        sessionStateById: {
          ...state.sessionStateById,
          [sessionId]: updater(prev),
        },
      };
    });
  };

  const invalidateSession = (
    sessionId: string,
    opts?: { notifySessionStore?: boolean },
  ) => {
    const { ws, _wsSessionId } = get();
    const isActive = _wsSessionId === sessionId;
    if (isActive && ws) {
      ws.close();
    }
    set((state) => {
      const next = { ...state.sessionStateById };
      delete next[sessionId];
      return {
        sessionStateById: next,
        ws: isActive ? null : state.ws,
        wsConnected: isActive ? false : state.wsConnected,
        wsCloseInfo: isActive ? "" : state.wsCloseInfo,
        _wsSessionId: isActive ? null : state._wsSessionId,
        _wsRetries: isActive ? 0 : state._wsRetries,
      };
    });
    if (opts?.notifySessionStore !== false) {
      sessionMissingHandler?.(sessionId);
    }
  };

  return {
    sessionStateById: {},
    ws: null,
    wsConnected: false,
    wsCloseInfo: "",
    _wsSessionId: null,
    _wsRetries: 0,

    setMessages: (msgs, sessionId) => {
      const sid = sessionId || get()._wsSessionId;
      if (!sid) return;
      patchSession(sid, (prev) => ({
        ...prev,
        timeline: msgs.map((m) => ({ source: "message" as const, msg: m })),
      }));
    },

    handleWsEvent: (ev) => {
      const sid = get()._wsSessionId;
      if (!sid) return;
      const session = ensureSession(sid);

      if (ev.type === "status") {
        if (ev.status === "running") {
          patchSession(sid, (prev) => ({ ...prev, isRunning: true, error: null }));
        } else if (ev.status === "completed") {
          patchSession(sid, (prev) => ({
            ...prev,
            isRunning: false,
            steps: ev.result?.steps_taken ?? prev.steps,
            tokens: ev.result?.total_tokens ?? prev.tokens,
            planApproval: null,
            streamingThought: "",
          }));
          return;
        } else if (ev.status === "failed") {
          patchSession(sid, (prev) => ({
            ...prev,
            isRunning: false,
            error: ev.error || "Execution failed",
            planApproval: null,
            streamingThought: "",
          }));
          return;
        } else if (ev.status === "finish" || ev.status === "gave_up") {
          patchSession(sid, (prev) => ({
            ...prev,
            isRunning: false,
            timeline: ev.message ? [...prev.timeline, { source: "ws" as const, ws: ev }] : prev.timeline,
          }));
          return;
        }
      }

      if (ev.type === "approval_required") {
        const rid = ev.request_id || "";
        patchSession(sid, (prev) => ({
          ...prev,
          toolApprovals: {
            ...prev.toolApprovals,
            [rid]: {
              requestId: rid,
              toolName: ev.tool_name || "",
              params: (ev.params || {}) as Record<string, unknown>,
              thought: ev.thought || "",
              decisionReason: ev.decision_reason,
              toolUseId: ev.tool_use_id,
              permissionMode: ev.permission_mode,
              riskLevel: ev.risk_level,
            },
          },
          timeline: [...prev.timeline, { source: "ws" as const, ws: ev }],
        }));
        return;
      }

      if (ev.type === "worktree_resolved") {
        const csid = ev.child_session_id || "";
        patchSession(sid, (prev) => {
          const nextAgents = { ...prev.backgroundAgents };
          if (nextAgents[csid]) {
            nextAgents[csid] = {
              ...nextAgents[csid],
              status: "completed",
              lastAction: `worktree ${ev.action}: ${ev.status}`,
            };
          }
          return {
            ...prev,
            backgroundAgents: nextAgents,
            worktreeStates: {
              ...prev.worktreeStates,
              [`${csid}_${ev.action}`]: ev.status || "error",
            },
            timeline: [...prev.timeline, { source: "ws" as const, ws: ev }],
          };
        });
        return;
      }

      if (ev.type === "approval_timeout") {
        const rid = ev.request_id || "";
        patchSession(sid, (prev) => {
          const nextApprovals = { ...prev.toolApprovals };
          delete nextApprovals[rid];
          return { ...prev, toolApprovals: nextApprovals };
        });
        return;
      }

      if (ev.type === "plan_ready") {
        patchSession(sid, (prev) => ({
          ...prev,
          isRunning: false,
          steps: ev.result?.steps_taken ?? session.steps,
          tokens: ev.result?.total_tokens ?? session.tokens,
          planApproval: {
            planText: ev.plan_text || ev.result?.summary || "",
            isWaiting: true,
            sessionId: sid,
            contract: (ev.contract || null) as Record<string, unknown> | null,
            revision: typeof ev.revision === "number" ? ev.revision : 0,
            maxRevisions: typeof ev.max_revisions === "number" ? ev.max_revisions : 5,
          },
          timeline: [...prev.timeline, { source: "ws" as const, ws: ev }],
        }));
        return;
      }

      if (ev.type === "subagent_start") {
        const csid = ev.child_session_id || "";
        patchSession(sid, (prev) => ({
          ...prev,
          backgroundAgents: {
            ...prev.backgroundAgents,
            [csid]: {
              childSessionId: csid,
              agentName: ev.agent_name || "agent",
              status: "running",
              toolCount: 0,
              lastAction: "",
            },
          },
        }));
      }

      if (ev.type === "subagent_stop") {
        const csid = ev.child_session_id || "";
        patchSession(sid, (prev) => {
          const nextAgents = { ...prev.backgroundAgents };
          if (nextAgents[csid]) {
            nextAgents[csid] = {
              ...nextAgents[csid],
              status: ev.status || "completed",
              _completedAt: Date.now(),
            };
          }
          const now = Date.now();
          for (const key of Object.keys(nextAgents)) {
            if (
              nextAgents[key].status !== "running" &&
              now - (nextAgents[key]._completedAt || 0) > 300000
            ) {
              delete nextAgents[key];
            }
          }
          return { ...prev, backgroundAgents: nextAgents };
        });
      }

      if (ev.type === "tool_call") {
        const childId = (ev as { child_session_id?: string }).child_session_id || "";
        // Only attribute to a child agent when child_session_id is set
        // and precisely matches a running background agent.
        // No fallback — misattribution is worse than no attribution.
        if (childId) {
          patchSession(sid, (prev) => {
            const agent = prev.backgroundAgents[childId];
            if (!agent || agent.status !== "running") return prev;
            const updated = { ...prev.backgroundAgents };
            updated[childId] = {
              ...agent,
              toolCount: agent.toolCount + 1,
              lastAction: ev.name || "",
            };
            return { ...prev, backgroundAgents: updated };
          });
        }
      }

      // Streaming thought deltas: accumulate into streamingThought buffer.
      // A full "thought" event clears the buffer (the complete text is in the timeline).
      if (ev.type === "thought_delta") {
        const deltaText = (ev as { text?: string }).text || "";
        if (deltaText) {
          patchSession(sid, (prev) => ({
            ...prev,
            streamingThought: prev.streamingThought + deltaText,
          }));
        }
        // Don't add deltas to timeline — they're rendered in-place.
        return;
      }

      // Full thought/reflection: clear the streaming buffer.
      if (ev.type === "thought" || ev.type === "reflection") {
        patchSession(sid, (prev) => ({ ...prev, streamingThought: "" }));
      }

      if (
        ev.type === "thought" ||
        ev.type === "tool_call" ||
        ev.type === "observation" ||
        ev.type === "reflection" ||
        ev.type === "subagent_start" ||
        ev.type === "subagent_stop"
      ) {
        patchSession(sid, (prev) => ({
          ...prev,
          timeline: [...prev.timeline, { source: "ws" as const, ws: ev }],
        }));
      }

      patchSession(sid, (prev) => ({
        ...prev,
        events: [ev, ...prev.events].slice(0, 100),
      }));
    },

    clearEvents: () => {
      const sid = get()._wsSessionId;
      if (!sid) return;
      patchSession(sid, (prev) => ({ ...prev, events: [] }));
    },

    clear: (sessionId) => {
      const sid = resolveSessionId(sessionId);
      if (!sid) return;
      patchSession(sid, (prev) => ({
        ...createEmptySessionUiState(),
        currentMode: prev.currentMode,
        currentModel: prev.currentModel,
      }));
    },

    forgetSession: (sessionId) =>
      invalidateSession(sessionId),

    pruneSessions: (validSessionIds) => {
      const validIds = new Set(validSessionIds);
      const { ws, _wsSessionId } = get();
      const activeRemoved = _wsSessionId && !validIds.has(_wsSessionId);
      if (activeRemoved && ws) {
        ws.close();
      }
      set((state) => {
        const nextEntries = Object.fromEntries(
          Object.entries(state.sessionStateById).filter(([id]) => validIds.has(id)),
        );
        return {
          sessionStateById: nextEntries,
          ws: activeRemoved ? null : state.ws,
          wsConnected: activeRemoved ? false : state.wsConnected,
          wsCloseInfo: activeRemoved ? "" : state.wsCloseInfo,
          _wsSessionId: activeRemoved ? null : state._wsSessionId,
          _wsRetries: activeRemoved ? 0 : state._wsRetries,
        };
      });
    },

    sendChat: async (sessionId, prompt, intent) => {
      if (get()._wsSessionId !== sessionId) return;
      ensureSession(sessionId);
      patchSession(sessionId, (prev) => ({
        ...prev,
        isRunning: true,
        error: null,
        // Only clear planApproval if it was already resolved (not waiting).
        // Preserve it when user is sending feedback while plan is still pending.
        planApproval: prev.planApproval?.isWaiting ? prev.planApproval : null,
      }));
      const watchdog = setTimeout(() => {
        const current = selectSessionUi(get(), sessionId);
        if (current.isRunning) {
          patchSession(sessionId, (prev) => ({
            ...prev,
            isRunning: false,
            error: "Request timed out after 30 minutes",
          }));
        }
      }, 30 * 60 * 1000);
      try {
        if (get()._wsSessionId !== sessionId) return;
        const userMsg: Message = { role: "user", content: prompt };
        patchSession(sessionId, (prev) => ({
          ...prev,
          timeline: [...prev.timeline, { source: "message" as const, msg: userMsg }],
        }));
        if (get()._wsSessionId !== sessionId) return;
        const { currentMode } = selectSessionUi(get(), sessionId);
        await api.chat(sessionId, prompt, intent, currentMode);
      } catch (e: unknown) {
        if (e instanceof ApiError && e.status === 404) {
          invalidateSession(sessionId);
          return;
        }
        const msg = e instanceof Error ? e.message : "Chat failed";
        patchSession(sessionId, (prev) => ({ ...prev, error: msg, isRunning: false }));
      } finally {
        clearTimeout(watchdog);
      }
    },

    setDraft: (text, sessionId) => {
      const sid = resolveSessionId(sessionId);
      if (!sid) return;
      patchSession(sid, (prev) => ({ ...prev, draft: text }));
    },

    setMode: (mode, sessionId) => {
      const sid = resolveSessionId(sessionId);
      if (!sid) return;
      patchSession(sid, (prev) => ({ ...prev, currentMode: mode }));
    },

    switchModel: async (model, provider, sessionId) => {
      const sid = resolveSessionId(sessionId);
      if (!sid) return;
      patchSession(sid, (prev) => ({ ...prev, currentModel: model }));
      try {
        await api.updateSessionModel(sid, { model, provider });
      } catch (e: unknown) {
        if (e instanceof ApiError && e.status === 404) {
          invalidateSession(sid);
          return;
        }
        patchSession(sid, (prev) => ({
          ...prev,
          currentModel: "",
          error: e instanceof Error ? e.message : "Switch model failed",
        }));
      }
    },

    setViewingChild: (id, sessionId) => {
      const sid = resolveSessionId(sessionId);
      if (!sid) return;
      patchSession(sid, (prev) => ({ ...prev, viewingChildSessionId: id }));
    },

    restorePlanFromDetail: (sessionId, detail) => {
      const session = get().sessionStateById[sessionId];
      // Only restore if there's no active planApproval and the session
      // is a completed plan session with a summary (plan text available).
      if (
        detail.agent_name === "plan" &&
        detail.summary &&
        detail.summary.trim() &&
        (!session || !session.planApproval?.isWaiting)
      ) {
        const revision = (detail.metadata?.plan_revision as number) ?? 0;
        patchSession(sessionId, (prev) => ({
          ...prev,
          planApproval: {
            planText: detail.summary!,
            isWaiting: true,
            sessionId,
            contract: null,
            revision,
            maxRevisions: 5,
          },
        }));
      }
    },

    compactSession: async (sessionId) => {
      const sid = resolveSessionId(sessionId);
      if (!sid) return false;
      try {
        await api.compactSession(sid);
        return true;
      } catch (e: unknown) {
        if (e instanceof ApiError && e.status === 404) {
          invalidateSession(sid);
          return false;
        }
        patchSession(sid, (prev) => ({
          ...prev,
          error: e instanceof Error ? e.message : "Compact session failed",
        }));
        return false;
      }
    },

    loadMessages: async (sessionId, signal) => {
      try {
        ensureSession(sessionId);
        const msgs = await api.getMessages(sessionId, signal);
        patchSession(sessionId, (prev) => {
          const traces = prev.timeline.filter((item) => item.source === "ws");
          const msgItems = msgs.map((m) => ({ source: "message" as const, msg: m }));
          return { ...prev, timeline: [...traces, ...msgItems] };
        });
      } catch (e: unknown) {
        if (e instanceof ApiError && e.status === 404) {
          invalidateSession(sessionId);
        }
      }
    },

    loadTraceEvents: async (sessionId, signal) => {
      try {
        ensureSession(sessionId);
        const events = await api.getTraceEvents(sessionId, 0, 200, signal);
        patchSession(sessionId, (prev) => {
          const msgs = prev.timeline.filter((item) => item.source === "message");
          const wsItems = events.map((ws) => ({ source: "ws" as const, ws }));

          // Restore planApproval from plan_ready events in the trace log.
          // This recovers the approve/reject UI after page refresh.
          const planEvent = events.find((e) => e.type === "plan_ready");
          const planRaw = planEvent as unknown as Record<string, unknown> | undefined;
          const restoredPlanApproval = planRaw && !prev.planApproval?.isWaiting
            ? {
                planText: (planRaw.plan_text as string) || "",
                isWaiting: true,
                sessionId,
                contract: (planRaw.contract || null) as Record<string, unknown> | null,
                revision: (planRaw.revision as number) ?? 0,
                maxRevisions: (planRaw.max_revisions as number) ?? 5,
              }
            : prev.planApproval;

          return {
            ...prev,
            events: events.slice().reverse().slice(0, 100),
            timeline: [...wsItems, ...msgs],
            planApproval: restoredPlanApproval,
          };
        });
      } catch (e: unknown) {
        if (e instanceof ApiError && e.status === 404) {
          invalidateSession(sessionId);
        }
      }
    },

    connectWs: (sessionId) => {
      get().disconnectWs();
      ensureSession(sessionId);
      patchSession(sessionId, (prev) => ({ ...prev, error: null }));
      set({
        wsCloseInfo: "",
        _wsSessionId: sessionId,
        _wsRetries: 0,
      });

      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${proto}//${window.location.host}/api/ws/sessions/${sessionId}`;
      const ws = new WebSocket(url);

      ws.onopen = () => {
        if (get()._wsSessionId !== sessionId) return;
        set({ wsConnected: true, wsCloseInfo: "" });
        patchSession(sessionId, (prev) => ({ ...prev, error: null }));
      };

      ws.onmessage = (ev) => {
        try {
          if (get()._wsSessionId !== sessionId) return;
          const raw = JSON.parse(ev.data) as Record<string, unknown>;
          if (raw.type === "pong") return;
          get().handleWsEvent(raw as unknown as WsMessage);
        } catch {
          // ignore malformed events
        }
      };

      ws.onerror = () => {
        if (get()._wsSessionId !== sessionId) return;
        set({ wsConnected: false });
      };

      ws.onclose = (ev) => {
        if (get()._wsSessionId !== sessionId) return;
        const info = `code=${ev.code}${ev.reason ? ` reason=${ev.reason}` : ""}`;
        const isAbnormal = ev.code !== 1000 && ev.code !== 1001;
        set({
          ws: null,
          wsConnected: false,
          wsCloseInfo: info,
        });
        if (isAbnormal) {
          patchSession(sessionId, (prev) => ({
            ...prev,
            error: prev.error || `WS closed: ${info}`,
          }));
          const retries = get()._wsRetries || 0;
          if (retries < 5) {
            const delay = Math.min(1000 * Math.pow(2, retries), 16000);
            set({ _wsRetries: retries + 1 });
            patchSession(sessionId, (prev) => ({
              ...prev,
              error: `Reconnecting in ${delay / 1000}s...`,
            }));
            setTimeout(() => {
              if (get()._wsSessionId !== sessionId) return;
              void api.getSession(sessionId)
                .then(() => {
                  if (get()._wsSessionId === sessionId) {
                    get().connectWs(sessionId);
                  }
                })
                .catch((e: unknown) => {
                  if (e instanceof ApiError && e.status === 404) {
                    invalidateSession(sessionId);
                    return;
                  }
                  if (get()._wsSessionId === sessionId) {
                    get().connectWs(sessionId);
                  }
                });
            }, delay);
          } else {
            set({ _wsRetries: 0 });
            patchSession(sessionId, (prev) => ({
              ...prev,
              error: "WebSocket connection lost - please refresh",
            }));
          }
        } else {
          set({ _wsRetries: 0 });
        }
      };

      set({ ws, _wsSessionId: sessionId, _wsRetries: 0 });
    },

    disconnectWs: () => {
      const { ws } = get();
      if (ws) {
        ws.close();
      }
      set({ ws: null, wsConnected: false });
    },

    approvePlan: async (sessionId, comment) => {
      const sid = resolveSessionId(sessionId);
      const { planApproval } = selectSessionUi(get(), sid);
      if (!sid || !planApproval || !planApproval.isWaiting) return;
      try {
        patchSession(sid, (prev) => ({
          ...prev,
          isRunning: true,
          planApproval: { ...planApproval, isWaiting: false },
        }));
        await api.approveSession(sid, comment);
      } catch (e: unknown) {
        if (e instanceof ApiError && e.status === 404) {
          invalidateSession(sid);
          return;
        }
        const msg = e instanceof Error ? e.message : "Approval failed";
        patchSession(sid, (prev) => ({
          ...prev,
          error: msg,
          isRunning: false,
          planApproval: prev.planApproval
            ? { ...prev.planApproval, isWaiting: true }
            : prev.planApproval,
        }));
      }
    },

    rejectPlan: async (sessionId, reason = "Please revise the plan") => {
      const sid = resolveSessionId(sessionId);
      const { planApproval } = selectSessionUi(get(), sid);
      if (!sid || !planApproval || !planApproval.isWaiting) return;
      try {
        patchSession(sid, (prev) => ({
          ...prev,
          isRunning: true,
          planApproval: { ...planApproval, isWaiting: false },
        }));
        await api.rejectSession(sid, reason);
      } catch (e: unknown) {
        if (e instanceof ApiError && e.status === 404) {
          invalidateSession(sid);
          return;
        }
        const msg = e instanceof Error ? e.message : "Rejection failed";
        patchSession(sid, (prev) => ({
          ...prev,
          error: msg,
          isRunning: false,
          planApproval: prev.planApproval
            ? { ...prev.planApproval, isWaiting: true }
            : prev.planApproval,
        }));
      }
    },

    savePlan: async (sessionId) => {
      const sid = resolveSessionId(sessionId);
      const { planApproval } = selectSessionUi(get(), sid);
      if (!sid || !planApproval || !planApproval.isWaiting) return;
      try {
        patchSession(sid, (prev) => ({
          ...prev,
          isRunning: true,
          planApproval: { ...planApproval, isWaiting: false },
        }));
        await api.savePlan(sid);
      } catch (e: unknown) {
        if (e instanceof ApiError && e.status === 404) {
          invalidateSession(sid);
          return;
        }
        const msg = e instanceof Error ? e.message : "Save failed";
        patchSession(sid, (prev) => ({
          ...prev,
          error: msg,
          isRunning: false,
          planApproval: prev.planApproval
            ? { ...prev.planApproval, isWaiting: true }
            : prev.planApproval,
        }));
      }
    },

    abortPlan: async (sessionId) => {
      const sid = resolveSessionId(sessionId);
      const { planApproval } = selectSessionUi(get(), sid);
      if (!sid || !planApproval || !planApproval.isWaiting) return;
      try {
        patchSession(sid, (prev) => ({
          ...prev,
          isRunning: true,
          planApproval: { ...planApproval, isWaiting: false },
        }));
        await api.abortPlan(sid);
        patchSession(sid, (prev) => ({ ...prev, planApproval: null }));
      } catch (e: unknown) {
        if (e instanceof ApiError && e.status === 404) {
          invalidateSession(sid);
          return;
        }
        const msg = e instanceof Error ? e.message : "Abort failed";
        patchSession(sid, (prev) => ({
          ...prev,
          error: msg,
          isRunning: false,
          planApproval: prev.planApproval
            ? { ...prev.planApproval, isWaiting: true }
            : prev.planApproval,
        }));
      }
    },

    clearPlanApproval: () => {
      const sid = get()._wsSessionId;
      if (!sid) return;
      patchSession(sid, (prev) => ({ ...prev, planApproval: null }));
    },

    resolveToolApproval: async (requestId, decision, opts) => {
      const sid = get()._wsSessionId;
      if (!sid) return;
      const snapshot = selectSessionUi(get(), sid).toolApprovals[requestId];
      if (!snapshot) return;

      patchSession(sid, (prev) => {
        const next = { ...prev.toolApprovals };
        delete next[requestId];
        return { ...prev, toolApprovals: next };
      });

      try {
        await api.resolveToolApproval(sid, {
          request_id: requestId,
          decision,
          note: opts?.note || "",
          always: opts?.always || false,
        });
      } catch (e: unknown) {
        if (e instanceof ApiError && e.status === 404) {
          invalidateSession(sid);
          return;
        }
        patchSession(sid, (prev) => ({
          ...prev,
          toolApprovals: { ...prev.toolApprovals, [requestId]: snapshot },
          error: e instanceof Error
            ? e.message.slice(0, 100)
            : `Approval failed: ${String(e).slice(0, 80)}`,
        }));
      }
    },
  };
});
