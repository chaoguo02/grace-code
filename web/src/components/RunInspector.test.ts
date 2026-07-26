import { describe, expect, it } from "vitest";

import { deriveToolUsage } from "./RunInspector";
import type { WsMessage } from "../types";


describe("deriveToolUsage", () => {
  it("groups tool calls and pairs successful and failed observations", () => {
    const events: WsMessage[] = [
      { type: "tool_call", name: "Read", id: "call-1" },
      { type: "tool_call", name: "Read", id: "call-2" },
      { type: "tool_call", name: "mcp__docs__lookup", id: "call-3" },
      { type: "observation", tool_name: "Read", id: "call-1", status: "success" },
      { type: "observation", tool_name: "Read", id: "call-2", status: "failed", error: "blocked" },
      { type: "observation", tool_name: "mcp__docs__lookup", id: "call-3", status: "success" },
    ];

    expect(deriveToolUsage(events)).toEqual([
      { name: "Read", calls: 2, successes: 1, failures: 1 },
      { name: "mcp__docs__lookup", calls: 1, successes: 1, failures: 0 },
    ]);
  });
});
