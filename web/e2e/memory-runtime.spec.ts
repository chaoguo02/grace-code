import { test, expect } from "playwright/test";

const SESSION_ID = "memory-runtime-sess";

function sessionSummary() {
  return [{
    id: SESSION_ID,
    agent_name: "build",
    title: "Memory Runtime Session",
    status: "running",
    mode: "build",
    summary: "Working with active memory recall.",
    error: "",
    parent_id: null,
    created_at: "2026-07-24T10:00:00.000Z",
    updated_at: "2026-07-24T10:05:00.000Z",
    completed_at: null,
    message_count: 2,
    total_tokens_estimate: 1200,
  }];
}

function sessionDetail() {
  return {
    id: SESSION_ID,
    parent_id: null,
    root_id: SESSION_ID,
    agent_name: "build",
    title: "Memory Runtime Session",
    status: "running",
    mode: "build",
    summary: "Working with active memory recall.",
    error: "",
    agent_kind: "primary",
    context_origin: "user",
    execution_placement: "local",
    workspace_mode: "workspace",
    agent_depth: 0,
    generation: 0,
    created_at: "2026-07-24T10:00:00.000Z",
    updated_at: "2026-07-24T10:05:00.000Z",
    completed_at: null,
    metadata: {},
    worktree_disposition: null,
    message_count: 2,
    total_tokens_estimate: 1200,
  };
}

function memorySnapshot() {
  return {
    overview: {
      enabled: true,
      preview: false,
      total: 2,
      active: 2,
      deprecated: 0,
      archived: 0,
      expiring: 0,
      by_type: { user: 0, feedback: 0, project: 2, reference: 0 },
      by_scope: { session: 0, project: 2, global: 0 },
      by_layer: { project: 2, global: 0, archive: 0 },
    },
    items: [
      {
        name: "react-loop",
        description: "React loop state must be session-aware",
        type: "project",
        status: "active",
        scope: "project",
        confidence: 0.85,
        access_count: 3,
        updated_at: "2026-07-24T10:00:00.000Z",
        created_at: "2026-07-24T10:00:00.000Z",
        content: "**Decision:** React websocket state is session-aware.\n\n**Confidence reason:** Verified across 3 production sessions with no cross-contamination observed.",
      },
      {
        name: "memory-discipline",
        description: "Memory extraction requires confidence reasons",
        type: "project",
        status: "active",
        scope: "project",
        confidence: 0.65,
        access_count: 1,
        updated_at: "2026-07-24T10:01:00.000Z",
        created_at: "2026-07-24T10:01:00.000Z",
        content: "**Decision:** Automatic memories require confidence reasons.",
      },
    ],
  };
}

function recallPayload() {
  return {
    session_id: SESSION_ID,
    items: [{
      session_id: SESSION_ID,
      memory_name: "react-loop",
      source: "scoped",
      score: 0.82,
      reason: "Matched terms: react, websocket, session",
      confidence: 0.85,
      scope: "project",
      injected: true,
      omitted_reason: "",
      created_at: "2026-07-24T10:04:00.000Z",
      description: "React loop state must be session-aware",
      type: "project",
      override: "",
    }, {
      session_id: SESSION_ID,
      memory_name: "old-decision",
      source: "scoped",
      score: 0.45,
      reason: "Low relevance to current task",
      confidence: 0.35,
      scope: "project",
      injected: false,
      omitted_reason: "item_budget",
      created_at: "2026-07-24T10:04:01.000Z",
      description: "Old architecture decision",
      type: "project",
      override: "",
    }],
  };
}

function generatedPayload() {
  return {
    session_id: SESSION_ID,
    items: [{
      name: "memory-discipline",
      description: "Memory extraction requires confidence reasons",
      type: "project",
      status: "active",
      scope: "project",
      confidence: 0.65,
      access_count: 1,
      updated_at: "2026-07-24T10:01:00.000Z",
      created_at: "2026-07-24T10:01:00.000Z",
      source_session_id: SESSION_ID,
    }],
  };
}

function tracePayload() {
  return [
    {
      type: "memory_recall",
      injected_count: 1,
      candidate_count: 3,
      omitted_count: 2,
      top_names: ["react-loop"],
      timestamp: "2026-07-24T10:03:00.000Z",
    },
    {
      type: "memory_written",
      name: "memory-discipline",
      description: "Memory extraction requires confidence reasons",
      source: "run_finalizer",
      confidence: 0.65,
      timestamp: "2026-07-24T10:04:00.000Z",
    },
  ];
}

test.beforeEach(async ({ page }) => {
  await page.route("**/api/sessions?limit=50", async (route) => route.fulfill({ json: sessionSummary() }));
  await page.route(`**/api/sessions/${SESSION_ID}`, async (route) => route.fulfill({ json: sessionDetail() }));
  await page.route(`**/api/sessions/${SESSION_ID}/messages`, async (route) => route.fulfill({ json: [] }));
  await page.route(`**/api/sessions/${SESSION_ID}/trace/events?after=0&limit=200`, async (route) => route.fulfill({ json: tracePayload() }));
  await page.route(`**/api/sessions/${SESSION_ID}/tree`, async (route) => route.fulfill({ json: { ...sessionSummary()[0], depth: 0, children: [], child_count: 0 } }));
  await page.route(`**/api/sessions/${SESSION_ID}/stats`, async (route) => route.fulfill({ json: { steps_taken: 1, max_steps: 10, total_tokens: 1200, duration_seconds: 30, tools: {} } }));
  await page.route(`**/api/sessions/${SESSION_ID}/plan`, async (route) => route.fulfill({ json: { session_id: SESSION_ID, content: "", has_plan: false } }));
  await page.route("**/api/config/models", async (route) => route.fulfill({ json: [] }));
  await page.route("**/api/skills", async (route) => route.fulfill({ json: [] }));
  await page.route("**/api/storage/stats", async (route) => route.fulfill({ json: { backend: "sqlite", total_sessions: 1, total_messages: 0, total_memories: 2, db_size_bytes: 1024 } }));
  await page.route("**/api/diffs/pending", async (route) => route.fulfill({ json: [] }));
  await page.route("**/api/memory?_expand=true", async (route) => route.fulfill({ json: memorySnapshot() }));
  // Mock memory detail: /api/memory/<name>
  await page.route(/\/api\/memory\/(?!sessions)[^/]+(?!\?)/, async (route) => {
    const url = route.request().url();
    const name = decodeURIComponent(url.replace(/.*\/api\/memory\//, "").split("?")[0]);
    const items = memorySnapshot().items.filter((it) => it.name === name);
    await route.fulfill({ json: items[0] || {} });
  });
  await page.route(`**/api/memory/sessions/${SESSION_ID}/generated`, async (route) => route.fulfill({ json: generatedPayload() }));
  await page.route(`**/api/memory/sessions/${SESSION_ID}/preview-recall`, async (route) => route.fulfill({ json: { ...recallPayload(), items: [{ ...recallPayload().items[0], memory_name: "memory-discipline", source: "scoped", score: 0.76 }] } }));

  // Dynamic override + recall state
  const overrideState: Record<string, string> = {};
  const recallItems = recallPayload().items;

  await page.route(`**/api/memory/sessions/${SESSION_ID}/overrides`, async (route) => {
    if (route.request().method() === "POST") {
      const body = JSON.parse(route.request().postData() || "{}");
      const { memory_name, action } = body;
      if (action === "pin" || action === "disable") {
        overrideState[memory_name] = action;
      } else {
        delete overrideState[memory_name];
      }
      await route.fulfill({ json: { session_id: SESSION_ID, memory_name, action: overrideState[memory_name] || "" } });
    } else {
      await route.fulfill({ json: { session_id: SESSION_ID, memory_name: "react-loop", action: "pin" } });
    }
  });

  await page.route(`**/api/memory/sessions/${SESSION_ID}/recalls`, async (route) => {
    const updatedItems = recallItems.map((item: Record<string, unknown>) => {
      const override = overrideState[item.memory_name as string] || "";
      const isDisabled = override === "disable";
      return {
        ...item,
        override,
        injected: isDisabled ? false : (item.injected as boolean),
        omitted_reason: isDisabled ? "disabled_for_session" : (item.omitted_reason as string),
      };
    });
    await route.fulfill({ json: { session_id: SESSION_ID, items: updatedItems } });
  });
});

test("shows memory trace events and current session recall controls", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Workbench", exact: true }).click();
  await page.getByText("Memory Runtime Session").click();

  await expect(page.locator("#event-sidebar")).toContainText("memory_recall");
  await expect(page.locator("#event-sidebar")).toContainText("memory_written");

  await page.locator("button[data-view='memory']").click();
  await expect(page.getByText("Current session recall")).toBeVisible();
  await expect(page.getByText("1 injected · 2 recorded")).toBeVisible();
  await expect(page.getByText("Matched terms: react, websocket, session")).toBeVisible();
  await expect(page.getByText("Generated from this session")).toBeVisible();
  await expect(page.locator(".memory-side").getByText("memory-discipline").first()).toBeVisible();

  // Omitted candidates: collapsed by default, can expand
  await expect(page.getByText("Omitted candidates (1)")).toBeVisible();
  await page.getByText("Omitted candidates (1)").click();
  await expect(page.getByText("item_budget").first()).toBeVisible();
  await expect(page.getByText("old-decision").first()).toBeVisible();

  // Confidence reason: select react-loop and verify the extracted reason block
  await page.locator(".memory-catalog-list").getByText("react-loop").click();
  await expect(page.getByText("Confidence reason", { exact: true })).toBeVisible();
  await expect(page.getByText("Verified across 3 production sessions").first()).toBeVisible();

  const overrideRequest = page.waitForRequest(`**/api/memory/sessions/${SESSION_ID}/overrides`);
  await page.getByRole("button", { name: "Pin" }).first().click();
  await overrideRequest;

  await page.getByPlaceholder("Preview recall query...").fill("memory confidence reason");
  await page.getByRole("button", { name: "Preview recall" }).click();
  await expect(page.locator(".memory-list-meta").filter({ hasText: "memory-discipline" })).toBeVisible();
});

test.describe("memory override lifecycle", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Workbench", exact: true }).click();
    await page.getByText("Memory Runtime Session").click();
    await page.locator("button[data-view='memory']").click();
    await expect(page.getByText("Current session recall")).toBeVisible();
  });

  test("pin then unpin toggles button label and calls API", async ({ page }) => {
    // Scope to the first recall item within the memory-side panel (not the catalog)
    const firstItem = page.locator(".memory-side .memory-list-item").filter({ hasText: "react-loop" }).first();
    const pinBtn = firstItem.getByRole("button", { name: "Pin" });
    await expect(pinBtn).toBeVisible();

    // Click Pin — should fire POST with action=pin
    const pinReq = page.waitForRequest((req) =>
      req.url().includes("overrides") && req.method() === "POST");
    await pinBtn.click();
    const pinPost = await pinReq;
    expect(JSON.parse(pinPost.postData() || "{}")).toMatchObject({
      memory_name: "react-loop", action: "pin",
    });

    // After recall reload, button should now read "Unpin"
    await expect(firstItem.getByRole("button", { name: "Unpin" })).toBeVisible();

    // Click Unpin — should fire POST with action=unpin
    const unpinReq = page.waitForRequest((req) =>
      req.url().includes("overrides") && req.method() === "POST");
    await firstItem.getByRole("button", { name: "Unpin" }).click();
    const unpinPost = await unpinReq;
    expect(JSON.parse(unpinPost.postData() || "{}")).toMatchObject({
      memory_name: "react-loop", action: "unpin",
    });

    // Button should revert to "Pin"
    await expect(firstItem.getByRole("button", { name: "Pin" })).toBeVisible();
  });

  test("disable then enable toggles button label and marks item omitted", async ({ page }) => {
    // Scope to the first recall item within the memory-side panel
    const firstItem = page.locator(".memory-side .memory-list-item").filter({ hasText: "react-loop" }).first();
    const disableBtn = firstItem.getByRole("button", { name: "Disable" });
    await expect(disableBtn).toBeVisible();

    // Click Disable
    const disableReq = page.waitForRequest((req) =>
      req.url().includes("overrides") && req.method() === "POST");
    await disableBtn.click();
    const disablePost = await disableReq;
    expect(JSON.parse(disablePost.postData() || "{}")).toMatchObject({
      memory_name: "react-loop", action: "disable",
    });

    // Button should now read "Enable"
    await expect(firstItem.getByRole("button", { name: "Enable" })).toBeVisible();
    // The recall item badge should show "disabled_for_session"
    await expect(page.getByText("disabled_for_session").first()).toBeVisible();

    // Click Enable
    const enableReq = page.waitForRequest((req) =>
      req.url().includes("overrides") && req.method() === "POST");
    await firstItem.getByRole("button", { name: "Enable" }).click();
    const enablePost = await enableReq;
    expect(JSON.parse(enablePost.postData() || "{}")).toMatchObject({
      memory_name: "react-loop", action: "enable",
    });

    // Button should revert to "Disable", badge shows "injected" again
    await expect(firstItem.getByRole("button", { name: "Disable" })).toBeVisible();
  });

  test("pin and disable can coexist on different memories", async ({ page }) => {
    // Scope to the first recall item within the memory-side panel
    const firstItem = page.locator(".memory-side .memory-list-item").filter({ hasText: "react-loop" }).first();

    await firstItem.getByRole("button", { name: "Pin" }).click();
    await expect(firstItem.getByRole("button", { name: "Unpin" })).toBeVisible();

    // The toast should confirm pin
    await expect(page.getByText("Memory pinned")).toBeVisible();

    // Unpin, then Disable
    await firstItem.getByRole("button", { name: "Unpin" }).click();
    await firstItem.getByRole("button", { name: "Disable" }).click();
    await expect(firstItem.getByRole("button", { name: "Enable" })).toBeVisible();
    await expect(page.getByText("Memory disabled")).toBeVisible();

    // Re-enable
    await firstItem.getByRole("button", { name: "Enable" }).click();
    await expect(firstItem.getByRole("button", { name: "Disable" })).toBeVisible();
    await expect(page.getByText("Memory enabled")).toBeVisible();
  });
});
