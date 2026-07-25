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

// ── Block types ──────────────────────────────────────────────────────────

export interface TextBlock {
  type: "text";
  content: string; // Markdown
}

export interface ThoughtBlock {
  type: "thought";
  content: string; // raw thought text
  summary: string; // smart summary (not "Thinking..."), populated on completion
  phase: "streaming" | "completed";
}

export interface ToolUseBlock {
  type: "tool_use";
  id: string; // stable ID: b_{messageId}_{index}
  name: string; // tool name (Read, Write, Bash, ...)
  input: Record<string, unknown>; // tool call params
  status: "running" | "success" | "error";

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
}

export type ContentBlock = TextBlock | ThoughtBlock | ToolUseBlock;

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

// ── Integrity check ──────────────────────────────────────────────────────

export interface IntegrityCheck {
  wsBlockCount: number;
  dbBlockCount: number;
  wsLastBlockHash: string; // SHA-256 first 8 hex chars of last block JSON
  dbLastBlockHash: string;
}

/**
 * Returns true when WS and DB data agree — safe for silent attribute replacement.
 * Returns false when structural mismatch detected — full message remount needed.
 */
export function integrityPasses(check: IntegrityCheck): boolean {
  return (
    check.wsBlockCount === check.dbBlockCount &&
    check.wsLastBlockHash === check.dbLastBlockHash
  );
}

// ── Block helpers ────────────────────────────────────────────────────────

let _blockSeq = 0;

/** Generate a stable block ID. */
export function blockId(messageId: string, index: number): string {
  return `b_${messageId}_${index}`;
}

/** Lightweight hash of a block's JSON representation (first 8 hex chars). */
export function blockHash(block: ContentBlock): string {
  const json = JSON.stringify(block);
  let hash = 0;
  for (let i = 0; i < json.length; i++) {
    hash = ((hash << 5) - hash + json.charCodeAt(i)) | 0;
  }
  return (hash >>> 0).toString(16).padStart(8, "0").slice(0, 8);
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
  /** Stable turn ID: turn_{sessionId}_{generation}. Survives streaming→DB. */
  id: string;

  userMessage: {
    /** tempId before DB load, real message ID after */
    id: string;
    blocks: ContentBlock[];
  };

  assistantResponse: {
    /** tempId before DB load, real message ID after */
    id: string;
    blocks: ContentBlock[];
    status: "streaming" | "completed" | "error";
  };

  meta: {
    steps: number;
    tokens: number;
    startedAt: number;
    completedAt?: number;
    /** Monotonic WS event counter, starts at 1. */
    eventSeq: number;
    /** True when seq discontinuity detected — forces remount on DB load. */
    hasGap: boolean;
  };
}

/** Create a fresh StreamingTurn for a new chat request. */
export function createStreamingTurn(
  sessionId: string,
  generation: number,
  prompt: string,
): StreamingTurn {
  const now = Date.now();
  const tempUserId = `temp_u_${now}`;
  const tempAsstId = `temp_a_${now}`;
  return {
    id: `turn_${sessionId}_${generation}`,
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

