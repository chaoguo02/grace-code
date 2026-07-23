import { create } from "zustand";
import type { Message, TimelineItem, WsMessage } from "../types";
import * as api from "../api/sessions";
import { ApiError } from "../api/client";
import { connectWebSocket, disconnectWebSocket, scheduleReconnect } from "../hooks/useWebSocket";

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
  /** Context window max tokens (from model config). Updated on session load. */
  contextTotal: number;
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
    contextTotal: 200000,  // default for deepseek-v4 / large models
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

const CHAT_TIMEOUT_MS = 30 * 60 * 1000;  // 30 minutes
let _watchdogTimer: ReturnType<typeof setTimeout> | null = null;

// Lightweight event dedup: tracks fingerprints of recently seen timeline events.
// Capped at 200 entries to bound memory.
const _seenFingerprints = new Set<string>();

function _eventFingerprint(ev: WsMessage): string | null {
  // Only fingerprint events that go into the timeline.
  // thought_delta is intentionally cumulative — never deduped.
  if (ev.type === "thought_delta") return null;
  const step = (ev as { step?: number }).step ?? 0;
  switch (ev.type) {
    case "tool_call":    return `tc:${step}:${ev.name || ""}`;
    case "observation":  return `ob:${step}:${ev.tool_name || ""}`;
    case "thought":      return `th:${step}:${(ev.content || "").slice(0, 40)}`;
    case "reflection":   return `rf:${step}:${(ev.content || "").slice(0, 40)}`;
    case "status":       return `st:${step}:${ev.status || ""}`;
    case "subagent_start": return `sa:${step}:${ev.child_session_id || ""}`;
    case "subagent_stop":  return `ss:${step}:${ev.child_session_id || ""}`;
    case "plan_ready":   return `pr:${step}`;
    case "approval_required": return `ar:${ev.request_id || ""}`;
    default:             return `${ev.type}:${step}`;
  }
}

function clearWatchdog() {
  if (_watchdogTimer) {
    clearTimeout(_watchdogTimer);
    _watchdogTimer = null;
  }
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
          clearWatchdog();
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
          clearWatchdog();
          patchSession(sid, (prev) => ({
            ...prev,
            isRunning: false,
            error: ev.error || "Execution failed",
            planApproval: null,
            streamingThought: "",
          }));
          return;
        } else if (ev.status === "finish" || ev.status === "gave_up") {
          clearWatchdog();
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

      // Dedup: skip timeline append if this event is a duplicate of the
      // last one added.  Uses a lightweight fingerprint (type+step+key field).
      // Prevents flicker from WS reconnect / replayed events.
      const _fp = _eventFingerprint(ev);
      const _isDup = _fp !== null && _seenFingerprints.has(_fp);

      if (
        !_isDup &&
        (ev.type === "thought" ||
        ev.type === "tool_call" ||
        ev.type === "observation" ||
        ev.type === "reflection" ||
        ev.type === "subagent_start" ||
        ev.type === "subagent_stop")
      ) {
        if (_fp !== null) {
          _seenFingerprints.add(_fp);
          if (_seenFingerprints.size > 200) {
            // Keep the set bounded — evict oldest 50 entries
            const iter = _seenFingerprints.values();
            for (let i = 0; i < 50; i++) { const v = iter.next().value; if (v) _seenFingerprints.delete(v); }
          }
        }
        patchSession(sid, (prev) => ({
          ...prev,
          timeline: [...prev.timeline, { source: "ws" as const, ws: ev }],
        }));
      }

      // Append to raw event log (always — this is the canonical event stream).
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
      clearWatchdog();  // clear any stale timer from a previous run
      _watchdogTimer = setTimeout(() => {
        const current = selectSessionUi(get(), sessionId);
        if (current.isRunning) {
          patchSession(sessionId, (prev) => ({
            ...prev,
            isRunning: false,
            error: `Request timed out after ${CHAT_TIMEOUT_MS / 60000} minutes`,
          }));
        }
      }, CHAT_TIMEOUT_MS);
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
        // api.chat() returned OK — keep watchdog alive; WS events will clear it on completion
      } catch (e: unknown) {
        clearWatchdog();  // network error — no WS events will follow
        if (e instanceof ApiError && e.status === 404) {
          invalidateSession(sessionId);
          return;
        }
        const msg = e instanceof Error ? e.message : "Chat failed";
        patchSession(sessionId, (prev) => ({ ...prev, error: msg, isRunning: false }));
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
          const wsItems = events.map((ws) => ({ source: "ws" as const, ws }));
          const msgItems = prev.timeline.filter((item) => item.source === "message");

          // Merge ws events and messages by timestamp — chronological order.
          const merged = [...wsItems, ...msgItems].sort((a, b) => {
            const aTs = a.source === "ws"
              ? (a.ws as { timestamp?: string }).timestamp || ""
              : a.source === "message" && a.msg?.created_at
                ? a.msg.created_at
                : "";
            const bTs = b.source === "ws"
              ? (b.ws as { timestamp?: string }).timestamp || ""
              : b.source === "message" && b.msg?.created_at
                ? b.msg.created_at
                : "";
            return aTs.localeCompare(bTs);
          });

          // Restore planApproval from plan_ready events in the trace log.
          const planEvent = events.find((e) => e.type === "plan_ready");
          const restoredPlanApproval = !prev.planApproval?.isWaiting
            && planEvent
            && typeof planEvent === "object"
            && "plan_text" in planEvent
            ? {
                planText: String((planEvent as unknown as Record<string,unknown>).plan_text || ""),
                isWaiting: true,
                sessionId,
                contract: ((planEvent as unknown as Record<string,unknown>).contract || null) as Record<string, unknown> | null,
                revision: Number((planEvent as unknown as Record<string,unknown>).revision) || 0,
                maxRevisions: Number((planEvent as unknown as Record<string,unknown>).max_revisions) || 5,
              }
            : prev.planApproval;

          return {
            ...prev,
            events: events.slice().reverse().slice(0, 100),
            timeline: merged,
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

      connectWebSocket(sessionId, {
        onOpen: () => {
          if (get()._wsSessionId !== sessionId) return;
          set({ wsConnected: true, wsCloseInfo: "" });
          patchSession(sessionId, (prev) => ({ ...prev, error: null }));
        },
        onMessage: (ev) => {
          if (get()._wsSessionId !== sessionId) return;
          get().handleWsEvent(ev);
        },
        onError: () => {
          if (get()._wsSessionId !== sessionId) return;
          set({ wsConnected: false });
        },
        onClose: (info, isAbnormal) => {
          if (get()._wsSessionId !== sessionId) return;
          set({ ws: null, wsConnected: false, wsCloseInfo: info });
          if (isAbnormal) {
            patchSession(sessionId, (prev) => ({
              ...prev,
              error: prev.error || `WS closed: ${info}`,
            }));
            const retries = get()._wsRetries || 0;
            if (retries < 5) {
              set({ _wsRetries: retries + 1 });
              patchSession(sessionId, (prev) => ({
                ...prev,
                error: `Reconnecting in ${Math.min(1000 * Math.pow(2, retries), 16000) / 1000}s...`,
              }));
              scheduleReconnect(sessionId, retries, (sid) => {
                if (get()._wsSessionId !== sid) return;
                void api.getSession(sid)
                  .then(() => { if (get()._wsSessionId === sid) get().connectWs(sid); })
                  .catch((e: unknown) => {
                    if (e instanceof ApiError && e.status === 404) {
                      invalidateSession(sid);
                      return;
                    }
                    if (get()._wsSessionId === sid) get().connectWs(sid);
                  });
              });
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
        },
        reconnect: (sid) => { if (get()._wsSessionId === sid) get().connectWs(sid); },
      });
    },

    disconnectWs: () => {
      clearWatchdog();
      disconnectWebSocket();
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
