import { afterEach, describe, expect, it, vi } from "vitest";

import * as multiAgentApi from "../api/multiAgent";
import * as sessionsApi from "../api/sessions";
import type { ContentBlock } from "../types/blocks";
import type { WsMessage } from "../types/events";
import type { TimelineResponse } from "../types/session";
import {
  applyTraceEventsToBlocks,
  applyWsToBlocks,
  selectSessionUi,
  useChatStore,
} from "./chatStore";


afterEach(() => {
  vi.restoreAllMocks();
  useChatStore.setState({
    sessionStateById: {},
    _wsSessionId: null,
  });
});


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

  it("reconciles the real thought-delta, answer, full-thought event order", () => {
    const blocks: ContentBlock[] = [];
    const events = [
      { type: "thought_delta", text: "The user said hello. " },
      { type: "thought_delta", text: "Respond politely." },
      { type: "assistant_text_start", block_id: "text-1" },
      {
        type: "assistant_text_delta",
        block_id: "text-1",
        text: "你好！有什么可以帮你的吗？",
      },
      { type: "assistant_text_end", block_id: "text-1" },
      {
        type: "thought",
        content: "The user said hello. Respond politely.",
        step: 1,
      },
      {
        type: "run_terminal",
        status: "completed",
        summary: "你好！有什么可以帮你的吗？",
      },
    ] as WsMessage[];

    applyTraceEventsToBlocks(blocks, events, "assistant-real-order");

    expect(blocks.map((block) => block.type)).toEqual(["thought", "text"]);
    expect(blocks[0]).toMatchObject({
      type: "thought",
      content: "The user said hello. Respond politely.",
      phase: "completed",
    });
    expect(blocks[1]).toMatchObject({
      type: "text",
      content: "你好！有什么可以帮你的吗？",
      phase: "completed",
    });
  });
});


describe("loadTimeline completion races", () => {
  it("ignores an older active-run response after a newer terminal response", async () => {
    let resolveOld!: (value: TimelineResponse) => void;
    let resolveNew!: (value: TimelineResponse) => void;
    const oldResponse = new Promise<TimelineResponse>((resolve) => {
      resolveOld = resolve;
    });
    const newResponse = new Promise<TimelineResponse>((resolve) => {
      resolveNew = resolve;
    });

    vi.spyOn(sessionsApi, "getTimeline")
      .mockReturnValueOnce(oldResponse)
      .mockReturnValueOnce(newResponse);
    vi.spyOn(multiAgentApi, "getMultiAgentSnapshot").mockResolvedValue(null);

    const sessionId = "timeline-race";
    const firstLoad = useChatStore.getState().loadTimeline(sessionId);
    const secondLoad = useChatStore.getState().loadTimeline(sessionId);

    resolveNew({
      session_id: sessionId,
      turns: [],
      items: [],
      last_seq: 22,
      has_more: false,
      active_run: null,
    });
    await secondLoad;

    resolveOld({
      session_id: sessionId,
      turns: [],
      items: [],
      last_seq: 18,
      has_more: false,
      active_run: {
        run_id: "old-running-run",
        turn_id: "old-running-turn",
        turn_index: 1,
        prompt: "你好",
        status: "running",
      },
    });
    await firstLoad;

    const state = selectSessionUi(useChatStore.getState(), sessionId);
    expect(state.isRunning).toBe(false);
    expect(state.activeTurn).toBeNull();
  });
});
