import { test, expect } from "playwright/test";

const SESSION_ID = "phase17-18-sess";

function sessionsPayload() {
  return [
    {
      id: SESSION_ID,
      agent_name: "build",
      title: "Phase 17-18 Workspace",
      status: "running",
      mode: "build",
      summary: "Working on the frontend workbench unification.",
      error: "",
      parent_id: null,
      created_at: "2026-07-22T10:00:00.000Z",
      updated_at: "2026-07-22T10:05:00.000Z",
      completed_at: null,
      message_count: 8,
      total_tokens_estimate: 4200,
    },
  ];
}

function sessionDetail() {
  return {
    id: SESSION_ID,
    parent_id: null,
    root_id: SESSION_ID,
    agent_name: "build",
    title: "Phase 17-18 Workspace",
    status: "running",
    mode: "build",
    summary: "Working on the frontend workbench unification.",
    error: "",
    agent_kind: "primary",
    context_origin: "user",
    execution_placement: "local",
    workspace_mode: "workspace",
    agent_depth: 0,
    generation: 0,
    created_at: "2026-07-22T10:00:00.000Z",
    updated_at: "2026-07-22T10:05:00.000Z",
    completed_at: null,
    metadata: {},
    worktree_disposition: null,
    message_count: 8,
    total_tokens_estimate: 4200,
  };
}

function messagesPayload() {
  return [
    {
      role: "user",
      content: "Please help refine the app layout.",
      created_at: "2026-07-22T10:00:10.000Z",
      tool_calls: [],
    },
    {
      role: "assistant",
      content: "I will review the surfaces and clean up the hierarchy.",
      created_at: "2026-07-22T10:00:20.000Z",
      tool_calls: [
        { id: "call-1", name: "Read", params: { path: "web/src/App.tsx" } },
      ],
    },
    {
      role: "tool",
      content: "File: web/src/App.tsx\n1 | import ...",
      tool_call_id: "call-1",
      created_at: "2026-07-22T10:00:21.000Z",
    },
  ];
}

function timelinePayload(sessionId: string, messages: Array<Record<string, unknown>>, events: Array<Record<string, unknown>>) {
  const eventItems = events.map((event, index) => {
    const seq = Number(event.seq || index + 1);
    return { source: "ws", timestamp: event.timestamp || "", seq, event: { ...event, seq } };
  });
  const items = [
    ...messages.map((message) => ({ source: "message", timestamp: message.created_at || "", message })),
    ...eventItems,
  ].sort((a, b) => String(a.timestamp || "").localeCompare(String(b.timestamp || "")));
  return {
    session_id: sessionId,
    items,
    last_seq: eventItems.reduce((max, item) => Math.max(max, item.seq), 0),
    has_more: false,
    turns: [{
      turn_id: "fixture-turn",
      run_id: "fixture-run",
      turn_index: 0,
      user_message: messages.find((item) => item.role === "user") || null,
      assistant_message: messages.find((item) => item.role === "assistant") || null,
      trace_events: events,
      meta: {
        steps: events.length, tokens: 1200, status: "completed",
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
}

function tracePayload() {
  return [
    { type: "thought", content: "I should reduce visual noise.", timestamp: "2026-07-22T10:00:20.000Z" },
    { type: "tool_call", name: "Read", params: { path: "web/src/styles.css" }, step: 1, timestamp: "2026-07-22T10:00:21.000Z" },
    { type: "observation", tool_name: "Read", output: "body { ... }", status: "success", timestamp: "2026-07-22T10:00:22.000Z" },
    { type: "approval_required", request_id: "req-1", tool_name: "Write", params: { path: "src/App.tsx" }, decision_reason: "needs approval", tool_use_id: "call-1", permission_mode: "default", risk_level: "medium", timestamp: "2026-07-22T10:00:23.000Z" },
    { type: "subagent_start", child_session_id: "child-1", agent_name: "explore", timestamp: "2026-07-22T10:00:24.000Z" },
    { type: "subagent_stop", child_session_id: "child-1", status: "completed", timestamp: "2026-07-22T10:00:25.000Z" },
    { type: "plan_ready", plan_text: "# Plan\n\n- Make layout calmer", contract: { goal: "visual unification" }, timestamp: "2026-07-22T10:00:26.000Z" },
    { type: "status", status: "finish", message: "Done.", timestamp: "2026-07-22T10:00:27.000Z" },
  ];
}

function treePayload() {
  return {
    id: SESSION_ID,
    agent_name: "build",
    title: "Phase 17-18 Workspace",
    status: "running",
    depth: 0,
    parent_id: null,
    created_at: "2026-07-22T10:00:00.000Z",
    children: [
      {
        id: "child-1",
        agent_name: "explore",
        title: "Child explore session",
        status: "completed",
        depth: 1,
        parent_id: SESSION_ID,
        created_at: "2026-07-22T10:00:24.000Z",
        children: [],
        child_count: 0,
      },
    ],
    child_count: 1,
  };
}

function statsPayload() {
  return {
    steps_taken: 8,
    max_steps: 20,
    total_tokens: 4200,
    duration_seconds: 300,
    tools: { Read: 2, Write: 1, Bash: 1 },
  };
}

function planPayload() {
  return {
    session_id: SESSION_ID,
    content: "# Frontend plan\n\n- Unify shell\n- Reduce clutter\n- Keep core surfaces",
    has_plan: true,
  };
}

function diffsPayload() {
  return [
    {
      id: 1,
      session_id: SESSION_ID,
      step_number: 4,
      file_path: "web/src/styles.css",
      diff_content: "--- a/web/src/styles.css\n+++ b/web/src/styles.css\n@@ -1,3 +1,3 @@\n-body {\n+body.workbench {",
      status: "pending",
      review_comment: "",
      created_at: "2026-07-22T10:03:00.000Z",
      session_title: "Phase 17-18 Workspace",
      session_agent: "build",
    },
  ];
}

test.beforeEach(async ({ page }) => {
  await page.route("**/api/sessions?limit=50", async (route) => route.fulfill({ json: sessionsPayload() }));
  await page.route(`**/api/sessions/${SESSION_ID}`, async (route) => route.fulfill({ json: sessionDetail() }));
  await page.route(`**/api/sessions/${SESSION_ID}/messages`, async (route) => route.fulfill({ json: messagesPayload() }));
  await page.route(`**/api/sessions/${SESSION_ID}/timeline?limit=200`, async (route) => {
    const timeline = timelinePayload(SESSION_ID, messagesPayload(), tracePayload());
    // Set plan_state to "waiting" so the plan approval UI renders in Chat.
    (timeline as Record<string, unknown>).plan_state = {
      lifecycle: "waiting",
      plan_text: "# Plan\n\n- Make layout calmer",
      revision: 0,
      max_revisions: 5,
      contract: { goal: "visual unification" },
    };
    await route.fulfill({ json: timeline });
  });
  await page.route(`**/api/sessions/${SESSION_ID}/trace/events?after=0&limit=200`, async (route) => route.fulfill({ json: tracePayload() }));
  await page.route(`**/api/sessions/${SESSION_ID}/tree`, async (route) => route.fulfill({ json: treePayload() }));
  await page.route(`**/api/sessions/${SESSION_ID}/plan`, async (route) => route.fulfill({ json: planPayload() }));
  await page.route(`**/api/sessions/${SESSION_ID}/stats`, async (route) => route.fulfill({ json: statsPayload() }));
  await page.route("**/api/config/models", async (route) => route.fulfill({ json: [{ key: "deepseek-v4-flash", family: "Fast", note: "Quick iteration" }] }));
  await page.route("**/api/skills", async (route) => route.fulfill({ json: [] }));
  await page.route("**/api/storage/stats", async (route) => route.fulfill({ json: { backend: "sqlite", total_sessions: 1, total_messages: 3, total_memories: 2, db_size_bytes: 1024 } }));
  await page.route("**/api/plans?limit=50&offset=0", async (route) => route.fulfill({ json: { plans: [], total: 0 } }));
  await page.route("**/api/diffs/pending", async (route) => route.fulfill({ json: diffsPayload() }));
  await page.route("**/api/sessions/*/approve", async (route) => route.fulfill({ json: { approved: true } }));
  await page.route("**/api/sessions/*/reject", async (route) => route.fulfill({ json: { approved: false } }));
});

test("keeps core surfaces reachable and secondary surfaces quieter", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Workbench", exact: true }).click();

  await expect(page.locator(".sidebar")).toBeVisible();
  await expect(page.locator(".view-tab[data-view='chat']")).toHaveText(/Chat/);
  await expect(page.locator(".view-tab[data-view='plan']")).toHaveCount(0);
  await expect(page.locator(".view-tab[data-view='reviews']")).toHaveText(/Changes/);
  await expect(page.locator(".view-tab[data-view='memory']")).toHaveText(/Memory/);

  await page.getByText("Phase 17-18 Workspace").click();

  await expect(page.locator(".session-tree-panel")).toBeVisible();
  await expect(page.getByText("Please help refine the app layout.")).toBeVisible();
  await expect(page.locator(".inline-tool")).toBeVisible();
  await expect(page.locator(".event-sidebar")).toBeVisible();

  await expect(page.getByRole("region", { name: "Plan approval" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Approve & Build/ })).toBeVisible();

  await page.locator("button[data-view='memory']").click();
  await expect(page.locator(".memory-hero")).toBeVisible();
  await expect(page.locator(".memory-page")).toBeVisible();

  await page.getByRole("button", { name: "Inspect", exact: true }).click();
  await page.locator("button[data-view='events']").click();
  await expect(page.locator("[data-view-name='events']")).toBeVisible();
  await expect(page.locator("[data-view-name='events'] .trace-summary").first()).toContainText("I should reduce visual noise.");

  await page.getByRole("button", { name: "Workbench", exact: true }).click();
  await page.locator("button[data-view='plans']").click();
  await expect(page.locator(".plan-library")).toBeVisible();

  await page.locator("button[data-view='reviews']").click();
  await expect(page.locator("[data-view-name='reviews'] .plan-hero-title")).toContainText("Quality, changes, and readiness");
});
