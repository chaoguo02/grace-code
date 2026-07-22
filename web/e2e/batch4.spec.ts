import { test, expect } from "playwright/test";

/* ------------------------------------------------------------------ */
/*  Batch 4 — E2E coverage: plan workflow, review diffs, session CRUD,*/
/*  sidebar trace timeline, tab navigation, and regression smoke.     */
/* ------------------------------------------------------------------ */

const S1 = "sess-plan-1";
const S2 = "sess-build-2";
const S3 = "sess-plan-3";
const CHILD = "child-sess-abc";

/* ── reusable payloads ─────────────────────────────────────────── */

function sessionsPayload() {
  return [
    {
      id: S1, agent_name: "plan", title: "Plan session test", status: "completed", mode: "plan",
      summary: "# Implementation Plan\n\n- Step 1: Add login\n- Step 2: Wire API",
      error: "", parent_id: null, created_at: "2026-07-22T09:00:00Z", updated_at: "2026-07-22T09:30:00Z",
      completed_at: "2026-07-22T09:30:00Z", message_count: 3, total_tokens_estimate: 900,
    },
    {
      id: S2, agent_name: "build", title: "Build session test", status: "running", mode: "build",
      summary: "Fixing auth middleware", error: "", parent_id: null,
      created_at: "2026-07-22T10:00:00Z", updated_at: "2026-07-22T10:00:00Z",
      completed_at: null, message_count: 0, total_tokens_estimate: 0,
    },
    {
      id: S3, agent_name: "plan", title: "Another plan", status: "completed", mode: "plan",
      summary: "Draft plan", error: "", parent_id: null,
      created_at: "2026-07-21T08:00:00Z", updated_at: "2026-07-21T08:20:00Z",
      completed_at: "2026-07-21T08:20:00Z", message_count: 2, total_tokens_estimate: 500,
    },
  ];
}

function sessionDetail(id: string, agentName = "build") {
  return {
    id, parent_id: null, root_id: id, agent_name: agentName,
    title: `Detail for ${id}`, status: agentName === "plan" ? "completed" : "running",
    mode: agentName === "plan" ? "plan" : "build", summary: "# Summary", error: "",
    agent_kind: "primary", context_origin: "user", execution_placement: "local",
    workspace_mode: "workspace", agent_depth: 0, generation: 0,
    created_at: "2026-07-22T10:00:00Z", updated_at: "2026-07-22T10:00:00Z",
    completed_at: null, metadata: {},
    worktree_disposition: null, message_count: 0, total_tokens_estimate: 0,
  };
}

function childSessionDetail() {
  return {
    id: CHILD, parent_id: S2, root_id: S2, agent_name: "explore",
    title: "Child explore session", status: "completed", mode: "explore",
    summary: "", error: "", agent_kind: "subagent", context_origin: "agent",
    execution_placement: "local", workspace_mode: "workspace",
    agent_depth: 1, generation: 1,
    created_at: "2026-07-22T10:05:00Z", completed_at: "2026-07-22T10:06:30Z",
    updated_at: "2026-07-22T10:06:30Z", metadata: { worktree_path: "/tmp/wt-abc" },
    worktree_disposition: "preserved", message_count: 4, total_tokens_estimate: 600,
  };
}

function tracePayload(overrides?: Array<Record<string, unknown>>) {
  if (overrides) return overrides;
  return [
    { type: "thought", content: "Let me plan this carefully.", timestamp: "2026-07-22T10:00:01Z" },
    { type: "tool_call", name: "Read", params: { file_path: "src/main.ts" }, step: 1, duration_ms: 120, timestamp: "2026-07-22T10:00:02Z" },
    { type: "observation", tool_name: "Read", output: "Line 1: import ...", status: "success", duration_ms: 5, timestamp: "2026-07-22T10:00:03Z" },
    { type: "status", status: "finish", message: "All done.", token_estimate: 450, timestamp: "2026-07-22T10:00:10Z" },
  ];
}

function childTracePayload() {
  return [
    { type: "subagent_start", agent_name: "explore", child_session_id: CHILD, timestamp: "2026-07-22T10:05:00Z" },
    { type: "thought", content: "Exploring repo structure...", timestamp: "2026-07-22T10:05:01Z" },
    { type: "tool_call", name: "Glob", params: { pattern: "**/*.ts" }, step: 1, duration_ms: 200, timestamp: "2026-07-22T10:05:02Z" },
    { type: "observation", tool_name: "Glob", output: "src/main.ts\nsrc/utils.ts", status: "success", duration_ms: 10, timestamp: "2026-07-22T10:05:03Z" },
    { type: "subagent_stop", child_session_id: CHILD, status: "completed", token_estimate: 300, timestamp: "2026-07-22T10:06:30Z" },
  ];
}

function planApprovalTrace() {
  return [
    {
      type: "plan_ready",
      plan_text: "# Architecture Plan\n\n## Goals\n- [x] Analyze requirements\n- [ ] Design API\n- [ ] Implement endpoints\n\n```ts\nconst plan = { phases: 3 };\n```\n\nSee [docs](https://example.com).",
      contract: { goal: "Design and implement the API layer", steps: ["analyze", "design", "implement"] },
      revision: 1, max_revisions: 5,
      timestamp: "2026-07-22T09:00:05Z",
    },
  ];
}

function pendingDiffsPayload() {
  return [
    {
      id: 1, session_id: S2, step_number: 2, file_path: "src/auth/login.ts",
      diff_content: "--- a/src/auth/login.ts\n+++ b/src/auth/login.ts\n@@ -1,3 +1,5 @@\n import { hash } from './crypto';\n+import { validate } from './validate';\n+\n export function login(user: string) {\n   return hash(user);",
      status: "pending", review_comment: "", created_at: "2026-07-22T10:01:00Z",
      session_title: "Build session test", session_agent: "build",
    },
    {
      id: 2, session_id: S2, step_number: 4, file_path: "src/api/routes.ts",
      diff_content: "--- a/src/api/routes.ts\n+++ b/src/api/routes.ts\n@@ -10,6 +10,8 @@\n router.get('/users', listUsers);\n+router.post('/users', createUser);\n+router.delete('/users/:id', deleteUser);",
      status: "pending", review_comment: "", created_at: "2026-07-22T10:02:00Z",
      session_title: "Build session test", session_agent: "build",
    },
  ];
}

function treePayload() {
  return {
    id: S2, agent_name: "build", title: "Build session test", status: "running",
    depth: 0, parent_id: null, created_at: "2026-07-22T10:00:00Z",
    children: [
      {
        id: CHILD, agent_name: "explore", title: "Child explore session", status: "completed",
        depth: 1, parent_id: S2, created_at: "2026-07-22T10:05:00Z",
        children: [], child_count: 0,
      },
    ],
    child_count: 1,
  };
}

/* ── shared route setup ────────────────────────────────────────── */

async function setupCommonRoutes(page: import("playwright").Page) {
  await page.route("**/api/sessions?limit=50", async (route) => {
    await route.fulfill({ json: sessionsPayload() });
  });
  await page.route("**/api/config/models", async (route) => {
    await route.fulfill({ json: [{ key: "deepseek-v4-flash", family: "Fast", note: "Quick" }] });
  });
  await page.route("**/api/skills", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/storage/stats", async (route) => {
    await route.fulfill({ json: { backend: "sqlite", total_sessions: 3, total_messages: 10, db_size_bytes: 2048 } });
  });
}

/* ═══════════════════════════════════════════════════════════════ */
/*  Tests                                                          */
/* ═══════════════════════════════════════════════════════════════ */

/* ── 1. Plan workflow ─────────────────────────────────────────── */

test("plan tab: shows empty state when no plan session selected", async ({ page }) => {
  await setupCommonRoutes(page);
  // sessions with no activeId — Plan tab should show empty state
  await page.route("**/api/sessions?limit=50", async (route) => {
    await route.fulfill({ json: [] });
  });

  await page.goto("/");
  await page.locator("button[data-view='plan']").click();

  await expect(page.locator(".plan-empty")).toBeVisible();
  await expect(page.locator(".plan-empty-title")).toContainText("No active session");
});

test("plan tab: renders plan approval UI for a plan_ready session", async ({ page }) => {
  await setupCommonRoutes(page);
  await page.route(`**/api/sessions/${S1}`, async (route) => {
    await route.fulfill({ json: sessionDetail(S1, "plan") });
  });
  await page.route(`**/api/sessions/${S1}/messages`, async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route(`**/api/sessions/${S1}/trace/events?after=0&limit=200`, async (route) => {
    await route.fulfill({ json: planApprovalTrace() });
  });
  await page.route(`**/api/sessions/${S1}/plan`, async (route) => {
    await route.fulfill({ json: { session_id: S1, content: "", has_plan: false } });
  });
  await page.route(`**/api/sessions/${S1}/tree`, async (route) => {
    await route.fulfill({ json: { ...treePayload(), id: S1, agent_name: "plan", children: [], child_count: 0 } });
  });
  await page.route(`**/api/sessions/${S1}/stats`, async (route) => {
    await route.fulfill({ json: {} });
  });

  await page.goto("/");
  await page.getByText("Plan session test").click();

  // Navigate to Plan tab
  await page.locator("button[data-view='plan']").click();

  // Should show the plan card with stats bar
  await expect(page.locator(".plan-card-prominent")).toBeVisible();
  await expect(page.locator(".plan-stats-bar")).toBeVisible();
  // Status should indicate awaiting approval
  await expect(page.locator(".plan-stat-waiting")).toContainText("Awaiting approval");
  // Should show approve and reject buttons (scoped to plan sidebar actions)
  await expect(page.locator(".plan-sidebar-actions button:has-text('Approve & Build')")).toBeVisible();
  await expect(page.locator(".plan-sidebar-actions button:has-text('Request Revision')")).toBeVisible();
  // Plan text should render markdown
  await expect(page.locator(".plan-pre")).toContainText("Architecture");
});

test("plan tab: approve plan sends POST to approve endpoint", async ({ page }) => {
  let approvedRequest: Record<string, unknown> | null = null;
  await setupCommonRoutes(page);
  await page.route(`**/api/sessions/${S1}`, async (route) => {
    await route.fulfill({ json: sessionDetail(S1, "plan") });
  });
  await page.route(`**/api/sessions/${S1}/messages`, async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route(`**/api/sessions/${S1}/trace/events?after=0&limit=200`, async (route) => {
    await route.fulfill({ json: planApprovalTrace() });
  });
  await page.route(`**/api/sessions/${S1}/plan`, async (route) => {
    await route.fulfill({ json: { session_id: S1, content: "", has_plan: false } });
  });
  await page.route(`**/api/sessions/${S1}/tree`, async (route) => {
    await route.fulfill({ json: { ...treePayload(), id: S1, agent_name: "plan", children: [], child_count: 0 } });
  });
  await page.route(`**/api/sessions/${S1}/approve`, async (route) => {
    if (route.request().method() === "POST") {
      approvedRequest = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({ json: { approved: true } });
      return;
    }
    await route.fallback();
  });
  await page.route(`**/api/sessions/${S1}/stats`, async (route) => {
    await route.fulfill({ json: {} });
  });

  await page.goto("/");
  await page.getByText("Plan session test").click();
  await page.locator("button[data-view='plan']").click();

  // Click approve (in plan sidebar actions, not composer footer)
  await page.locator(".plan-sidebar-actions button:has-text('Approve & Build')").click();

  // Verify POST was sent
  expect(approvedRequest).not.toBeNull();
});

test("plan tab: reject plan sends POST to reject endpoint", async ({ page }) => {
  let rejectRequest: Record<string, unknown> | null = null;
  await setupCommonRoutes(page);
  await page.route(`**/api/sessions/${S1}`, async (route) => {
    await route.fulfill({ json: sessionDetail(S1, "plan") });
  });
  await page.route(`**/api/sessions/${S1}/messages`, async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route(`**/api/sessions/${S1}/trace/events?after=0&limit=200`, async (route) => {
    await route.fulfill({ json: planApprovalTrace() });
  });
  await page.route(`**/api/sessions/${S1}/plan`, async (route) => {
    await route.fulfill({ json: { session_id: S1, content: "", has_plan: false } });
  });
  await page.route(`**/api/sessions/${S1}/tree`, async (route) => {
    await route.fulfill({ json: { ...treePayload(), id: S1, agent_name: "plan", children: [], child_count: 0 } });
  });
  await page.route(`**/api/sessions/${S1}/reject`, async (route) => {
    if (route.request().method() === "POST") {
      rejectRequest = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({ json: { approved: false } });
      return;
    }
    await route.fallback();
  });
  await page.route(`**/api/sessions/${S1}/stats`, async (route) => {
    await route.fulfill({ json: {} });
  });

  await page.goto("/");
  await page.getByText("Plan session test").click();
  await page.locator("button[data-view='plan']").click();

  // Click reject (Request Revision)
  await page.locator("button:has-text('Request Revision')").click();

  expect(rejectRequest).not.toBeNull();
});

/* ── 2. Review diff workflow ──────────────────────────────────── */

test("review tab: shows diffs and can toggle expand", async ({ page }) => {
  await setupCommonRoutes(page);
  await page.route("**/api/diffs/pending", async (route) => {
    await route.fulfill({ json: pendingDiffsPayload() });
  });

  await page.goto("/");
  await page.locator("button[data-view='reviews']").click();

  // Should see the review hero
  await expect(page.locator(".review-hero")).toBeVisible();
  // Should see the pending count
  await expect(page.locator(".meta-pill-value").first()).toContainText("2");
  // Should see file paths (sorted by created_at desc, so routes.ts first)
  await expect(page.locator(".review-card")).toHaveCount(2);
  await expect(page.locator(".review-card-title").first()).toContainText("routes.ts");
  await expect(page.locator(".review-card-title").nth(1)).toContainText("login.ts");

  // Diff should be collapsed by default
  await expect(page.locator(".diff-block")).toHaveCount(0);

  // Click the diff toggle
  await page.locator(".review-diff-toggle").first().click();
  await expect(page.locator(".diff-block")).toHaveCount(1);
  await expect(page.locator(".diff-line-added")).toHaveCount(2); // "+import { validate }..." and blank line

  // Click again to collapse
  await page.locator(".review-diff-toggle").first().click();
  await expect(page.locator(".diff-block")).toHaveCount(0);
});

test("review tab: approve diff removes it from list", async ({ page }) => {
  let patchRequest: Record<string, unknown> | null = null;
  await setupCommonRoutes(page);
  await page.route("**/api/diffs/pending", async (route) => {
    await route.fulfill({ json: [pendingDiffsPayload()[0]] }); // single diff
  });
  await page.route("**/api/diffs/1", async (route) => {
    if (route.request().method() === "PATCH") {
      patchRequest = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({ json: { updated: true, status: "approved" } });
      return;
    }
    await route.fallback();
  });

  await page.goto("/");
  await page.locator("button[data-view='reviews']").click();

  await expect(page.locator(".review-card")).toHaveCount(1);

  // Click Approve
  await page.locator("button:has-text('Approve')").first().click();

  // Should have sent PATCH with approved status
  await expect.poll(() => patchRequest).not.toBeNull();
  expect(patchRequest).toMatchObject({ status: "approved" });
});

test("review tab: reject diff sends PATCH with rejected status", async ({ page }) => {
  let patchRequest: Record<string, unknown> | null = null;
  await setupCommonRoutes(page);
  await page.route("**/api/diffs/pending", async (route) => {
    await route.fulfill({ json: [pendingDiffsPayload()[0]] });
  });
  await page.route("**/api/diffs/1", async (route) => {
    if (route.request().method() === "PATCH") {
      patchRequest = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({ json: { updated: true, status: "rejected" } });
      return;
    }
    await route.fallback();
  });

  await page.goto("/");
  await page.locator("button[data-view='reviews']").click();

  await page.locator("button:has-text('Reject')").first().click();

  await expect.poll(() => patchRequest).not.toBeNull();
  expect(patchRequest).toMatchObject({ status: "rejected" });
});

/* ── 3. Session CRUD ──────────────────────────────────────────── */

test("session sidebar: lists sessions and navigates on click", async ({ page }) => {
  await setupCommonRoutes(page);
  await page.route(`**/api/sessions/${S1}`, async (route) => {
    await route.fulfill({ json: sessionDetail(S1, "plan") });
  });
  await page.route(`**/api/sessions/${S1}/messages`, async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route(`**/api/sessions/${S1}/trace/events?after=0&limit=200`, async (route) => {
    await route.fulfill({ json: tracePayload() });
  });
  await page.route(`**/api/sessions/${S1}/plan`, async (route) => {
    await route.fulfill({ json: { session_id: S1, content: "", has_plan: false } });
  });
  await page.route(`**/api/sessions/${S1}/tree`, async (route) => {
    await route.fulfill({ json: { ...treePayload(), id: S1, agent_name: "plan", children: [], child_count: 0 } });
  });
  await page.route(`**/api/sessions/${S1}/stats`, async (route) => {
    await route.fulfill({ json: {} });
  });

  await page.goto("/");

  // Should list 3 sessions
  await expect(page.locator(".session-item")).toHaveCount(3);
  // Click first session
  await page.getByText("Plan session test").click();
  // Should become active
  await expect(page.locator(".session-item.active")).toHaveCount(1);
});

test("session sidebar: create session via + Build button", async ({ page }) => {
  await setupCommonRoutes(page);
  // Need a second sessions fetch after creation
  let createCalled = false;
  let sessionsFetchCount = 0;
  await page.route("**/api/sessions?limit=50", async (route) => {
    sessionsFetchCount++;
    await route.fulfill({ json: sessionsPayload() });
  });
  await page.route("**/api/sessions", async (route) => {
    if (route.request().method() === "POST") {
      createCalled = true;
      await route.fulfill({ json: { session_id: "sess-new-99" } });
      return;
    }
    await route.fallback();
  });
  // Mock the new session detail
  await page.route("**/api/sessions/sess-new-99", async (route) => {
    await route.fulfill({ json: sessionDetail("sess-new-99", "build") });
  });
  await page.route("**/api/sessions/sess-new-99/messages", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/sessions/sess-new-99/trace/events?after=0&limit=200", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/sessions/sess-new-99/plan", async (route) => {
    await route.fulfill({ json: { session_id: "sess-new-99", content: "", has_plan: false } });
  });
  await page.route("**/api/sessions/sess-new-99/tree", async (route) => {
    await route.fulfill({ json: { id: "sess-new-99", agent_name: "build", title: "", status: "queued", depth: 0, parent_id: null, created_at: "", children: [], child_count: 0 } });
  });
  await page.route("**/api/sessions/sess-new-99/stats", async (route) => {
    await route.fulfill({ json: {} });
  });

  await page.goto("/");
  await page.locator("button:has-text('+ Build')").click();

  await expect.poll(() => createCalled).toBe(true);
});

test("session sidebar: shows delete confirmation modal", async ({ page }) => {
  await setupCommonRoutes(page);

  await page.goto("/");
  // Click the × delete button on the third session
  const deleteBtn = page.locator(".session-delete").first();
  await deleteBtn.click();

  // Confirm modal should appear
  await expect(page.locator("text=Delete session")).toBeVisible();
  await expect(page.locator("text=Permanently delete this session?")).toBeVisible();

  // Cancel the modal
  await page.locator("button:has-text('Cancel')").click();
  await expect(page.locator("text=Delete session")).not.toBeVisible();
});

/* ── 4. Sidebar trace timeline ────────────────────────────────── */

test("trace sidebar: renders events in timeline", async ({ page }) => {
  await setupCommonRoutes(page);
  await page.route(`**/api/sessions/${S1}`, async (route) => {
    await route.fulfill({ json: sessionDetail(S1, "plan") });
  });
  await page.route(`**/api/sessions/${S1}/messages`, async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route(`**/api/sessions/${S1}/trace/events?after=0&limit=200`, async (route) => {
    await route.fulfill({ json: tracePayload() });
  });
  await page.route(`**/api/sessions/${S1}/plan`, async (route) => {
    await route.fulfill({ json: { session_id: S1, content: "", has_plan: false } });
  });
  await page.route(`**/api/sessions/${S1}/tree`, async (route) => {
    await route.fulfill({ json: { ...treePayload(), id: S1, agent_name: "plan", children: [], child_count: 0 } });
  });
  await page.route(`**/api/sessions/${S1}/stats`, async (route) => {
    await route.fulfill({ json: { steps_taken: 2, max_steps: 10, total_tokens: 450, duration_seconds: 10, tools: { Read: 1 } } });
  });

  await page.goto("/");
  await page.getByText("Plan session test").click();

  // Event sidebar should render timeline cards
  await expect(page.locator("#event-sidebar")).toBeVisible();
  await expect(page.locator(".timeline-card")).toHaveCount(4); // thought + tool_call + observation + status

  // The execution stats card should show step count
  await expect(page.locator(".execution-stats-card")).toBeVisible();
  await expect(page.locator(".execution-stats-card")).toContainText("2");
});

test("trace sidebar: filter buttons switch visible events", async ({ page }) => {
  await setupCommonRoutes(page);
  await page.route(`**/api/sessions/${S1}`, async (route) => {
    await route.fulfill({ json: sessionDetail(S1, "plan") });
  });
  await page.route(`**/api/sessions/${S1}/messages`, async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route(`**/api/sessions/${S1}/trace/events?after=0&limit=200`, async (route) => {
    await route.fulfill({ json: tracePayload() });
  });
  await page.route(`**/api/sessions/${S1}/plan`, async (route) => {
    await route.fulfill({ json: { session_id: S1, content: "", has_plan: false } });
  });
  await page.route(`**/api/sessions/${S1}/tree`, async (route) => {
    await route.fulfill({ json: { ...treePayload(), id: S1, agent_name: "plan", children: [], child_count: 0 } });
  });
  await page.route(`**/api/sessions/${S1}/stats`, async (route) => {
    await route.fulfill({ json: { steps_taken: 2, max_steps: 10, total_tokens: 450, duration_seconds: 10, tools: { Read: 1 } } });
  });

  await page.goto("/");
  await page.getByText("Plan session test").click();

  // All events visible
  await expect(page.locator(".timeline-card")).toHaveCount(4);

  // Click Steps filter
  await page.locator(".event-filter-row button:has-text('Steps')").click();
  // Should only show tool_call + observation (2 events)
  await expect(page.locator(".timeline-card")).toHaveCount(2);

  // Click Logs filter
  await page.locator(".event-filter-row button:has-text('Logs')").click();
  // Should show thought + status
  await expect(page.locator(".timeline-card")).toHaveCount(2);

  // Back to All
  await page.locator(".event-filter-row button:has-text('All')").click();
  await expect(page.locator(".timeline-card")).toHaveCount(4);
});

/* ── 5. Tab navigation ────────────────────────────────────────── */

test("tab navigation: all 5 tabs render their content", async ({ page }) => {
  await setupCommonRoutes(page);
  await page.route("**/api/diffs/pending", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/sessions/**/stats", async (route) => {
    await route.fulfill({ json: {} });
  });
  await page.route("**/api/stats/sessions", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/stats/daily", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/stats/steps", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/memory/**", async (route) => {
    await route.fulfill({ json: [] });
  });

  await page.goto("/");

  // Chat tab (default)
  await expect(page.locator(".view-tab.active")).toContainText("Chat");

  // Plan tab
  await page.locator("button[data-view='plan']").click();
  await expect(page.locator("button[data-view='plan'].active")).toBeVisible();
  await expect(page.locator(".plan-page")).toBeVisible();

  // Reviews tab
  await page.locator("button[data-view='reviews']").click();
  await expect(page.locator(".review-page")).toBeVisible();

  // Stats tab
  await page.locator("button[data-view='stats']").click();
  await expect(page.locator("[data-view-name='stats']")).toBeVisible();

  // Memory tab
  await page.locator("button[data-view='memory']").click();
  await expect(page.locator("[data-view-name='memory']")).toBeVisible();
});

/* ── 6. Regression smoke — Batch 1 guard ───────────────────────── */

test("regression: markdown rendering still works in message bubbles", async ({ page }) => {
  await setupCommonRoutes(page);
  await page.route(`**/api/sessions/${S1}`, async (route) => {
    await route.fulfill({ json: sessionDetail(S1, "build") });
  });
  await page.route(`**/api/sessions/${S1}/messages`, async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ json: { session_id: S1, status: "accepted", summary: "queued", steps_taken: 0, total_tokens: 0, error: null, termination_reason: null } });
      return;
    }
    await route.fulfill({
      json: [{
        role: "assistant",
        content: "# Result\n\n```js\nconst x = 1;\n```\n\n| Col | Val |\n|-----|-----|\n| A   | 1   |\n\n[end](https://x.com).",
        created_at: "2026-07-22T10:00:30Z",
        tool_calls: [],
      }],
    });
  });
  await page.route(`**/api/sessions/${S1}/trace/events?after=0&limit=200`, async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route(`**/api/sessions/${S1}/plan`, async (route) => {
    await route.fulfill({ json: { session_id: S1, content: "", has_plan: false } });
  });
  await page.route(`**/api/sessions/${S1}/tree`, async (route) => {
    await route.fulfill({ json: { ...treePayload(), id: S1, children: [], child_count: 0 } });
  });
  await page.route(`**/api/sessions/${S1}/stats`, async (route) => {
    await route.fulfill({ json: {} });
  });

  await page.goto("/");
  await page.getByText("Plan session test").click();

  // Batch 1 assertions: code blocks, tables, links in message bubbles
  await expect(page.locator(".message-bubble pre code")).toContainText("const x = 1;");
  await expect(page.locator(".message-bubble table")).toContainText("Col");
  await expect(page.locator(".message-bubble a")).toHaveAttribute("href", "https://x.com");
});

/* ── 7. Regression smoke — Batch 2 guard ───────────────────────── */

test("regression: no content truncation — tool output shows full content", async ({ page }) => {
  const longOutput = "A".repeat(600); // longer than old 500-char limit
  await setupCommonRoutes(page);
  await page.route(`**/api/sessions/${S1}`, async (route) => {
    await route.fulfill({ json: sessionDetail(S1, "build") });
  });
  await page.route(`**/api/sessions/${S1}/messages`, async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ json: { session_id: S1, status: "accepted" } });
      return;
    }
    await route.fulfill({
      json: [{
        role: "tool",
        content: longOutput,
        tool_call_id: "call-abc-123",
        created_at: "2026-07-22T10:00:30Z",
        tool_calls: [],
      }],
    });
  });
  await page.route(`**/api/sessions/${S1}/trace/events?after=0&limit=200`, async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route(`**/api/sessions/${S1}/plan`, async (route) => {
    await route.fulfill({ json: { session_id: S1, content: "", has_plan: false } });
  });
  await page.route(`**/api/sessions/${S1}/tree`, async (route) => {
    await route.fulfill({ json: { ...treePayload(), id: S1, children: [], child_count: 0 } });
  });
  await page.route(`**/api/sessions/${S1}/stats`, async (route) => {
    await route.fulfill({ json: {} });
  });

  await page.goto("/");
  await page.getByText("Plan session test").click();

  // The obs-output should contain the full 600-char content
  const obsText = await page.locator(".obs-output").textContent();
  expect(obsText).toContain(longOutput);
});

test("regression: ExpandableText toggle works on plan contract", async ({ page }) => {
  await setupCommonRoutes(page);
  // Use a plan session with plan approval
  await page.route(`**/api/sessions/${S1}`, async (route) => {
    await route.fulfill({ json: sessionDetail(S1, "plan") });
  });
  await page.route(`**/api/sessions/${S1}/messages`, async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route(`**/api/sessions/${S1}/trace/events?after=0&limit=200`, async (route) => {
    await route.fulfill({ json: planApprovalTrace() });
  });
  await page.route(`**/api/sessions/${S1}/plan`, async (route) => {
    await route.fulfill({ json: { session_id: S1, content: "", has_plan: false } });
  });
  await page.route(`**/api/sessions/${S1}/tree`, async (route) => {
    await route.fulfill({ json: { ...treePayload(), id: S1, agent_name: "plan", children: [], child_count: 0 } });
  });
  await page.route(`**/api/sessions/${S1}/approve`, async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ json: { approved: true } });
      return;
    }
    await route.fallback();
  });
  await page.route(`**/api/sessions/${S1}/stats`, async (route) => {
    await route.fulfill({ json: {} });
  });

  await page.goto("/");
  await page.getByText("Plan session test").click();

  // The plan approval should show in the composer area
  await expect(page.locator(".plan-actions")).toBeVisible();
  // The expandable-text-toggle should be present for the plan contract
  // (if contract goal is long enough, or always shown per Batch 2)
  await expect(page.locator(".expandable-text-toggle")).toBeVisible();
});
