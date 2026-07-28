import { test, expect } from "playwright/test";

const SESSION_ID = "sess-123";

function sessionsPayload() {
  return [
    {
      id: SESSION_ID,
      agent_name: "build",
      title: "Batch 1 Markdown Session",
      status: "completed",
      mode: "build",
      summary: "# Plan\n\nThis plan contains **bold** text, a [link](https://example.com), a table, and code.",
      error: "",
      parent_id: null,
      created_at: "2026-07-22T10:00:00.000Z",
      updated_at: "2026-07-22T10:00:00.000Z",
      completed_at: "2026-07-22T10:01:00.000Z",
      message_count: 3,
      total_tokens_estimate: 1200,
    },
  ];
}

function sessionDetail() {
  return {
    id: SESSION_ID,
    parent_id: null,
    root_id: SESSION_ID,
    agent_name: "build",
    title: "Batch 1 Markdown Session",
    status: "completed",
    mode: "build",
    summary: "# Plan\n\nThis plan contains **bold** text, a [link](https://example.com), a table, and code.",
    error: "",
    agent_kind: "primary",
    context_origin: "user",
    execution_placement: "local",
    workspace_mode: "workspace",
    agent_depth: 0,
    generation: 0,
    created_at: "2026-07-22T10:00:00.000Z",
    updated_at: "2026-07-22T10:01:00.000Z",
    completed_at: "2026-07-22T10:01:00.000Z",
    metadata: { plan_revision: 1 },
    worktree_disposition: null,
    message_count: 3,
    total_tokens_estimate: 1200,
  };
}

function messagesPayload() {
  return [
    {
      role: "assistant",
      content: "# Hello\n\nHere is a code block:\n\n```ts\nconst answer = 42;\n```\n\nAnd a table:\n\n| Name | Value |\n| --- | --- |\n| Foo | Bar |\n\nA [link](https://example.com) is here.",
      created_at: "2026-07-22T10:00:30.000Z",
      tool_calls: [],
    },
  ];
}

function tracePayload() {
  return [
    {
      type: "plan_ready",
      plan_text: "# Review\n\nSee [docs](https://example.com).\n\n- [x] Collect files\n- [ ] Implement changes\n\n```json\n{\"goal\": \"batch 1\"}\n```",
      contract: { goal: "batch 1", steps: ["collect", "implement"] },
      revision: 1,
      max_revisions: 5,
      result: { summary: "done", steps_taken: 3, total_tokens: 1200 },
      timestamp: "2026-07-22T10:01:00.000Z",
    },
  ];
}

test.beforeEach(async ({ page }) => {
  await page.route("**/api/sessions?limit=50", async (route) => {
    await route.fulfill({ json: sessionsPayload() });
  });
  await page.route(`**/api/sessions/${SESSION_ID}`, async (route) => {
    await route.fulfill({ json: sessionDetail() });
  });
  await page.route(`**/api/sessions/${SESSION_ID}/messages`, async (route) => {
    await route.fulfill({ json: messagesPayload() });
  });
  await page.route(`**/api/sessions/${SESSION_ID}/trace/events?after=0&limit=200`, async (route) => {
    await route.fulfill({ json: tracePayload() });
  });
  await page.route(`**/api/sessions/${SESSION_ID}/timeline?limit=200`, async (route) => {
    const timelinePayload = {
      session_id: SESSION_ID,
      items: [
        ...messagesPayload().map((m: Record<string, unknown>) => ({ source: "message", timestamp: m.created_at || "", message: m })),
        ...tracePayload().map((e: Record<string, unknown>, i: number) => ({ source: "ws", timestamp: e.timestamp || "", seq: i + 1, event: { ...e, seq: i + 1 } })),
      ].sort((a: Record<string, unknown>, b: Record<string, unknown>) => String(a.timestamp || "").localeCompare(String(b.timestamp || ""))),
      last_seq: tracePayload().length,
      has_more: false,
      turns: [{
        turn_id: "fixture-turn",
        run_id: "fixture-run",
        turn_index: 0,
        user_message: null,
        assistant_message: messagesPayload()[0],
        trace_events: tracePayload(),
        meta: {
          steps: 3, tokens: 1200, status: "completed",
          started_at: "2026-07-22T10:00:00.000Z",
          completed_at: "2026-07-22T10:01:00.000Z",
          termination_reason: "goal_achieved",
          verification: { status: "verified", reason: "tests_passed", checks: [] },
          workspace_delta: {},
        },
      }],
      active_run: null,
      plan_state: { lifecycle: "none", plan_text: "", revision: 0, max_revisions: 5 },
    };
    await route.fulfill({ json: timelinePayload });
  });
  await page.route(`**/api/sessions/${SESSION_ID}/tree`, async (route) => {
    await route.fulfill({
      json: {
        id: SESSION_ID,
        agent_name: "plan",
        title: "Batch 1 Markdown Session",
        status: "completed",
        depth: 0,
        parent_id: null,
        created_at: "2026-07-22T10:00:00.000Z",
        children: [],
        child_count: 0,
      },
    });
  });
  await page.route(`**/api/sessions/${SESSION_ID}/plan`, async (route) => {
    await route.fulfill({ json: { session_id: SESSION_ID, content: "", has_plan: false } });
  });
  await page.route("**/api/config/models", async (route) => {
    await route.fulfill({
      json: [
        { key: "deepseek-v4-flash", family: "Fast", note: "Quick iteration" },
      ],
    });
  });
  await page.route("**/api/skills", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/storage/stats", async (route) => {
    await route.fulfill({
      json: { backend: "sqlite", total_sessions: 1, total_messages: 1, total_memories: 0, db_size_bytes: 1024 },
    });
  });
  await page.route("**/api/sessions/sess-123/stats", async (route) => {
    await route.fulfill({ json: { steps_taken: 3, max_steps: 10, total_tokens: 1200, duration_seconds: 60, tools: { Read: 1 } } });
  });
  await page.route("**/api/sessions/sess-123/approve", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ json: { approved: true, session_id: SESSION_ID, message: "Build started with plan context" } });
      return;
    }
    await route.fallback();
  });
});

test("renders markdown code blocks, tables, and links", async ({ page }) => {
  await page.goto("/");
  await page.getByText("Batch 1 Markdown Session").click();

  await expect(page.locator(".blocks-text pre code")).toContainText("const answer = 42;");
  await expect(page.locator(".blocks-text table")).toContainText("Foo");
  await expect(page.locator(".blocks-text a")).toHaveAttribute("href", "https://example.com");

});

test("sends the expected chat request payload", async ({ page }) => {
  const requests: Array<Record<string, unknown>> = [];
  await page.route(`**/api/sessions/${SESSION_ID}/trace/events?after=0&limit=200`, async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route(`**/api/sessions/${SESSION_ID}/messages`, async (route) => {
    if (route.request().method() === "POST") {
      requests.push(route.request().postDataJSON() as Record<string, unknown>);
      await route.fulfill({ json: { session_id: SESSION_ID, status: "accepted", summary: "queued", steps_taken: 0, total_tokens: 0, error: null, termination_reason: null } });
      return;
    }
    await route.fulfill({ json: messagesPayload() });
  });

  await page.goto("/");
  await page.getByText("Batch 1 Markdown Session").click();
  await expect(page.locator("#prompt-input")).toBeVisible();
  await page.locator("#prompt-input").fill("Please analyze the codebase");
  await page.locator(".composer-send-btn").click();

  await expect.poll(() => requests.length).toBe(1);
  expect(requests[0]).toMatchObject({
    prompt: "Please analyze the codebase",
    agent_name: "build",
  });
});
