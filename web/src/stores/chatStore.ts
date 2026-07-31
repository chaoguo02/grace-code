import { create } from "zustand";
import type { Message, TimelineItem, WsMessage } from "../types";
import type {
  WsEnvelope, WsPlanReadyEvent,
  WsObservationEvent,
  WsRunTerminalEvent,
  WsAssistantTextStartEvent, WsAssistantTextDeltaEvent, WsAssistantTextEndEvent,
  WsAssistantTextAbortedEvent,
  RunEvidenceRecord,
} from "../types/events";
import type { ContentBlock, RunOutcome, StreamingTurn } from "../types/blocks";
import type { DelegationRuns } from "../types/delegation";
import {
  isDelegationEvent,
  rebuildDelegationRuns,
  reduceDelegationEvent,
} from "../types/delegation";
import {
  blockId,
  createStreamingTurn,
  upsertByTurnId,
  mergeTurnsByTurnId,
  reconcileFinalTextBlock,
} from "../types/blocks";
import * as api from "../api/sessions";
import { ApiError } from "../api/client";
import { connectWebSocket, disconnectWebSocket, scheduleReconnect } from "../hooks/useWebSocket";
import { agentNameForUiMode } from "../modes";
import { getMultiAgentSnapshot } from "../api/multiAgent";
import type { DelegationRunProjection, DelegationTaskProjection } from "../types/multiAgent";

let sessionMissingHandler: ((sessionId: string) => void) | null = null;

export interface PlanApproval {
  planText: string;
  isWaiting: boolean;
  sessionId: string;
  contract?: Record<string, unknown> | null;
  revision?: number;
  maxRevisions?: number;
  lifecycle?: "waiting" | "saved";
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
  delegationRuns: DelegationRuns;
  /** Phase 4: live resource governance state (from snapshot). */
  resourceGovernance: Record<string, unknown> | null;
  /** Canonical evidence projection keyed by durable evidence id. */
  evidenceById: Record<string, RunEvidenceRecord>;
  worktreeStates: Record<string, string>;
  /** Per-session draft text — survives tab switches. */
  draft: string;
  /** Accumulated thought_delta text during live streaming. Cleared on full thought. */
  streamingThought: string;
  /** Context window max tokens (from model config). Updated on session load. */
  contextTotal: number;
  /** Highest backend trace sequence rendered for this session. */
  lastTraceSeq: number;
  /** Highest applied sequence — only events ≤ this have been rendered. */
  lastAppliedSequence: number;
  /** Events received out-of-order, keyed by sequence. */
  pendingEvents: [number, WsMessage][];
  /** Observations buffered before their tool_call arrives, keyed by tool_call_id. */
  pendingObservations: Record<string, WsObservationEvent>;
  /** Current streaming turn — created by sendChat, finalized by loadTimeline. */
  activeTurn: StreamingTurn | null;
  /** Active run_id for the current turn. */
  activeRunId: string;
  /** Past turns loaded from DB. */
  completedTurns: StreamingTurn[];
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
  sendChat: (
    sessionId: string,
    prompt: string,
    intent?: string,
    skill?: { name: string; arguments?: string },
  ) => Promise<void>;
  loadMessages: (sessionId: string, signal?: AbortSignal) => Promise<void>;
  loadTimeline: (sessionId: string, signal?: AbortSignal, afterSeq?: number, reconcileTurnId?: string) => Promise<void>;
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
    delegationRuns: {},
    resourceGovernance: null,
    evidenceById: {},
    worktreeStates: {},
    draft: "",
    streamingThought: "",
    contextTotal: 200000,  // default for deepseek-v4 / large models
    lastTraceSeq: 0,
    lastAppliedSequence: 0,
    pendingEvents: [],
    pendingObservations: {},
    activeTurn: null,
    activeRunId: "",
    completedTurns: [],
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

/**
 * Single entry point for mapping ANY WS event → ContentBlock mutations.
 *
 * This is the ONLY function that mutates a ContentBlock[] array.  All three
 * consumption paths (live WS, timeline replay, DB rebuild) go through here.
 *
 * Events that do NOT produce blocks (run_started, status, subagent_*, etc.)
 * are no-ops — they're handled by downstream UI state updates in handleWsEvent.
 */
export function applyWsToBlocks(
  blocks: ContentBlock[],
  ev: WsMessage,
  messageId: string,
): void {
  // ── Thought streaming (delta) ──
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

  // ── Thought completed ──
  if (ev.type === "thought") {
    // The full Action event is persisted only after the provider stream has
    // ended, so answer text may already follow its thought deltas. Complete
    // the nearest streaming thought in place instead of appending a second
    // thought after the answer.
    for (let i = blocks.length - 1; i >= 0; i--) {
      const block = blocks[i];
      if (block.type === "thought" && block.phase === "streaming") {
        block.content = ev.content || "";
        block.phase = "completed";
        block.summary = summarizeThought(ev.content || "");
        return;
      }
    }

    const completedThought = {
      type: "thought" as const,
      content: ev.content || "",
      summary: summarizeThought(ev.content || ""),
      phase: "completed" as const,
    };
    const trailingTextIndex = blocks.length - 1;
    if (blocks[trailingTextIndex]?.type === "text") {
      blocks.splice(trailingTextIndex, 0, completedThought);
    } else {
      blocks.push(completedThought);
    }
    return;
  }

  // ── Tool call ──
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

  // ── Observation (tool result) ──
  if (ev.type === "observation") {
    for (let i = blocks.length - 1; i >= 0; i--) {
      const b = blocks[i];
      if (b.type === "tool_use" && b.status === "running") {
        if (!ev.id || b.id === ev.id || b.name === (ev.tool_name || "")) {
          b.status = ev.error ? "error" : "success";
          b.output = ev.output;
          b.error = ev.error;
          b.outputSize = (ev.output || "").length;
          b.evidence = ev.evidence;
          break;
        }
      }
    }
    return;
  }

  // ── Assistant text streaming (was a separate branch in handleWsEvent) ──
  if (ev.type === "assistant_text_start") {
    const se = ev as WsAssistantTextStartEvent;
    blocks.push({ type: "text", content: "", blockId: se.block_id, phase: "streaming" });
    return;
  }

  if (ev.type === "assistant_text_delta") {
    const de = ev as WsAssistantTextDeltaEvent;
    const bid = de.block_id || "";
    const last = blocks[blocks.length - 1];
    if (last?.type === "text" && last.phase === "streaming" && last.blockId === bid) {
      last.content += de.text || "";
    } else if (last?.type === "text" && last.phase === "streaming") {
      // Fallback: append regardless of blockId (survives missing text_start)
      last.content += de.text || "";
    } else {
      blocks.push({ type: "text", content: de.text || "", blockId: bid, phase: "streaming" });
    }
    return;
  }

  if (ev.type === "assistant_text_end") {
    const ee = ev as WsAssistantTextEndEvent;
    const bid = ee.block_id || "";
    for (let i = blocks.length - 1; i >= 0; i--) {
      const b = blocks[i];
      if (b.type === "text" && b.phase === "streaming" && b.blockId === bid) {
        b.phase = "completed";
        break;
      }
    }
    return;
  }

  if (ev.type === "assistant_text_aborted") {
    const ae = ev as WsAssistantTextAbortedEvent;
    const bid = ae.block_id || "";
    for (let i = blocks.length - 1; i >= 0; i--) {
      const b = blocks[i];
      if (b.type === "text" && b.phase === "streaming" && b.blockId === bid) {
        b.phase = "completed";
        b.content += `\n\n[Aborted: ${ae.reason || "stream error"}]`;
        break;
      }
    }
    return;
  }

  // ── run_terminal summary → text fallback ──
  // If no text block was streamed via assistant_text_delta, append the
  // final summary from run_terminal as a text block.
  if (ev.type === "run_terminal") {
    const re = ev as WsRunTerminalEvent;
    if (re.summary) {
      reconcileFinalTextBlock(blocks, re.summary || "");
    }
    return;
  }

  // All other events (run_started, status, subagent_start/stop, approval,
  // plan_ready, memory_*, etc.) are NOT mapped to ContentBlocks.
  // They drive UI state changes (isRunning, toolApprovals, planApproval)
  // via their dedicated handleWsEvent branches — not through blocks.
}

/** Project persisted trace events into UI blocks while repairing the legacy
 * contract that mirrored visible answer tokens into thought_delta events. */
export function applyTraceEventsToBlocks(
  blocks: ContentBlock[],
  events: WsMessage[],
  messageId: string,
): void {
  events.forEach((event, index) => {
    if (event.type === "thought_delta") {
      let next = index + 1;
      if (events[next]?.type === "assistant_text_start") next += 1;
      const candidate = events[next];
      if (
        candidate?.type === "assistant_text_delta"
        && (candidate.text || "") === (event.text || "")
      ) {
        return;
      }
    }
    applyWsToBlocks(blocks, event, messageId);
  });
}

function runOutcomeFromTerminal(re: WsRunTerminalEvent): RunOutcome {
  const verification = re.verification ?? (
    re.verification_status || re.verification_reason
      ? {
          status: re.verification_status || "not_applicable",
          reason: re.verification_reason || "none",
          checks: [],
        }
      : undefined
  );
  return {
    status: re.status,
    terminationReason: re.termination_reason,
    verification,
    workspaceDelta: re.workspace_delta,
    evidenceSummary: re.evidence_summary,
    error: re.error,
    runId: re.run_id,
  };
}

/** Extract first sentence as thought summary. Fallback: generic label. */
function summarizeThought(content: string): string {
  const first = content.split(/[.。\n]/)[0]?.trim() || "";
  return first.length > 10 && first.length < 120 ? first : "Thinking…";
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

// Terminal event idempotency: ensures each run_id is only processed once.
// Prevents duplicate archiving when both status:"completed" and run_terminal
// arrive for the same run (legacy + new signal paths).
const _seenTerminalRunsBySession = new Map<string, Set<string>>();

// Timeline loads can overlap (mount, websocket reconnect, and run_terminal all
// trigger them). Only the newest request for a session may mutate UI state.
const _timelineRequestVersionBySession = new Map<string, number>();

function _isDuplicateTerminal(sessionId: string, runId: string): boolean {
  if (!runId) return false;
  let seen = _seenTerminalRunsBySession.get(sessionId);
  if (!seen) {
    seen = new Set<string>();
    _seenTerminalRunsBySession.set(sessionId, seen);
  }
  if (seen.has(runId)) return true;
  seen.add(runId);
  // Cap at 50 entries per session
  if (seen.size > 50) {
    const iter = seen.values();
    for (let i = 0; i < 10; i++) { const v = iter.next().value; if (v) seen.delete(v); }
  }
  return false;
}

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
    case "approval_resolved": return `ad:${ev.request_id || ""}`;
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
    lifecycle: "waiting",
  };
}

function maxTraceSeq(events: WsEnvelope[], fallback: number): number {
  return events.reduce((max, ev) => Math.max(max, ev.seq ?? 0), fallback);
}

function delegationSnapshotEvents(
  runs: DelegationRunProjection[],
  tasks: DelegationTaskProjection[],
): WsMessage[] {
  const byRun = new Map<string, DelegationTaskProjection[]>();
  for (const task of tasks) {
    if (!task.run_id) continue;
    const group = byRun.get(task.run_id) || [];
    group.push(task);
    byRun.set(task.run_id, group);
  }
  const events: WsMessage[] = [];
  for (const run of runs) {
    const runTasks = byRun.get(run.id) || [];
    const timestamp = run.completed_at || run.created_at;
    events.push({
      type: "delegation_planned",
      delegation_run_id: run.id,
      topology: run.topology,
      task_count: runTasks.length,
      timestamp,
      reason: "snapshot_reconciliation",
    });
    for (const task of runTasks) {
      const resource = task.resource || {};
      const base = {
        delegation_run_id: run.id,
        task_id: task.id,
        agent_type: task.agent_name,
        child_session_id: task.child_session_id || undefined,
        status: task.status,
        generation: task.generation,
        dependencies: task.dependencies,
        integration_status: task.integration_status,
        tokens_used: task.tokens_used,
        duration_ms: task.elapsed_ms,
        reason: "snapshot_reconciliation",
        timestamp,
      };
      if (task.status === "queued") events.push({ type: "delegation_task_queued", ...base });
      else if (task.status === "running") events.push({ type: "delegation_task_started", ...base });
      else if (task.status === "blocked") events.push({ type: "delegation_task_blocked", ...base });
      else if (["completed", "no_findings", "partial"].includes(task.status)) {
        events.push({ type: "delegation_task_reported", ...base });
      } else {
        events.push({ type: "delegation_task_failed", ...base });
      }
      if (resource.requested) {
        events.push({
          type: "delegation_resource_queued",
          ...base,
          resources: resource.requested,
          queue_position: resource.queue_position,
          wait_time_s: resource.wait_time_s,
          outcome: resource.outcome,
        });
      }
      if (resource.granted) {
        events.push({
          type: "delegation_resource_granted",
          ...base,
          resources: resource.granted,
          wait_time_s: resource.wait_time_s,
          outcome: resource.outcome,
        });
      }
      if (resource.consumed) {
        events.push({
          type: "delegation_resource_released",
          ...base,
          resources: resource.granted || resource.requested || {},
          actual: resource.consumed,
          wait_time_s: resource.wait_time_s,
          outcome: resource.outcome,
        });
      }
    }
    if (run.verification) {
      events.push({
        type: "delegation_verification_completed",
        delegation_run_id: run.id,
        phase: run.phase,
        status: String(run.verification.status || "not_run"),
        verification: run.verification,
        reason: "snapshot_reconciliation",
        timestamp,
      });
    }
    if (["completed", "partial", "failed", "cancelled"].includes(run.status)) {
      events.push({
        type: "delegation_completed",
        delegation_run_id: run.id,
        phase: run.phase,
        status: run.status,
        report_count: runTasks.filter((task) => task.status !== "queued" && task.status !== "running").length,
        reason: "snapshot_reconciliation",
        timestamp,
      });
    } else {
      events.push({
        type: "delegation_phase_changed",
        delegation_run_id: run.id,
        phase: run.phase || "executing",
        status: run.status,
        reason: "snapshot_reconciliation",
        timestamp,
      });
    }
  }
  return events;
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
    _seenTerminalRunsBySession.delete(sessionId);
    _timelineRequestVersionBySession.delete(sessionId);
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

      if (isDelegationEvent(ev)) {
        const delegationData = {
          ...((ev.payload || {}) as Record<string, unknown>),
          ...(ev as unknown as Record<string, unknown>),
        };
        patchSession(sid, (prev) => ({
          ...prev,
          delegationRuns: reduceDelegationEvent(prev.delegationRuns, ev),
          resourceGovernance: ev.type.startsWith("delegation_resource_")
            ? (delegationData.governance as Record<string, unknown> | undefined)
              || prev.resourceGovernance
            : prev.resourceGovernance,
        }));
      }

      if (ev.type === "evidence_record") {
        patchSession(sid, (prev) => ({
          ...prev,
          evidenceById: {
            ...prev.evidenceById,
            [ev.evidence.evidence_id]: ev.evidence,
          },
        }));
        return;
      }

      // ── ContentBlock streaming: mutate activeTurn.assistantResponse ──
      // ALL event types that produce ContentBlocks go through this single branch.
      // applyWsToBlocks() is the ONLY function that mutates blocks.
      if (
        ev.type === "thought_delta" || ev.type === "thought" ||
        ev.type === "tool_call" || ev.type === "observation" ||
        ev.type === "assistant_text_start" || ev.type === "assistant_text_delta" ||
        ev.type === "assistant_text_end" || ev.type === "assistant_text_aborted"
      ) {
        patchSession(sid, (prev) => {
          if (!prev.activeTurn) return prev;
          const turn = { ...prev.activeTurn };
          const blocks = [...turn.assistantResponse.blocks];
          applyWsToBlocks(blocks, ev, turn.assistantResponse.id);
          turn.assistantResponse = { ...turn.assistantResponse, blocks };
          // eventSeq: detect gaps in WS event stream
          const wsSeq = ev.seq || 0;
          turn.meta = {
            ...turn.meta,
            eventSeq: wsSeq > 0 ? wsSeq : turn.meta.eventSeq,
            hasGap: turn.meta.hasGap || (wsSeq > 0 && wsSeq !== turn.meta.eventSeq + 1 && turn.meta.eventSeq > 0),
          };
          return { ...prev, activeTurn: turn };
        });
        // text_delta is too noisy for timeline — let text_start/end fall through
        if (ev.type === "assistant_text_delta") return;
      }

      // ── New run lifecycle events (P0) ──
      if (ev.type === "run_started") {
                patchSession(sid, (prev) => ({ ...prev, isRunning: true, error: null }));
        return;
      }

      if (ev.type === "run_terminal") {
        clearWatchdog();
        const re = ev as WsRunTerminalEvent;
        // Idempotency: skip duplicate terminal events for the same run
        if (_isDuplicateTerminal(sid, re.run_id || "")) return;
        if (re.status === "completed") {
          patchSession(sid, (prev) => {
            if (!prev.activeTurn) {
              // A refresh/reconnect can clear the optimistic activeTurn while
              // the backend run continues.  Update an already reconstructed
              // turn when possible; the full reconciliation below supplies
              // messages and trace blocks if it is not in memory yet.
              const matchingIndex = prev.completedTurns.findIndex(
                (turn) =>
                  (re.turn_id && turn.turnId === re.turn_id) ||
                  (re.run_id && turn.runId === re.run_id),
              );
              if (matchingIndex < 0) {
                return { ...prev, isRunning: false, streamingThought: "" };
              }
              const completedTurns = [...prev.completedTurns];
              const matching = completedTurns[matchingIndex];
              const blocks = [...matching.assistantResponse.blocks];
              applyWsToBlocks(blocks, ev, matching.assistantResponse.id);
              completedTurns[matchingIndex] = {
                ...matching,
                turnId: re.turn_id || matching.turnId,
                runId: re.run_id || matching.runId,
                assistantResponse: {
                  ...matching.assistantResponse,
                  blocks,
                  status: "completed" as const,
                },
                meta: {
                  ...matching.meta,
                  completedAt: Date.now(),
                  outcome: runOutcomeFromTerminal(re),
                },
              };
              return {
                ...prev,
                isRunning: false,
                streamingThought: "",
                completedTurns,
              };
            }
            // Delegate block mutations (incl. summary→text fallback) to unified builder
            const blocks = [...prev.activeTurn.assistantResponse.blocks];
            applyWsToBlocks(blocks, ev, prev.activeTurn.assistantResponse.id);

            // Archive completed turn — move from activeTurn to completedTurns.
            // The turn is now immutable; further DB sync via loadTimeline will
            // merge by turn_id without creating duplicates.
            const completedTurn: StreamingTurn = {
              ...prev.activeTurn,
              turnId: re.turn_id || prev.activeTurn.turnId,
              runId: re.run_id || prev.activeTurn.runId,
              assistantResponse: { ...prev.activeTurn.assistantResponse, blocks, status: "completed" as const },
              meta: {
                ...prev.activeTurn.meta,
                completedAt: Date.now(),
                outcome: runOutcomeFromTerminal(re),
              },
            };

            return {
              ...prev,
              isRunning: false,
              steps: re.steps_taken ?? prev.steps,
              tokens: re.total_tokens ?? prev.tokens,
              streamingThought: "",
              activeTurn: null,
              completedTurns: upsertByTurnId(prev.completedTurns, completedTurn),
            };
          });
          // Reconcile from the durable source after terminal persistence.
          // A full load is intentional: incremental loading after the
          // terminal sequence cannot rebuild events from earlier in the turn.
          void get().loadTimeline(sid, undefined, 0);
        } else {
          patchSession(sid, (prev) => {
            const turn = prev.activeTurn;
            const blocks = turn
              ? [...turn.assistantResponse.blocks]
              : [];
            if (turn) {
              applyWsToBlocks(blocks, ev, turn.assistantResponse.id);
            }
            return {
              ...prev,
              isRunning: false,
              error: re.error || (re.status === "cancelled" ? "Run cancelled" : "Run failed"),
              planApproval: null,
              streamingThought: "",
              completedTurns: turn
                ? [...prev.completedTurns, {
                    ...turn,
                    assistantResponse: {
                      ...turn.assistantResponse,
                      blocks,
                      status: "error" as const,
                    },
                    meta: {
                      ...turn.meta,
                      completedAt: Date.now(),
                      outcome: runOutcomeFromTerminal(re),
                    },
                  }]
                : prev.completedTurns,
              activeTurn: null,
            };
          });
          void get().loadTimeline(sid, undefined, 0);
        }
        return;
      }
      if (ev.type === "status") {
        if (ev.status === "running") {
                    patchSession(sid, (prev) => ({
            ...prev, isRunning: true, error: null,
          }));
        } else if (ev.status === "completed") {
          // Legacy path — run_terminal is the canonical completion signal.
          // Only set isRunning=false.  If activeTurn still lingers (abnormal),
          // defensively archive it.
          clearWatchdog();
          patchSession(sid, (prev) => {
            const turn = prev.activeTurn;
            return {
              ...prev,
              isRunning: false,
              steps: ev.result?.steps_taken ?? prev.steps,
              tokens: ev.result?.total_tokens ?? prev.tokens,
              streamingThought: "",
              activeTurn: null,
              completedTurns: turn
                ? upsertByTurnId(prev.completedTurns, {
                    ...turn,
                    assistantResponse: { ...turn.assistantResponse, status: "completed" as const },
                    meta: { ...turn.meta, completedAt: Date.now() },
                  })
                : prev.completedTurns,
            };
          });
          return;
        } else if (ev.status === "failed" || ev.status === "cancelled" || ev.status === "finish" || ev.status === "gave_up") {
          if (_isDuplicateTerminal(sid, (ev as { run_id?: string }).run_id || "")) return;
          clearWatchdog();
          patchSession(sid, (prev) => ({
            ...prev,
            isRunning: false,
            error: ev.status === "failed" ? (ev.error || "Execution failed") :
                   ev.status === "cancelled" ? (ev.error || ev.message || "Execution cancelled") : null,
            planApproval: null,
            streamingThought: "",
            // Terminal without success — move turn to completedTurns as-is.
            // loadTimeline will replace with canonical DB data shortly.
            completedTurns: prev.activeTurn
              ? [...prev.completedTurns, { ...prev.activeTurn, assistantResponse: { ...prev.activeTurn.assistantResponse, status: "error" as const } }]
              : prev.completedTurns,
            activeTurn: null,
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

      if (ev.type === "approval_resolved") {
        const rid = ev.request_id || "";
        patchSession(sid, (prev) => {
          const nextApprovals = { ...prev.toolApprovals };
          delete nextApprovals[rid];
          return {
            ...prev,
            toolApprovals: nextApprovals,
            timeline: [...prev.timeline, { source: "ws" as const, ws: ev }],
          };
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
            lifecycle: "waiting",
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
      _seenTerminalRunsBySession.delete(sid);
      _timelineRequestVersionBySession.delete(sid);
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
        if (!validIds.has(sessionId)) _seenTerminalRunsBySession.delete(sessionId);
        if (!validIds.has(sessionId)) _timelineRequestVersionBySession.delete(sessionId);
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

    sendChat: async (sessionId, prompt, intent, skill) => {
      if (get()._wsSessionId !== sessionId) return;
      ensureSession(sessionId);

      // Generate clientRequestId — same value used as POST idempotency_key.
      // This binds the optimistic turn to the server-created run/turn via the HTTP response.
      const clientRequestId = crypto.randomUUID
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`;

      patchSession(sessionId, (prev) => ({
        ...prev,
        isRunning: true,
        error: null,
        planApproval: prev.planApproval?.isWaiting ? prev.planApproval : null,
        // Defensive: normally run_terminal has already archived activeTurn.
        // If it's still here (agent crashed before emitting run_terminal), archive as error.
        completedTurns: prev.activeTurn
          ? upsertByTurnId(prev.completedTurns, {
              ...prev.activeTurn,
              assistantResponse: { ...prev.activeTurn.assistantResponse, status: "error" as const },
              meta: { ...prev.activeTurn.meta, completedAt: Date.now() },
            })
          : prev.completedTurns,
        activeTurn: createStreamingTurn(
          sessionId,
          Math.floor(Date.now() / 1000),
          prompt,
          clientRequestId,
        ),
      }));
      clearWatchdog();
      _watchdogTimer = setTimeout(() => {
        const current = selectSessionUi(get(), sessionId);
        if (current.isRunning) {
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
        if (!get().wsConnected) {
          const deadline = Date.now() + 3000;
          while (!get().wsConnected && Date.now() < deadline) {
            await new Promise((r) => setTimeout(r, 50));
          }
        }
        const { currentMode } = selectSessionUi(get(), sessionId);
        const result = await api.chat(
          sessionId,
          prompt,
          intent,
          agentNameForUiMode(currentMode),
          clientRequestId,
          skill,
          currentMode,
        );

        // ── Bind server turn_id / run_id to the optimistic activeTurn ──
        if (get()._wsSessionId === sessionId && result) {
          const turnId = (result as Record<string, unknown>).turn_id as string | undefined;
          const runId = (result as Record<string, unknown>).run_id as string | undefined;
          if (turnId) {
            patchSession(sessionId, (prev) => {
              if (!prev.activeTurn || prev.activeTurn.clientRequestId !== clientRequestId) return prev;
              return {
                ...prev,
                activeRunId: runId || "",
                activeTurn: { ...prev.activeTurn, turnId, runId: runId || "" },
              };
            });
          }
        }
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

    loadTimeline: async (sessionId, signal, afterSeq = 0, reconcileTurnId = "") => {
      const requestVersion = (
        _timelineRequestVersionBySession.get(sessionId) ?? 0
      ) + 1;
      _timelineRequestVersionBySession.set(sessionId, requestVersion);
      try {
        ensureSession(sessionId);
        const [response, multiAgentSnapshot] = await Promise.all([
          api.getTimeline(sessionId, signal, afterSeq),
          getMultiAgentSnapshot(sessionId, signal).catch(() => null),
        ]);
        if (_timelineRequestVersionBySession.get(sessionId) !== requestVersion) {
          return;
        }
        const snapshotEvents = multiAgentSnapshot
          ? delegationSnapshotEvents(
              multiAgentSnapshot.delegation_runs || [],
              multiAgentSnapshot.delegation_tasks || [],
            )
          : [];

        // ── Legacy events for timeline/plan_state compat ──
        const events = (response.items || [])
          .filter((item) => item.source === "ws")
          .map((item) => (item as { source: "ws"; event: WsMessage }).event);

        // Plan approval from backend-owned plan_state
        const planState = response.plan_state;
        let planApproval: PlanApproval | null = null;
        if (planState && (planState.lifecycle === "waiting" || planState.lifecycle === "saved") && planState.plan_text) {
          planApproval = {
            planText: planState.plan_text,
            isWaiting: true,
            sessionId,
            contract: planState.contract ?? null,
            revision: planState.revision,
            maxRevisions: planState.max_revisions,
            lifecycle: planState.lifecycle === "saved" ? "saved" : "waiting",
          };
        }

        // ── Build completedTurns directly from backend turn-grouped data ──
        // No more extractBlocksFromTimeline — the backend owns turn grouping.
        const dbTurns: StreamingTurn[] = (response.turns || []).map((turn) => {
          // Build assistant blocks from trace events via the unified builder
          const asstBlocks: ContentBlock[] = [];
          const asstId = `${turn.turn_id}_asst`;
          applyTraceEventsToBlocks(asstBlocks, turn.trace_events, asstId);
          // The durable final assistant message is canonical.  Reconcile it
          // even when streaming left an empty block or a pre-tool preamble.
          reconcileFinalTextBlock(
            asstBlocks,
            turn.assistant_message?.content || "",
          );
          const terminalEvent = turn.trace_events.find(
            (event): event is WsRunTerminalEvent =>
              event.type === "run_terminal",
          );

          const localId = `turn_${sessionId}_db_${turn.turn_id.slice(0, 8)}`;
          return {
            localId,
            turnId: turn.turn_id,
            runId: turn.run_id || "",
            clientRequestId: "",
            id: localId,
            userMessage: {
              id: `${turn.turn_id}_user`,
              blocks: turn.user_message?.content
                ? [{ type: "text" as const, content: turn.user_message.content }]
                : [{ type: "text" as const, content: "" }],
            },
            assistantResponse: {
              id: asstId,
              blocks: asstBlocks,
              status: "completed" as const,
            },
            meta: {
              steps: turn.meta.steps || 0,
              tokens: turn.meta.tokens || 0,
              startedAt: turn.meta.started_at ? new Date(turn.meta.started_at).getTime() : 0,
              completedAt: turn.meta.completed_at ? new Date(turn.meta.completed_at).getTime() : undefined,
              eventSeq: 0,
              hasGap: false,
              outcome: terminalEvent
                ? runOutcomeFromTerminal(terminalEvent)
                : {
                    status: (
                      [
                        "failed", "cancelled", "partial", "gave_up", "blocked",
                      ].includes(turn.meta.status)
                        ? turn.meta.status as RunOutcome["status"]
                        : "completed"
                    ),
                    terminationReason: turn.meta.termination_reason,
                    verification: turn.meta.verification,
                    workspaceDelta: turn.meta.workspace_delta,
                    error: turn.meta.error,
                    runId: turn.run_id || "",
                  },
            },
          };
        });

        // Legacy timeline items (for backward compat)
        const timelineItems = response.items.map((item) => (
          item.source === "message"
            ? { source: "message" as const, msg: (item as { source: "message"; message: Message }).message }
            : { source: "ws" as const, ws: { ...(item as { source: "ws"; event: WsMessage; seq?: number }).event, seq: item.seq } }
        ));

        patchSession(sessionId, (prev) => {
            // A terminal local state must not retain an old active turn when
            // the authoritative response has no active run. Preserve only an
            // optimistic/live turn while the local run is still in progress.
            const activeRun = response.active_run;
            let activeTurn = activeRun
              ? prev.activeTurn
              : (prev.isRunning ? prev.activeTurn : null);
            if (activeRun) {
              const restored = dbTurns.find(
                (turn) =>
                  turn.runId === activeRun.run_id ||
                  turn.turnId === activeRun.turn_id,
              );
              if (restored) {
                activeTurn = {
                  ...restored,
                  assistantResponse: {
                    ...restored.assistantResponse,
                    status: "streaming" as const,
                  },
                };
              } else if (!activeTurn) {
                activeTurn = createStreamingTurn(
                  sessionId,
                  activeRun.turn_index || Math.floor(Date.now() / 1000),
                  activeRun.prompt || "",
                  "",
                );
                activeTurn = {
                  ...activeTurn,
                  turnId: activeRun.turn_id || "",
                  runId: activeRun.run_id,
                };
              }
            }
            const activeTurnId = activeTurn?.turnId || "";

          // Filter out the turn matching live activeTurn
          const newCompleted = dbTurns.filter((t) => t.turnId !== activeTurnId);

          return {
            ...prev,
            delegationRuns: rebuildDelegationRuns(
              snapshotEvents,
              rebuildDelegationRuns(
                events,
                afterSeq > 0 ? prev.delegationRuns : {},
              ),
            ),
            resourceGovernance: (multiAgentSnapshot as unknown as Record<string, unknown> | null)?.resource as Record<string, unknown> ?? prev.resourceGovernance,
            events: afterSeq > 0
              ? [...events.slice().reverse(), ...prev.events].slice(0, 100)
              : events.slice().reverse().slice(0, 100),
            timeline: mergeTimelineItems(afterSeq > 0 ? prev.timeline : [], timelineItems),
            planApproval: afterSeq > 0
              ? prev.planApproval
              : (planState ? planApproval : prev.planApproval),
            lastTraceSeq: Math.max(prev.lastTraceSeq, response.last_seq || 0),
            lastAppliedSequence: afterSeq > 0
              ? prev.lastAppliedSequence
              : Math.max(prev.lastAppliedSequence, response.last_seq || 0),
            isRunning: Boolean(activeRun),
            activeRunId: activeRun?.run_id || "",
            activeTurn,
            completedTurns: afterSeq > 0
              ? mergeTurnsByTurnId(prev.completedTurns, newCompleted)
              : newCompleted,
          };
        });
      } catch (e: unknown) {
        if (_timelineRequestVersionBySession.get(sessionId) !== requestVersion) {
          return;
        }
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
        const [events, multiAgentSnapshot] = await Promise.all([
          api.getTraceEvents(sessionId, 0, 200, signal, afterSeq),
          getMultiAgentSnapshot(sessionId, signal).catch(() => null),
        ]);
        const snapshotEvents = multiAgentSnapshot
          ? delegationSnapshotEvents(
              multiAgentSnapshot.delegation_runs || [],
              multiAgentSnapshot.delegation_tasks || [],
            )
          : [];
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
            delegationRuns: rebuildDelegationRuns(
              snapshotEvents,
              rebuildDelegationRuns(
                events,
                afterSeq > 0 ? prev.delegationRuns : {},
              ),
            ),
            resourceGovernance: (multiAgentSnapshot as unknown as Record<string, unknown> | null)?.resource as Record<string, unknown> ?? prev.resourceGovernance,
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
        patchSession(sid, (prev) => ({
          ...prev,
          isRunning: false,
          planApproval: prev.planApproval
            ? {
                ...prev.planApproval,
                isWaiting: true,
                lifecycle: "saved",
              }
            : prev.planApproval,
        }));
        // Refresh from the durable saved lifecycle. loadTimeline also
        // converges isRunning=false when no active run exists.
        await get().loadTimeline(sid, undefined, 0);
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
