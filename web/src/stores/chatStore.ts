import { create } from "zustand";
import type { Message, WsMessage, TimelineItem } from "../types";
import * as api from "../api/sessions";

export interface PlanApproval {
  planText: string;
  isWaiting: boolean;
  sessionId: string;
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
  /** Internal: the session ID the current WS is connected to */
  _wsSessionId: string;
  /** Plan approval state (set when plan_ready event arrives) */
  planApproval: PlanApproval | null;

  setMessages: (msgs: Message[]) => void;
  handleWsEvent: (ev: WsMessage) => void;
  clearEvents: () => void;
  clear: () => void;
  /** Submit chat (async — returns immediately, events come via WS) */
  sendChat: (sessionId: string, prompt: string, intent?: string) => Promise<void>;
  /** Load persisted messages for a past session */
  loadMessages: (sessionId: string) => Promise<void>;
  connectWs: (sessionId: string) => void;
  disconnectWs: () => void;
  /** Approve the current plan and trigger build */
  approvePlan: (comment?: string) => Promise<void>;
  /** Reject the current plan and request revision */
  rejectPlan: (reason: string) => Promise<void>;
  /** Clear plan approval state */
  clearPlanApproval: () => void;
}

export const useChatStore = create<ChatState>((set, get) => ({
  timeline: [],
  events: [],
  isRunning: false,
  steps: 0,
  tokens: 0,
  error: null,
  ws: null,
  _wsSessionId: "",
  planApproval: null,

  setMessages: (msgs) =>
    set({ timeline: msgs.map((m) => ({ source: "message" as const, msg: m })) }),

  handleWsEvent: (ev) => {
    const s = get();

    if (ev.type === "status") {
      if (ev.status === "running") {
        set({ isRunning: true, error: null });
      } else if (ev.status === "completed") {
        set({
          isRunning: false,
          steps: ev.result?.steps_taken ?? s.steps,
          tokens: ev.result?.total_tokens ?? s.tokens,
        });
        return;
      } else if (ev.status === "failed") {
        set({ isRunning: false, error: ev.error || "Execution failed" });
        return;
      } else if (ev.status === "finish" || ev.status === "gave_up") {
        set({ isRunning: false });
      }
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
        },
      });
      // Also add to timeline for rendering
      set((prev) => ({
        timeline: [...prev.timeline, { source: "ws" as const, ws: ev }],
      }));
      return;
    }

    // Add to timeline (for thought, tool_call, observation, reflection, etc.)
    if (
      ev.type === "thought" ||
      ev.type === "tool_call" ||
      ev.type === "observation" ||
      ev.type === "reflection" ||
      ev.type === "subagent_start" ||
      ev.type === "subagent_stop"
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
    }),

  sendChat: async (sessionId, prompt, intent) => {
    set({ isRunning: true, error: null, planApproval: null });
    try {
      const userMsg: Message = { role: "user", content: prompt };
      set((prev) => ({
        timeline: [...prev.timeline, { source: "message" as const, msg: userMsg }],
      }));
      await api.chat(sessionId, prompt);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Chat failed";
      set({ error: msg, isRunning: false });
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

  connectWs: (sessionId) => {
    get().disconnectWs();
    // Store sessionId for plan approval context
    set({ planApproval: null }); // clear stale plan state on session switch
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/api/ws/sessions/${sessionId}`;
    const ws = new WebSocket(url);
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as WsMessage;
        if (msg.type === "pong") return;
        get().handleWsEvent(msg);
      } catch {
        /* skip */
      }
    };
    ws.onclose = () => set({ ws: null });
    set({ ws, _wsSessionId: sessionId });
  },

  disconnectWs: () => {
    const { ws } = get();
    if (ws) {
      ws.close();
      set({ ws: null });
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
    }
  },

  clearPlanApproval: () => set({ planApproval: null }),
}));
