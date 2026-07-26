/**
 * Unit tests for ContentBlock data model — the "constitution" tests.
 * These must pass before any UI code is written.
 */
// Run with: npx vitest run src/types/blocks.test.ts
import { describe, it, expect } from "vitest";
import {
  blockId,
  canGroupWith,
  reconcileFinalTextBlock,
  toolUseSummary,
  type ToolUseBlock,
  type ContentBlock,
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

describe("reconcileFinalTextBlock", () => {
  it("fills an empty trailing streaming text block", () => {
    const blocks: ContentBlock[] = [
      { type: "thought", content: "work", summary: "work", phase: "completed" },
      { type: "text", content: "", blockId: "answer", phase: "streaming" },
    ];

    reconcileFinalTextBlock(blocks, "Final answer");

    expect(blocks).toHaveLength(2);
    expect(blocks[1]).toMatchObject({
      type: "text",
      content: "Final answer",
      phase: "completed",
    });
  });

  it("appends the final answer when the only text was before a tool", () => {
    const blocks: ContentBlock[] = [
      { type: "text", content: "I will inspect it.", phase: "completed" },
      { type: "tool_use", id: "tc-1", name: "Read", input: {}, status: "success" },
    ];

    reconcileFinalTextBlock(blocks, "Final answer");

    expect(blocks).toHaveLength(3);
    expect(blocks[2]).toMatchObject({ type: "text", content: "Final answer" });
  });

  it("makes the durable final message authoritative for the trailing text slot", () => {
    const blocks: ContentBlock[] = [
      { type: "tool_use", id: "tc-1", name: "Read", input: {}, status: "success" },
      { type: "text", content: "Partial ans", phase: "streaming" },
    ];

    reconcileFinalTextBlock(blocks, "Complete final answer");

    expect(blocks).toHaveLength(2);
    expect(blocks[1]).toMatchObject({
      type: "text",
      content: "Complete final answer",
      phase: "completed",
    });
  });

  it("strips GraceCode's legacy verification banner from persisted answers", () => {
    const blocks: ContentBlock[] = [];

    reconcileFinalTextBlock(
      blocks,
      "[UNVERIFIED — no test environment available. " +
        "Code changes were made but NOT independently verified.]\n\n" +
        "Actual answer",
    );

    expect(blocks).toEqual([
      { type: "text", content: "Actual answer", phase: "completed" },
    ]);
  });
});
