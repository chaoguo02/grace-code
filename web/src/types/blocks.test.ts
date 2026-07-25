/**
 * Unit tests for ContentBlock data model — the "constitution" tests.
 * These must pass before any UI code is written.
 */
// Run with: npx vitest run src/types/blocks.test.ts
import { describe, it, expect } from "vitest";
import {
  blockId,
  blockHash,
  canGroupWith,
  toolUseSummary,
  integrityPasses,
  type ToolUseBlock,
  type ContentBlock,
  type IntegrityCheck,
} from "./blocks";

// ── blockId ──────────────────────────────────────────────────────────────

describe("blockId", () => {
  it("generates stable format b_{messageId}_{index}", () => {
    expect(blockId("msg_abc", 0)).toBe("b_msg_abc_0");
    expect(blockId("msg_abc", 5)).toBe("b_msg_abc_5");
  });

  it("is deterministic — same inputs → same id", () => {
    expect(blockId("x", 1)).toBe(blockId("x", 1));
  });

  it("different index → different id", () => {
    expect(blockId("x", 0)).not.toBe(blockId("x", 1));
  });
});

// ── blockHash ────────────────────────────────────────────────────────────

describe("blockHash", () => {
  it("produces 8-char hex string", () => {
    const b: ToolUseBlock = {
      type: "tool_use", id: "b_x_0", name: "Read",
      input: { file_path: "test.ts" }, status: "success",
    };
    const hash = blockHash(b);
    expect(hash).toHaveLength(8);
    expect(/^[0-9a-f]{8}$/.test(hash)).toBe(true);
  });

  it("same content → same hash", () => {
    const a: ToolUseBlock = { type: "tool_use", id: "b_x_0", name: "Read", input: {}, status: "success" };
    const b: ToolUseBlock = { type: "tool_use", id: "b_x_0", name: "Read", input: {}, status: "success" };
    expect(blockHash(a)).toBe(blockHash(b));
  });

  it("different content → different hash", () => {
    const a: ToolUseBlock = { type: "tool_use", id: "b_x_0", name: "Read", input: {}, status: "success" };
    const b: ToolUseBlock = { type: "tool_use", id: "b_x_0", name: "Write", input: {}, status: "success" };
    expect(blockHash(a)).not.toBe(blockHash(b));
  });
});

// ── canGroupWith ─────────────────────────────────────────────────────────

describe("canGroupWith", () => {
  const base: ToolUseBlock = { type: "tool_use", id: "b_0", name: "Read", input: {}, status: "success" };

  it("groups same-name, both success", () => {
    expect(canGroupWith(base, { ...base, id: "b_1" })).toBe(true);
  });

  it("rejects different tool name", () => {
    expect(canGroupWith(base, { ...base, id: "b_1", name: "Grep" })).toBe(false);
  });

  it("rejects if either has error status", () => {
    expect(canGroupWith(base, { ...base, id: "b_1", status: "error" })).toBe(false);
    expect(canGroupWith({ ...base, status: "error" }, { ...base, id: "b_1" })).toBe(false);
  });

  it("rejects if either is a retry", () => {
    expect(canGroupWith(base, { ...base, id: "b_1", retryOf: "b_0" })).toBe(false);
  });
});

// ── toolUseSummary ───────────────────────────────────────────────────────

describe("toolUseSummary", () => {
  it("returns file_path when present", () => {
    const b: ToolUseBlock = { type: "tool_use", id: "b_0", name: "Read", input: { file_path: "src/app.ts" }, status: "success" };
    expect(toolUseSummary(b)).toBe("src/app.ts");
  });

  it("returns command when present", () => {
    const b: ToolUseBlock = { type: "tool_use", id: "b_0", name: "Bash", input: { command: "npm test" }, status: "success" };
    expect(toolUseSummary(b)).toBe("npm test");
  });

  it("truncates long values to 60 chars", () => {
    const long = "a".repeat(100);
    const b: ToolUseBlock = { type: "tool_use", id: "b_0", name: "Read", input: { file_path: long }, status: "success" };
    const s = toolUseSummary(b);
    expect(s.length).toBeLessThanOrEqual(63); // 60 + "…"
    expect(s.endsWith("…")).toBe(true);
  });

  it("returns empty string when no known param", () => {
    const b: ToolUseBlock = { type: "tool_use", id: "b_0", name: "Skill", input: {}, status: "success" };
    expect(toolUseSummary(b)).toBe("");
  });
});

// ── integrityPasses ──────────────────────────────────────────────────────

describe("integrityPasses", () => {
  const passing: IntegrityCheck = {
    wsBlockCount: 5, dbBlockCount: 5,
    wsLastBlockHash: "abc12345", dbLastBlockHash: "abc12345",
  };

  it("passes when counts and hashes match", () => {
    expect(integrityPasses(passing)).toBe(true);
  });

  it("fails when counts differ", () => {
    expect(integrityPasses({ ...passing, dbBlockCount: 6 })).toBe(false);
  });

  it("fails when hashes differ", () => {
    expect(integrityPasses({ ...passing, dbLastBlockHash: "deadbeef" })).toBe(false);
  });

  it("fails when both differ", () => {
    expect(integrityPasses({ ...passing, wsBlockCount: 3, dbLastBlockHash: "bad" })).toBe(false);
  });
});

// ── Type narrowing (compile-time) ────────────────────────────────────────

describe("ContentBlock type narrowing", () => {
  it("text block has content string", () => {
    const b: ContentBlock = { type: "text", content: "hello" };
    if (b.type === "text") {
      expect(typeof b.content).toBe("string");
    }
  });

  it("thought block has phase field", () => {
    const b: ContentBlock = { type: "thought", content: "hmm", summary: "", phase: "streaming" };
    if (b.type === "thought") {
      expect(b.phase).toBeDefined();
    }
  });

  it("tool_use block has stable id", () => {
    const b: ContentBlock = { type: "tool_use", id: "b_x_0", name: "Read", input: {}, status: "running" };
    if (b.type === "tool_use") {
      expect(b.id).toMatch(/^b_/);
    }
  });
});
