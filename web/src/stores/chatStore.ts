import { create } from "zustand";
import type { Message, TimelineItem, WsMessage } from "../types";
import type { WsEnvelope, WsPlanReadyEvent } from "../types/events";
import type { ContentBlock, ThoughtBlock, ToolUseBlock } from "../types/blocks";
import { blockId, blockHash } from "../types/blocks";
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
  /** Highest backend trace sequence rendered for this session. */
  lastTraceSeq: number;
  /** Live streaming blocks — WS deltas mutate this array directly. */
  streamingBlocks: ContentBlock[];
  /** DB-loaded blocks per message id. Replaces streaming on loadTimeline. */
  dbBlocksByMsgId: Record<string, { blocks: ContentBlock[]; role: "user" | "assistant" }>;
  /** View mode: verbose | normal | summary (CC-aligned). */
  viewMode: "verbose" | "normal" | "summary";
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
  loadTimeline: (sessionId: string, signal?: AbortSignal, afterSeq?: number) => Promise<void>;
  loadTraceEvents: (sessionId: string, signal?: AbortSignal, afterSeq?: number) => Promise<void>;
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
  setRunning: (sessionId: string | null | undefined, value: boolean) => void;
  setMode: (mode: string, sessionId?: string | null) => void;
  cycleViewMode: (sessionId?: string | null) => void;
  switchModel: (model: string, provider?: string, sessionId?: string | null) => Promise<void>;
  compactSession: (sessionId?: string | null) => Promise<boolean>;
  setViewingChild: (id: string | null, sessionId?: string | null) => void;

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
    lastTraceSeq: 0,
    streamingBlocks: [],
    dbBlocksByMsgId: {},
    viewMode: loadViewModePreference(),
  };
}

/** Load global view mode preference from localStorage. Default: normal. */
function loadViewModePreference(): "verbose" | "normal" | "summary" {
  try {
    const v = localStorage.getItem("grace-view-mode");
    if (v === "verbose" || v === "summary") return v;
  } catch { /* localStorage unavailable */ }
  return "normal";
}

// ── WS → ContentBlock mapping ──────────────────────────────────────────

/** Map a WS event to a ContentBlock delta for the streaming blocks array. */
function applyWsToBlocks(
  blocks: ContentBlock[],
  ev: WsMessage,
  messageId: string,
): void {
  if (ev.type === "thought_delta") {
    const last = blocks[blocks.length - 1];
    if (last?.type === "thought" && last.phase === "streaming") {
      last.content += ev.text || "";
    } else {
      blocks.push({
        type: "thought",
        content: ev.text || "",
        summary: "",
        phase: "streaming",
      });
    }
    return;
  }

  if (ev.type === "thought") {
    // Full thought replaces any streaming thought blocks
    const last = blocks[blocks.length - 1];
    if (last?.type === "thought" && last.phase === "streaming") {
      last.content = ev.content || "";
      last.phase = "completed";
      last.summary = summarizeThought(ev.content || "");
    } else {
      blocks.push({
        type: "thought",
        content: ev.content || "",
        summary: summarizeThought(ev.content || ""),
        phase: "completed",
      });
    }
    return;
  }

  if (ev.type === "tool_call") {
    blocks.push({
      type: "tool_use",
      id: ev.id || blockId(messageId, blocks.length),
      name: ev.name || "unknown",
      input: (ev.params || {}) as Record<string, unknown>,
      status: "running",
    });
    return;
  }

  if (ev.type === "observation") {
    // Find the matching tool_use block (last running one with matching id)
    for (let i = blocks.length - 1; i >= 0; i--) {
      const b = blocks[i];
      if (b.type === "tool_use" && b.status === "running") {
        if (!ev.id || b.id === ev.id || b.name === (ev.tool_name || "")) {
          b.status = ev.error ? "error" : "success";
          b.output = ev.output;
          b.error = ev.error;
          b.outputSize = (ev.output || "").length;
          break;
        }
      }
    }
    return;
  }

  // Other events (status, subagent, etc.) — not mapped to blocks
}

/** Extract first sentence as thought summary. Fallback: generic label. */
function summarizeThought(content: string): string {
  const first = content.split(/[.。\n]/)[0]?.trim() || "";
  return first.length > 10 && first.length < 120 ? first : "Thinking…";
}

/** Extract ContentBlocks from merged timeline items (DB path). */
function extractBlocksFromTimeline(
  items: TimelineItem[],
): Map<string, { blocks: ContentBlock[]; role: "user" | "assistant" }> {
  const result = new Map<string, { blocks: ContentBlock[]; role: "user" | "assistant" }>();
  let currentMsgId = "";
  let currentRole: "user" | "assistant" = "assistant";
  let blocks: ContentBlock[] = [];

  for (const item of items) {
    if (item.source === "message") {
      const role = item.msg.role;
      // Flush previous message before starting a new one
      if (currentMsgId && blocks.length > 0) {
        result.set(currentMsgId, { blocks, role: currentRole });
      }
      currentMsgId = item.msg.created_at || `msg_${result.size}`;
      blocks = [];

      if (role === "user" && item.msg.content) {
        currentRole = "user";
        blocks.push({ type: "text", content: item.msg.content });
      } else if (role === "assistant" && item.msg.content) {
        currentRole = "assistant";
        blocks.push({ type: "text", content: item.msg.content });
      }
      // tool messages are ignored — their content appears in tool_use blocks
    } else if (item.source === "ws") {
      const msgId = currentMsgId || `msg_${result.size}_stream`;
      applyWsToBlocks(blocks, item.ws, msgId);
    }
  }
  if (currentMsgId && blocks.length > 0) {
    result.set(currentMsgId, { blocks, role: currentRole });
  }
  return result;
}

/** Integrity check: compare streaming block count + last block hash with DB. */
function checkBlockIntegrity(
  streamingBlocks: ContentBlock[],
  dbBlocksByMsgId: Record<string, { blocks: ContentBlock[]; role: string }>,
): boolean {
  if (streamingBlocks.length === 0) return true;
  const allDbBlocks = Object.values(dbBlocksByMsgId).flatMap((e) => e.blocks);
  if (allDbBlocks.length === 0) return true;

  if (streamingBlocks.length !== allDbBlocks.length) return false;

  const wsLast = streamingBlocks[streamingBlocks.length - 1];
  const dbLast = allDbBlocks[allDbBlocks.length - 1];
  return blockHash(wsLast) === blockHash(dbLast);
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
// Capped at 200 entries per session to bound memory.
const _seenFingerprintsBySession = new Map<string, Set<string>>();

function _eventFingerprint(ev: WsMessage): string | null {
  // Only fingerprint events that go into the timeline.
  // thought_delta is intentionally cumulative — never deduped.
  if (ev.type === "thought_delta") return null;
  const step = (ev as { step?: number }).step ?? 0;
  switch (ev.type) {
    case "tool_call":    return `tc:${step}:${ev.name || ""}:${ev.id || ""}`;
    case "observation":  return `ob:${step}:${ev.tool_name || ""}:${ev.id || ""}`;
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

function getSeenFingerprints(sessionId: string): Set<string> {
  let seen = _seenFingerprintsBySession.get(sessionId);
  if (!seen) {
    seen = new Set<string>();
    _seenFingerprintsBySession.set(sessionId, seen);
  }
  return seen;
}

function rememberFingerprint(sessionId: string, fingerprint: string): void {
  const seen = getSeenFingerprints(sessionId);
  seen.add(fingerprint);
  if (seen.size > 200) {
    const iter = seen.values();
    for (let i = 0; i < 50; i++) {
      const v = iter.next().value;
      if (v) seen.delete(v);
    }
  }
}

function timelineTimestamp(item: TimelineItem): string {
  if (item.source === "ws") return (item.ws as { timestamp?: string }).timestamp || "";
  return item.msg.created_at || "";
}

function timelineItemKey(item: TimelineItem): string {
  if (item.source === "message") {
    const msg = item.msg;
    // Use content length + first/last 20 chars as a fast fingerprint
    // that catches rephrased-duplicate detection while avoiding
    // destructive collisions on long similar content.
    const contentFp = `${msg.content.length}:${msg.content.slice(0, 20)}:${msg.content.slice(-20)}`;
    return `msg:${msg.role}:${msg.tool_call_id || ""}:${contentFp}`;
  }

  const ev = item.ws as WsMessage & {
    timestamp?: string;
    step?: number;
    id?: string;
    request_id?: string;
    child_session_id?: string;
  };
  const id = ev.id || ev.request_id || ev.child_session_id || "";
  return `ws:${ev.type}:${ev.timestamp || ""}:${ev.step ?? ""}:${id}`;
}

function mergeTimelineItems(existing: TimelineItem[], incoming: TimelineItem[]): TimelineItem[] {
  const incomingPersistedMessages = incoming
    .filter((item) => item.source === "message" && !!item.msg.created_at)
    .map((item) => item.source === "message" ? `${item.msg.role}:${item.msg.content}` : "");
  const persistedMessageSet = new Set(incomingPersistedMessages);

  const merged = new Map<string, TimelineItem>();
  for (const item of existing) {
    if (
      item.source === "message" &&
      !item.msg.created_at &&
      persistedMessageSet.has(`${item.msg.role}:${item.msg.content}`)
    ) {
      continue;
    }
    merged.set(timelineItemKey(item), item);
  }
  for (const item of incoming) {
    merged.set(timelineItemKey(item), item);
  }

  return Array.from(merged.values()).sort((a, b) => {
    const aTs = timelineTimestamp(a);
    const bTs = timelineTimestamp(b);
    if (!aTs && !bTs) return 0;
    if (!aTs) return 1;
    if (!bTs) return -1;
    return aTs.localeCompare(bTs);
  });
}

/** LEGACY: Infer plan approval status by scanning trace events for plan_ready.
 *  Kept as fallback for loadTraceEvents (legacy path). New code should read
 *  planApproval from /timeline's plan_state field instead. */
function restorePlanApprovalFromEvents(
  current: PlanApproval | null,
  sessionId: string,
  events: WsMessage[],
): PlanApproval | null {
  if (current?.isWaiting) return current;
  const planEvent: WsPlanReadyEvent | undefined = events.find(
    (e): e is WsPlanReadyEvent => e.type === "plan_ready",
  );
  if (!planEvent || !planEvent.plan_text) return current;
  return {
    planText: planEvent.plan_text,
    isWaiting: true,
    sessionId,
    contract: planEvent.contract ?? null,
    revision: planEvent.revision ?? 0,
    maxRevisions: planEvent.max_revisions ?? 5,
  };
}

function maxTraceSeq(events: WsEnvelope[], fallback: number): number {
  return events.reduce((max, ev) => Math.max(max, ev.seq ?? 0), fallback);
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
    _seenFingerprintsBySession.delete(sessionId);
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
      const msgItems = msgs.map((m) => ({ source: "message" as const, msg: m }));
      patchSession(sid, (prev) => ({
        ...prev,
        timeline: mergeTimelineItems(prev.timeline.filter((item) => item.source === "ws"), msgItems),
      }));
    },

    handleWsEvent: (ev: WsEnvelope) => {
      const sid = get()._wsSessionId;
      if (!sid) return;
      const session = ensureSession(sid);

      const evSeq = ev.seq ?? 0;
      if (evSeq > 0 && evSeq <= session.lastTraceSeq) return;

      // Append to raw event log first.  This is the canonical live stream and
      // must include lifecycle events even when specialized handlers return.
      patchSession(sid, (prev) => ({
        ...prev,
        events: [ev, ...prev.events].slice(0, 100),
        lastTraceSeq: evSeq > prev.lastTraceSeq ? evSeq : prev.lastTraceSeq,
      }));

      // ── ContentBlock streaming: map WS events to blocks ──
      // Mutates the streamingBlocks array in place for the current session.
      if (
        ev.type === "thought_delta" || ev.type === "thought" ||
        ev.type === "tool_call" || ev.type === "observation"
      ) {
        patchSession(sid, (prev) => {
          const blocks = [...prev.streamingBlocks];
          applyWsToBlocks(blocks, ev, `msg_${sid}_stream`);
          return { ...prev, streamingBlocks: blocks };
        });
      }

      if (ev.type === "status") {
        if (ev.status === "running") {
          patchSession(sid, (prev) => ({
            ...prev, isRunning: true, error: null,
            // Only clear if truly empty — sendChat may have already
            // injected the user's prompt.  WS reconnect or replay can
            // send running again without going through sendChat.
            streamingBlocks: prev.streamingBlocks.length > 0 ? prev.streamingBlocks : [],
          }));
        } else if (ev.status === "completed") {
          clearWatchdog();
          patchSession(sid, (prev) => ({
            ...prev,
            isRunning: false,
            steps: ev.result?.steps_taken ?? prev.steps,
            tokens: ev.result?.total_tokens ?? prev.tokens,
            streamingThought: "",
            streamingBlocks: [],  // run complete → clear live blocks
            // Do NOT clear planApproval here — when the plan agent
            // exits via ExitPlanMode, the plan_ready event that
            // follows immediately will overwrite it.  If the plan
            // agent exited WITHOUT a contract (error / step budget
            // / model gave up), the planApproval stays visible and
            // the next loadTimeline will restore it from plan_state.
          }));
          return;
        } else if (ev.status === "failed") {
          clearWatchdog();
          patchSession(sid, (prev) => ({
            ...prev,
            isRunning: false,
            error: ev.error || "Execution failed",
            planApproval: null,  // a failed run invalidates any pending plan
            streamingThought: "",
          }));
          return;
        } else if (ev.status === "cancelled") {
          clearWatchdog();
          patchSession(sid, (prev) => ({
            ...prev,
            isRunning: false,
            error: ev.error || ev.message || "Execution cancelled",
            planApproval: null,  // explicit cancellation invalidates the plan
            streamingThought: "",
          }));
          return;
        } else if (ev.status === "finish" || ev.status === "gave_up") {
          clearWatchdog();
          patchSession(sid, (prev) => ({
            ...prev,
            isRunning: false,
            streamingThought: "",
            planApproval: null,  // agent gave a final response, not a plan
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
      const _seenFingerprints = getSeenFingerprints(sid);
      const _isDup = _fp !== null && _seenFingerprints.has(_fp);

      if (
        !_isDup &&
        (ev.type === "thought" ||
        ev.type === "tool_call" ||
        ev.type === "observation" ||
        ev.type === "reflection" ||
        ev.type === "subagent_start" ||
        ev.type === "subagent_stop" ||
        ev.type === "memory_recall" ||
        ev.type === "memory_written")
      ) {
        if (_fp !== null) {
          rememberFingerprint(sid, _fp);
        }
        patchSession(sid, (prev) => ({
          ...prev,
          timeline: [...prev.timeline, { source: "ws" as const, ws: ev }],
        }));
      }

    },

    clearEvents: () => {
      const sid = get()._wsSessionId;
      if (!sid) return;
      patchSession(sid, (prev) => ({ ...prev, events: [] }));
    },

    clear: (sessionId) => {
      const sid = resolveSessionId(sessionId);
      if (!sid) return;
      _seenFingerprintsBySession.delete(sid);
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
      for (const sessionId of _seenFingerprintsBySession.keys()) {
        if (!validIds.has(sessionId)) _seenFingerprintsBySession.delete(sessionId);
      }
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
        // Inject the user's prompt as the first streaming block.
        // This fills the "streaming window gap" before WS events arrive.
        // loadTimeline replaces this with real DB blocks on completion.
        streamingBlocks: [{ type: "text", content: prompt }],
      }));
      clearWatchdog();  // clear any stale timer from a previous run
      _watchdogTimer = setTimeout(() => {
        const current = selectSessionUi(get(), sessionId);
        if (current.isRunning) {
          // Try to cancel the backend run so resources aren't wasted (I3).
          try { void api.cancelSession(sessionId, "Timed out from web frontend"); } catch { /* best-effort */ }
          patchSession(sessionId, (prev) => ({
            ...prev,
            isRunning: false,
            error: `Request timed out after ${CHAT_TIMEOUT_MS / 60000} minutes`,
          }));
        }
      }, CHAT_TIMEOUT_MS);
      try {
        if (get()._wsSessionId !== sessionId) return;
        const userMsg: Message = { role: "user", content: prompt, created_at: new Date().toISOString() };
        patchSession(sessionId, (prev) => ({
          ...prev,
          timeline: mergeTimelineItems(prev.timeline, [{ source: "message" as const, msg: userMsg }]),
        }));
        if (get()._wsSessionId !== sessionId) return;
        // Wait for WS connection before triggering the backend (I1).
        // If the WS isn't ready yet, events emitted by the agent will be
        // persisted to SQLite but never delivered live — resulting in a
        // spinner with no visible progress.
        if (!get().wsConnected) {
          const deadline = Date.now() + 3000;
          while (!get().wsConnected && Date.now() < deadline) {
            await new Promise((r) => setTimeout(r, 50));
          }
          if (!get().wsConnected) {
            // WS still not connected — events will be missed.
            // loadTimeline will recover them, but set a flag so the UI
            // knows to refresh when the WS finally opens.
            // WS still not connected after wait — events delivered
            // during this gap will be recovered by loadTimeline on
            // the next user action or page refresh.
          }
        }
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

    setRunning: (sessionId, value) => {
      if (!sessionId) return;
      patchSession(sessionId, (prev) => ({ ...prev, isRunning: value }));
    },

    setMode: (mode, sessionId) => {
      const sid = resolveSessionId(sessionId);
      if (!sid) return;
      patchSession(sid, (prev) => ({ ...prev, currentMode: mode }));
    },

    cycleViewMode: (sessionId) => {
      const sid = resolveSessionId(sessionId);
      if (!sid) return;
      const next: Record<string, "verbose" | "normal" | "summary"> = {
        verbose: "normal", normal: "summary", summary: "verbose",
      };
      patchSession(sid, (prev) => {
        const vm = next[prev.viewMode] || "normal";
        try { localStorage.setItem("grace-view-mode", vm); } catch { /* ok */ }
        return { ...prev, viewMode: vm };
      });
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
          const msgItems = msgs.map((m) => ({ source: "message" as const, msg: m }));
          return { ...prev, timeline: mergeTimelineItems(prev.timeline, msgItems) };
        });
      } catch (e: unknown) {
        if (e instanceof ApiError && e.status === 404) {
          invalidateSession(sessionId);
        }
      }
    },

    loadTimeline: async (sessionId, signal, afterSeq = 0) => {
      try {
        ensureSession(sessionId);
        const response = await api.getTimeline(sessionId, signal, afterSeq);
        const timelineItems = response.items.map((item) => (
          item.source === "message"
            ? { source: "message" as const, msg: item.message }
            : { source: "ws" as const, ws: { ...item.event, seq: item.seq } }
        ));
        const events = response.items
          .filter((item) => item.source === "ws")
          .map((item) => item.source === "ws" ? item.event : null)
          .filter((item): item is WsMessage => !!item);

        // Read plan approval from backend-owned plan_state (primary path).
        // Falls back to event scanning only when plan_state is absent (legacy).
        const planState = response.plan_state;
        let planApproval: PlanApproval | null = null;
        // "waiting" = fresh plan, "saved" = deferred after explicit Save — both
        // should present the approval UI after refresh (I6).
        if (planState && (planState.lifecycle === "waiting" || planState.lifecycle === "saved") && planState.plan_text) {
          planApproval = {
            planText: planState.plan_text,
            isWaiting: true,
            sessionId,
            contract: planState.contract ?? null,
            revision: planState.revision,
            maxRevisions: planState.max_revisions,
          };
        }

        // ── ContentBlock integrity check ──
        // Compare streaming blocks with DB blocks. If they match, silently
        // replace attributes (no remount). If they differ, full remount.
        const dbBlocks = extractBlocksFromTimeline(timelineItems);
        const dbBlocksByMsgId: Record<string, { blocks: ContentBlock[]; role: "user" | "assistant" }> = {};
        for (const [msgId, entry] of dbBlocks) {
          if (entry.blocks.length > 0) dbBlocksByMsgId[msgId] = entry;
        }

        patchSession(sessionId, (prev) => {
          const integrityOk = checkBlockIntegrity(prev.streamingBlocks, dbBlocksByMsgId);
          return {
            ...prev,
            events: afterSeq > 0
              ? [...events.slice().reverse(), ...prev.events].slice(0, 100)
              : events.slice().reverse().slice(0, 100),
            timeline: mergeTimelineItems(afterSeq > 0 ? prev.timeline : [], timelineItems),
            planApproval: afterSeq > 0 ? prev.planApproval : (planApproval ?? prev.planApproval),
            lastTraceSeq: Math.max(prev.lastTraceSeq, response.last_seq || 0),
            dbBlocksByMsgId,
            // If integrity passes, streamingBlocks survive (no remount for render).
            // If it fails, clear them so the render layer creates fresh components.
            streamingBlocks: integrityOk ? prev.streamingBlocks : [],
          };
        });
      } catch (e: unknown) {
        if (afterSeq <= 0) {
          await get().loadMessages(sessionId, signal);
          await get().loadTraceEvents(sessionId, signal);
        } else if (e instanceof ApiError && e.status === 404) {
          invalidateSession(sessionId);
        }
      }
    },

    /** LEGACY: Fallback path for backends without /timeline support.
     *  loadTimeline is the primary load path — this exists only for compatibility. */
    loadTraceEvents: async (sessionId, signal, afterSeq = 0) => {
      try {
        ensureSession(sessionId);
        const events = await api.getTraceEvents(sessionId, 0, 200, signal, afterSeq);
        patchSession(sessionId, (prev) => {
          // Lifecycle status events are NOT timeline items on replay:
          //   "completed" → isRunning=false signal, no display value
          //   "finish" / "gave_up" → content comes from persisted
          //     assistant message (loadMessages), not from WS trace
          //   "failed" / "running" → transient state signals
          const _LIFECYCLE_STATUSES = new Set(["completed", "finish", "gave_up", "failed", "running", "cancelled"]);
          const wsItems = events
            .filter((ws) => ws.type !== "status" || !_LIFECYCLE_STATUSES.has(ws.status || ""))
            .map((ws) => ({ source: "ws" as const, ws }));

          // Merge ws events and messages by timestamp — chronological order.
          const merged = mergeTimelineItems(prev.timeline, wsItems);

          return {
            ...prev,
            events: afterSeq > 0
              ? [...events.slice().reverse(), ...prev.events].slice(0, 100)
              : events.slice().reverse().slice(0, 100),
            timeline: merged,
            planApproval: restorePlanApprovalFromEvents(prev.planApproval, sessionId, events),
            lastTraceSeq: maxTraceSeq(events, prev.lastTraceSeq),
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

      const ws = connectWebSocket(sessionId, {
        onOpen: () => {
          if (get()._wsSessionId !== sessionId) return;
          set({ wsConnected: true, wsCloseInfo: "" });
          patchSession(sessionId, (prev) => ({ ...prev, error: null }));
          const lastSeq = selectSessionUi(get(), sessionId).lastTraceSeq;
          if (lastSeq > 0) void get().loadTimeline(sessionId, undefined, lastSeq);
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
      if (get()._wsSessionId === sessionId) {
        set({ ws });
      } else {
        ws.close();
      }
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
        // Explicit mode switch — approve always transitions to build.
        // Don't rely on the useEffect to catch agent_name change because
        // if the user temporarily switched to build in-memory before
        // approving, the backend writing "build" again is a no-op and
        // refreshActive won't trigger a mode sync.
        get().setMode("build", sid);
        // Defensive: catch any events the backend emitted during
        // the approve → build transition (P4).
        try { void get().loadTimeline(sid, undefined, 0); } catch { /* best-effort */ }
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
        // Defensive: catch the re-plan trace events (P4).
        try { void get().loadTimeline(sid, undefined, 0); } catch { /* best-effort */ }
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
        // Defensive: refresh state from backend (P4).
        try { void get().loadTimeline(sid, undefined, 0); } catch { /* best-effort */ }
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
        // Defensive: refresh to confirm the backend-side abort (P4).
        try { void get().loadTimeline(sid, undefined, 0); } catch { /* best-effort */ }
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
