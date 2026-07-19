import { create } from "zustand";
import type { Message, WsMessage, TimelineItem } from "../types";
import * as api from "../api/sessions";

export interface PlanApproval {
  planText: string;
  isWaiting: boolean;
  sessionId: string;
  contract?: Record<string, unknown> | null;
  revision?: number;
  maxRevisions?: number;
}

/** A pending tool approval — CC control_request equivalent. */
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

interface ChatState {
  /** Timeline: persisted messages + live WS events */
  timeline: TimelineItem[];
  /** Compact event list for EventSidebar */
  events: WsMessage[];
  isRunning: boolean;
  steps: number;
  tokens: number;
  error: string | null;
  ws: WebSocket | null;
  /** Is the WebSocket currently connected? */
  wsConnected: boolean;
  /** Last WS close code + reason (for diagnostics) */
  wsCloseInfo: string;
  /** Internal: the session ID the current WS is connected to */
  _wsSessionId: string;
  /** Plan approval state (set when plan_ready event arrives) */
  planApproval: PlanApproval | null;
  /** Pending tool approvals keyed by request_id (supports concurrent batch) */
  toolApprovals: Record<string, ToolApproval>;

  setMessages: (msgs: Message[]) => void;
  handleWsEvent: (ev: WsMessage) => void;
  clearEvents: () => void;
  clear: () => void;
  /** Submit chat (async — returns immediately, events come via WS) */
  sendChat: (sessionId: string, prompt: string, intent?: string) => Promise<void>;
  /** Load persisted messages for a past session */
  loadMessages: (sessionId: string) => Promise<void>;
  /** Load historical WS-format trace events for a past session */
  loadTraceEvents: (sessionId: string) => Promise<void>;
  connectWs: (sessionId: string) => void;
  disconnectWs: () => void;
  /** Approve the current plan and trigger build */
  approvePlan: (comment?: string) => Promise<void>;
  /** Reject the current plan and request revision */
  rejectPlan: (reason: string) => Promise<void>;
  /** Clear plan approval state */
  clearPlanApproval: () => void;
  /** Resolve a pending tool approval (Allow/Deny/Always Allow). */
  resolveToolApproval: (requestId: string, decision: "allow" | "deny", opts?: { note?: string; always?: boolean }) => Promise<void>;
  /** Current session mode/agent_name */
  currentMode: string;
  /** Current LLM model */
  currentModel: string;
  /** Set the mode for the current session */
  setMode: (mode: string) => void;
  /** Switch the LLM model mid-session */
  switchModel: (model: string, provider?: string) => Promise<void>;
  /** Compact the current session's context */
  compactSession: () => Promise<boolean>;
  /** Currently viewed child session (null = main timeline) */
  viewingChildSessionId: string | null;
  /** Set the child session to view in SubagentDetail overlay */
  setViewingChild: (id: string | null) => void;
  /** Background subagent progress entries */
  backgroundAgents: Record<string, { childSessionId: string; agentName: string; status: string; toolCount: number; lastAction: string }>;
  /** Worktree resolution states: key="{childId}_{action}" → "applied"|"discarded"|"error" */
  _worktreeStates: Record<string, string>;
}

export const useChatStore = create<ChatState>((set, get) => ({
  timeline: [],
  events: [],
  isRunning: false,
  steps: 0,
  tokens: 0,
  error: null,
  ws: null,
  wsConnected: false,
  wsCloseInfo: "",
  _wsSessionId: "",
  planApproval: null,
  toolApprovals: {},
  currentMode: "build",
  currentModel: "",
  viewingChildSessionId: null,
  backgroundAgents: {},
  _worktreeStates: {},

  setMessages: (msgs) =>
    set({ timeline: msgs.map((m) => ({ source: "message" as const, msg: m })) }),

  handleWsEvent: (ev) => {
    const s = get();
    console.log("[WS] handleWsEvent:", ev.type, ev.status || ev.name || ev.tool_name || "");

    if (ev.type === "status") {
      if (ev.status === "running") {
        set({ isRunning: true, error: null });
      } else if (ev.status === "completed") {
        set({
          isRunning: false,
          steps: ev.result?.steps_taken ?? s.steps,
          tokens: ev.result?.total_tokens ?? s.tokens,
          planApproval: null,  // clear plan state when build completes
          // TODO: replace with run_id-scoped cleanup when run tracking is added
        });
        return;
      } else if (ev.status === "failed") {
        set({ isRunning: false, error: ev.error || "Execution failed", planApproval: null });
        return;
      } else if (ev.status === "finish" || ev.status === "gave_up") {
        set({ isRunning: false });
        // Render the agent's final response in the timeline
        if (ev.message) {
          set((prev) => ({
            timeline: [...prev.timeline, { source: "ws" as const, ws: ev }],
          }));
        }
        return;
      }
    }

    if (ev.type === "approval_required") {
      const rid = ev.request_id || "";
      set((prev) => ({
        toolApprovals: {
          ...prev.toolApprovals,
          [rid]: {
            requestId: rid,
            toolName: ev.tool_name || ev.name || "",
            params: (ev.params || {}) as Record<string, unknown>,
            thought: ev.thought || ev.content,
            decisionReason: ev.decision_reason,
            toolUseId: ev.tool_use_id,
            permissionMode: ev.permission_mode,
            riskLevel: ev.risk_level,
          },
        },
      }));
      set((prev) => ({
        timeline: [...prev.timeline, { source: "ws" as const, ws: ev }],
      }));
      return;
    }

    if (ev.type === "worktree_resolved") {
      const csid = ev.child_session_id || "";
      set((prev) => {
        const next = { ...prev.backgroundAgents };
        if (next[csid]) {
          next[csid] = {
            ...next[csid],
            status: "completed",
            lastAction: `worktree ${ev.action}: ${ev.status}`,
          };
        }
        // Also track for SubagentDetail to pick up
        const wts = { ...prev._worktreeStates };
        wts[`${csid}_${ev.action}`] = ev.status || "error";
        return { backgroundAgents: next, _worktreeStates: wts };
      });
      set((prev) => ({
        timeline: [...prev.timeline, { source: "ws" as const, ws: ev }],
      }));
      return;
    }

    if (ev.type === "approval_timeout") {
      const rid = ev.request_id || "";
      set((prev) => {
        const next = { ...prev.toolApprovals };
        delete next[rid];
        return { toolApprovals: next };
      });
      return;
    }

    if (ev.type === "plan_ready") {
      set({
        isRunning: false,
        steps: ev.result?.steps_taken ?? s.steps,
        tokens: ev.result?.total_tokens ?? s.tokens,
        planApproval: {
          planText: ev.plan_text || ev.result?.summary || "",
          isWaiting: true,
          sessionId: s._wsSessionId,
          contract: (ev.contract || null) as Record<string, unknown> | null,
          revision: typeof ev.revision === "number" ? ev.revision : 0,
          maxRevisions: typeof ev.max_revisions === "number" ? ev.max_revisions : 5,
        },
      });
      // Also add to timeline for rendering
      set((prev) => ({
        timeline: [...prev.timeline, { source: "ws" as const, ws: ev }],
      }));
      return;
    }

    // Track background subagent progress
    if (ev.type === "subagent_start") {
      const csid = ev.child_session_id || "";
      set((prev) => ({
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
      set((prev) => {
        const next = { ...prev.backgroundAgents };
        if (next[csid]) {
          next[csid] = { ...next[csid], status: ev.status || "completed" };
        }
        return { backgroundAgents: next };
      });
    }
    // Update tool count + last action for running background agents.
    // Use child_session_id for precise attribution when available.
    if (ev.type === "tool_call" || ev.type === "observation") {
      set((prev) => {
        const updated = { ...prev.backgroundAgents };
        const csid = ev.child_session_id || "";
        if (csid && updated[csid]?.status === "running") {
          updated[csid] = {
            ...updated[csid],
            toolCount: updated[csid].toolCount + 1,
            lastAction: ev.name || ev.tool_name || "",
          };
          return { backgroundAgents: updated };
        }
        // Fallback: no child_session_id — update first running agent
        for (const key of Object.keys(updated)) {
          if (updated[key].status === "running") {
            updated[key] = {
              ...updated[key],
              toolCount: updated[key].toolCount + 1,
              lastAction: ev.name || ev.tool_name || "",
            };
            break;  // only one — don't broadcast
          }
        }
        return { backgroundAgents: updated };
      });
    }

    // Add to timeline (for thought, tool_call, observation, reflection, etc.)
    if (
      ev.type === "thought" ||
      ev.type === "tool_call" ||
      ev.type === "observation" ||
      ev.type === "reflection" ||
      ev.type === "subagent_start" ||
      ev.type === "subagent_stop" ||
      ev.type === "status" ||
      ev.type === "worktree_resolved"
    ) {
      set((prev) => ({
        timeline: [...prev.timeline, { source: "ws" as const, ws: ev }],
      }));
    }

    // Add to compact event list
    set((prev) => ({
      events: [ev, ...prev.events].slice(0, 100),
    }));
  },

  clearEvents: () => set({ events: [] }),

  clear: () =>
    set({
      timeline: [],
      events: [],
      steps: 0,
      tokens: 0,
      error: null,
      isRunning: false,
      _wsSessionId: "",
      planApproval: null,
      toolApprovals: {},
      backgroundAgents: {},
      _worktreeStates: {},
      viewingChildSessionId: null,
    }),

  sendChat: async (sessionId, prompt, intent) => {
    set({ isRunning: true, error: null, planApproval: null });
    try {
      const userMsg: Message = { role: "user", content: prompt };
      set((prev) => ({
        timeline: [...prev.timeline, { source: "message" as const, msg: userMsg }],
      }));
      const { currentMode } = get();
      await api.chat(sessionId, prompt, intent, currentMode);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Chat failed";
      set({ error: msg, isRunning: false });
    }
  },

  setMode: (mode: string) => {
    set({ currentMode: mode });
  },

  switchModel: async (model: string, provider?: string) => {
    const { _wsSessionId } = get();
    if (!_wsSessionId) return;
    set({ currentModel: model });
    try {
      const r = await fetch(`/api/sessions/${encodeURIComponent(_wsSessionId)}/model`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model, provider: provider || "" }),
      });
      if (!r.ok) {
        console.error("[Model] Switch failed:", r.status);
        set({ currentModel: "" });  // revert on failure
      }
    } catch (e) {
      console.error("[Model] Switch error:", e);
      set({ currentModel: "" });
    }
  },

  setViewingChild: (id) => set({ viewingChildSessionId: id }),

  compactSession: async () => {
    const { _wsSessionId } = get();
    if (!_wsSessionId) return false;
    try {
      await api.compactSession(_wsSessionId);
      return true;
    } catch {
      return false;
    }
  },

  loadMessages: async (sessionId) => {
    try {
      const msgs = await api.getMessages(sessionId);
      set({ timeline: msgs.map((m) => ({ source: "message" as const, msg: m })) });
    } catch {
      /* ignore */
    }
  },

  loadTraceEvents: async (sessionId) => {
    try {
      const events = await api.getTraceEvents(sessionId);
      if (events.length === 0) return;
      // Prepend historical events before messages (they happened before)
      const wsItems = events.map((ws) => ({ source: "ws" as const, ws }));
      set((prev) => {
        const msgs = prev.timeline.filter((item) => item.source === "message");
        return { timeline: [...wsItems, ...msgs] };
      });
    } catch {
      /* ignore */
    }
  },

  connectWs: (sessionId) => {
    get().disconnectWs();
    // Clear all session-scoped state on switch
    set({
      planApproval: null,
      error: null,
      toolApprovals: {},
      backgroundAgents: {},
      _worktreeStates: {},
      viewingChildSessionId: null,
    });
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/api/ws/sessions/${sessionId}`;
    console.log("[WS] Connecting to", url);
    const ws = new WebSocket(url);
    ws.onopen = () => {
      console.log("[WS] Connected — session:", sessionId);
      set({ wsConnected: true, wsCloseInfo: "", error: null });
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as WsMessage;
        if (msg.type === "pong") return;
        console.log("[WS] ←", msg.type, msg.status || msg.name || "");
        get().handleWsEvent(msg);
      } catch (err) {
        console.warn("[WS] Failed to parse message:", ev.data.slice(0, 100), err);
      }
    };
    ws.onerror = () => {
      console.error("[WS] Connection error for session", sessionId);
      // onerror provides no details — onclose will fire next with the code
    };
    ws.onclose = (ev) => {
      const info = `code=${ev.code}${ev.reason ? " reason=" + ev.reason : ""}`;
      console.log("[WS] Closed —", info);
      set({ ws: null, wsConnected: false, wsCloseInfo: info });
      if (ev.code !== 1000 && ev.code !== 1001) {
        set({ error: `WS closed: ${info}` });
      }
    };
    set({ ws, _wsSessionId: sessionId });
  },

  disconnectWs: () => {
    const { ws } = get();
    if (ws) {
      ws.close();
      set({ ws: null, wsConnected: false });
    }
  },

  approvePlan: async (comment) => {
    const { planApproval } = get();
    if (!planApproval) return;
    const sid = planApproval.sessionId;
    try {
      set({ isRunning: true, planApproval: { ...planApproval, isWaiting: false } });
      await api.approveSession(sid, comment);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Approval failed";
      set({ error: msg, isRunning: false });
      // Restore plan approval state so user can retry
      const { planApproval } = get();
      if (planApproval) {
        set({ planApproval: { ...planApproval, isWaiting: true } });
      }
    }
  },

  rejectPlan: async (reason) => {
    const { planApproval } = get();
    if (!planApproval) return;
    const sid = planApproval.sessionId;
    try {
      set({ isRunning: true, planApproval: { ...planApproval, isWaiting: false } });
      await api.rejectSession(sid, reason);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Rejection failed";
      set({ error: msg, isRunning: false });
      // Restore plan approval state so user can retry
      const { planApproval } = get();
      if (planApproval) {
        set({ planApproval: { ...planApproval, isWaiting: true } });
      }
    }
  },

  clearPlanApproval: () => set({ planApproval: null }),

  /** Resolve a pending tool approval (Allow/Deny/Always Allow). */
  resolveToolApproval: async (requestId: string, decision: "allow" | "deny", opts?: { note?: string; always?: boolean }) => {
    const snapshot = get().toolApprovals[requestId];
    if (!snapshot) return;
    const sid = get()._wsSessionId;
    console.log("[ToolApproval] Resolving", requestId, decision, "session:", sid);

    // Optimistic removal
    set((prev) => {
      const next = { ...prev.toolApprovals };
      delete next[requestId];
      return { toolApprovals: next };
    });

    try {
      const r = await fetch(`/api/sessions/${encodeURIComponent(sid)}/tool-approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request_id: requestId,
          decision,
          note: opts?.note || "",
          always: opts?.always || false,
        }),
      });
      if (!r.ok) {
        const errText = await r.text().catch(() => "");
        console.error("[ToolApproval] Server rejected:", r.status, errText);
        // Restore card so user can retry
        set((prev) => ({
          toolApprovals: { ...prev.toolApprovals, [requestId]: snapshot },
          error: `Approval failed: ${r.status} ${errText}`.slice(0, 100),
        }));
      }
    } catch (e: unknown) {
      console.error("[ToolApproval] Network error:", e);
      // Restore card so user can retry
      set((prev) => ({
        toolApprovals: { ...prev.toolApprovals, [requestId]: snapshot },
        error: `Approval network error: ${String(e).slice(0, 80)}`,
      }));
    }
  },
}));
