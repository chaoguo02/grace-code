/**
 * ContentBlock data model — the "constitution" of the chat display redesign.
 *
 * A single assistant message is composed of ordered ContentBlocks.
 * The rendering layer simply iterates blocks — no regex parsing, no
 * cross-referencing WS events with DB messages.
 *
 * ID convention:   b_{messageId}_{index}   (e.g. "b_msg123_0")
 * Stable across streaming → DB transition, survives remount.
 */

import type { EvidenceRef, RunVerification, RunWorkspaceDelta } from "./events";

// ── Block types ──────────────────────────────────────────────────────────

export interface TextBlock {
  type: "text";
  content: string; // Markdown
  blockId?: string; // server-assigned block_id (from assistant_text_start)
  phase?: "streaming" | "completed"; // streaming lifecycle
}

export interface ThoughtBlock {
  type: "thought";
  content: string; // raw thought text
  summary: string; // smart summary (not "Thinking..."), populated on completion
  phase: "streaming" | "completed";
  blockId?: string; // server-assigned block_id
}

export interface ToolUseBlock {
  type: "tool_use";
  id: string; // stable ID: tool_call_id (model-assigned)
  name: string; // tool name (Read, Write, Bash, ...)
  input: Record<string, unknown>; // tool call params
  status: "running" | "success" | "error";
  blockId?: string; // = tool_call_id

  output?: string; // observation output (success)
  error?: string; // observation error (failure)
  outputSize?: number; // byte size, for truncation decision

  // ── Smart grouping (P1) ──
  groupedWith?: string[]; // IDs of merged-into-this tool_use blocks
  groupLabel?: string; // e.g. "Read 5 files"

  // ── Retry tracking (P1) ──
  retryOf?: string; // block ID of the original failed attempt
  retrySucceeded?: boolean; // whether the retry succeeded

  // ── Anchor targets (P2) ──
  anchorTargets?: string[]; // file paths this tool touched, for ref-backlinks
  evidence?: EvidenceRef;
}

export type ContentBlock = TextBlock | ThoughtBlock | ToolUseBlock;

const LEGACY_UNVERIFIED_PREFIX =
  /^\[UNVERIFIED — (?:no test environment available|project has no Git fact source|tests ran but failed|test\/validation did not run or was unavailable)\. Code changes were made but NOT independently verified\.\]\r?\n\r?\n/;

/**
 * Reconcile the durable final assistant message into an ordered block list.
 *
 * Streaming may leave an empty text_start block, or may emit a short preamble
 * before the final tool call without producing a later text block.  In both
 * cases merely checking for the existence of any text block hides the final
 * answer.  A text block after the final non-text block is the final-answer
 * slot; otherwise append a new one.
 */
export function reconcileFinalTextBlock(
  blocks: ContentBlock[],
  finalContent: string,
): void {
  const cleanFinalContent = finalContent.replace(LEGACY_UNVERIFIED_PREFIX, "");
  if (!cleanFinalContent.trim()) return;

  let lastTextIndex = -1;
  let lastNonTextIndex = -1;
  for (let i = 0; i < blocks.length; i++) {
    if (blocks[i].type === "text") {
      lastTextIndex = i;
    } else {
      lastNonTextIndex = i;
    }
  }

  if (lastTextIndex >= 0 && lastTextIndex > lastNonTextIndex) {
    const current = blocks[lastTextIndex];
    if (current.type === "text") {
      current.content = cleanFinalContent;
      current.phase = "completed";
    }
    return;
  }

  blocks.push({ type: "text", content: cleanFinalContent, phase: "completed" });
}

// ── Message with blocks ──────────────────────────────────────────────────

export interface BlocksMessage {
  id: string; // same as DB message id (or streaming temp id)
  role: "assistant";
  blocks: ContentBlock[];
  metadata: BlocksMessageMeta;
}

export interface BlocksMessageMeta {
  steps: number;
  tokens: number;
  durationMs: number;
  agentName?: string;
  mode?: string;
}

// ── Block helpers ────────────────────────────────────────────────────────

let _blockSeq = 0;

/** Generate a stable block ID (fallback when server doesn't provide one). */
export function blockId(messageId: string, index: number): string {
  return `b_${messageId}_${index}`;
}

/** Check if a block can be merged with its predecessor (smart grouping). */
export function canGroupWith(
  current: ToolUseBlock,
  previous: ToolUseBlock,
): boolean {
  return (
    current.name === previous.name &&
    current.status === "success" &&
    previous.status === "success" &&
    !current.retryOf &&
    !previous.retryOf
  );
}

/** Return a human-readable summary for a tool_use block. */
export function toolUseSummary(block: ToolUseBlock): string {
  const primary = block.input?.file_path
    || block.input?.path
    || block.input?.command
    || block.input?.pattern
    || block.input?.query
    || "";
  const str = typeof primary === "string" ? primary : JSON.stringify(primary);
  const max = 60;
  return str.length > max ? str.slice(0, max) + "…" : str;
}

// ── StreamingTurn — lifecycle-managed streaming context ──────────────────

/**
 * A single conversation turn: one user message + its assistant response.
 *
 * Created by sendChat, mutated by WS events, finalized by loadTimeline.
 * This replaces the three-array patchwork (streamingBlocks + dbBlocksByMsgId
 * + timeline) with a single lifecycle-managed domain object.
 */
export interface StreamingTurn {
  /** Frontend-local ID — stable React key, never used for merge/dedup. */
  localId: string;

  /** Server-assigned UUID — the authoritative identity across live + DB paths.
   *  Bound from POST /messages 202 response.  Empty until HTTP response arrives. */
  turnId: string;

  /** Server-assigned run UUID — bound from POST response. */
  runId: string;

  /** Client-generated — matches POST idempotency_key, used to bind HTTP response. */
  clientRequestId: string;

  /** Legacy: included for backward compat with code that reads .id. */
  id: string;

  userMessage: {
    id: string;
    blocks: ContentBlock[];
  };

  assistantResponse: {
    id: string;
    blocks: ContentBlock[];
    status: "streaming" | "completed" | "error";
  };

  meta: {
    steps: number;
    tokens: number;
    startedAt: number;
    completedAt?: number;
    eventSeq: number;
    hasGap: boolean;
    outcome?: RunOutcome;
  };
}

export interface RunOutcome {
  status:
    | "completed"
    | "failed"
    | "cancelled"
    | "partial"
    | "gave_up"
    | "blocked";
  terminationReason?: string;
  verification?: RunVerification;
  workspaceDelta?: RunWorkspaceDelta;
  evidenceSummary?: {
    total: number;
    by_kind: Record<string, number>;
    failed: number;
  };
  error?: string;
  runId?: string;
}

/** Create a fresh StreamingTurn for a new chat request. */
export function createStreamingTurn(
  sessionId: string,
  generation: number,
  prompt: string,
  clientRequestId: string,
): StreamingTurn {
  const now = Date.now();
  const localId = `turn_${sessionId}_${generation}`;
  const tempUserId = `temp_u_${now}`;
  const tempAsstId = `temp_a_${now}`;
  return {
    localId,
    turnId: "",
    runId: "",
    clientRequestId,
    id: localId,
    userMessage: {
      id: tempUserId,
      blocks: [{ type: "text", content: prompt }],
    },
    assistantResponse: {
      id: tempAsstId,
      blocks: [],
      status: "streaming",
    },
    meta: {
      steps: 0,
      tokens: 0,
      startedAt: now,
      eventSeq: 0,
      hasGap: false,
    },
  };
}

// ── Turn merge helpers ──────────────────────────────────────────────────

/** Upsert a turn into an array, keyed by turnId. */
export function upsertByTurnId(
  turns: StreamingTurn[],
  turn: StreamingTurn,
): StreamingTurn[] {
  if (!turn.turnId) return [...turns, turn];
  const idx = turns.findIndex((t) => t.turnId === turn.turnId);
  if (idx >= 0) {
    const next = [...turns];
    next[idx] = turn;
    return next;
  }
  return [...turns, turn];
}

/** Merge DB turns into existing turns by turnId.
 *  DB text blocks are canonical (from persisted assistant_message).
 *  Live non-text blocks (tool calls, thoughts) are preserved when DB
 *  has fewer blocks — this handles post-completion sync where DB
 *  trace_events may be empty (afterSeq > 0 returns no new events). */
export function mergeTurnsByTurnId(
  current: StreamingTurn[],
  incoming: StreamingTurn[],
): StreamingTurn[] {
  const map = new Map(current.filter((t) => t.turnId).map((t) => [t.turnId, t]));

  for (const dbTurn of incoming) {
    if (!dbTurn.turnId) continue;
    const live = map.get(dbTurn.turnId);
    if (live) {
      const dbBlocks = dbTurn.assistantResponse.blocks;
      const liveBlocks = live.assistantResponse.blocks;

      // If DB has fewer blocks, it's a post-completion sync — supplement,
      // don't replace.  Only add DB text blocks missing from live.
      if (dbBlocks.length < liveBlocks.length) {
        const dbTexts = dbBlocks.filter((b) => b.type === "text");
        const hasLiveText = liveBlocks.some((b) => b.type === "text");
        map.set(dbTurn.turnId, {
          ...live,
          assistantResponse: {
            ...live.assistantResponse,
            blocks: hasLiveText
              ? liveBlocks
              : [...liveBlocks, ...dbTexts],
          },
        });
      } else {
        // DB has full data (afterSeq=0 reload) — DB is canonical
        const dbBlockIds = new Set(dbBlocks.map((b) => b.blockId).filter(Boolean));
        const liveOnlyBlocks = liveBlocks.filter(
          (b) => b.blockId && !dbBlockIds.has(b.blockId),
        );
        map.set(dbTurn.turnId, {
          ...dbTurn,
          assistantResponse: {
            ...dbTurn.assistantResponse,
            blocks: [...dbBlocks, ...liveOnlyBlocks],
          },
        });
      }
    } else {
      map.set(dbTurn.turnId, dbTurn);
    }
  }

  return [...map.values()];
}
