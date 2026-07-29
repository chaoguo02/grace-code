import { describe, expect, it } from "vitest";

import type { ContentBlock } from "../types/blocks";
import type { WsMessage } from "../types/events";
import { applyTraceEventsToBlocks, applyWsToBlocks } from "./chatStore";


describe("applyWsToBlocks", () => {
  it("coalesces reasoning and preserves a visible tool call with its result", () => {
    const blocks: ContentBlock[] = [];
    const apply = (event: WsMessage) =>
      applyWsToBlocks(blocks, event, "assistant-1");

    apply({ type: "thought_delta", text: "Inspect " } as WsMessage);
    apply({ type: "thought_delta", text: "the directory." } as WsMessage);
    apply({
      type: "thought",
      content: "Inspect the directory.",
      step: 1,
    } as WsMessage);
    apply({
      type: "tool_call",
      id: "call-1",
      name: "Bash",
      params: { command: "count docs" },
      step: 1,
    } as WsMessage);
    apply({
      type: "observation",
      id: "call-1",
      tool_name: "Bash",
      output: "75",
      status: "success",
      step: 1,
    } as WsMessage);
    apply({
      type: "assistant_text_start",
      block_id: "text-1",
    } as WsMessage);
    apply({
      type: "assistant_text_delta",
      block_id: "text-1",
      text: "共有 75 个文件。",
    } as WsMessage);
    apply({
      type: "assistant_text_end",
      block_id: "text-1",
    } as WsMessage);

    expect(blocks).toHaveLength(3);
    expect(blocks[0]).toMatchObject({
      type: "thought",
      content: "Inspect the directory.",
      phase: "completed",
    });
    expect(blocks[1]).toMatchObject({
      type: "tool_use",
      id: "call-1",
      name: "Bash",
      status: "success",
      output: "75",
    });
    expect(blocks[2]).toMatchObject({
      type: "text",
      content: "共有 75 个文件。",
      phase: "completed",
    });
  });

  it("hides legacy answer tokens mirrored into thought deltas", () => {
    const blocks: ContentBlock[] = [];
    const events = [
      { type: "thought_delta", text: "你" },
      { type: "assistant_text_start", block_id: "text-1" },
      { type: "assistant_text_delta", block_id: "text-1", text: "你" },
      { type: "thought_delta", text: "好" },
      { type: "assistant_text_delta", block_id: "text-1", text: "好" },
      { type: "assistant_text_end", block_id: "text-1" },
      { type: "thought", content: "Respond politely.", step: 1 },
    ] as WsMessage[];

    applyTraceEventsToBlocks(blocks, events, "assistant-legacy");

    expect(blocks.filter((block) => block.type === "text")).toEqual([
      {
        type: "text",
        content: "你好",
        blockId: "text-1",
        phase: "completed",
      },
    ]);
    expect(blocks.filter((block) => block.type === "thought")).toHaveLength(1);
  });
});
