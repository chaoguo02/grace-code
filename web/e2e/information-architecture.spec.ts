import { expect, test } from "playwright/test";

test.beforeEach(async ({ page }) => {
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (!url.pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }
    if (url.pathname === "/api/sessions") {
      await route.fulfill({ json: [] });
      return;
    }
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Not required by shell navigation test" }),
    });
  });
});

test("supports scoped two-level keyboard navigation", async ({ page }) => {
  await page.goto("/?module=system&view=overview");

  const primary = page.getByRole("navigation", { name: "Primary modules" });
  await expect(primary.getByRole("button")).toHaveCount(4);
  await expect(page).toHaveTitle("Overview · Grace Code");
  await expect(page.getByText("Project scope", { exact: true })).toBeVisible();

  // ArrowRight wraps from System (last) to Workbench (first).
  const system = primary.getByRole("button", { name: "System" });
  await system.focus();
  await system.press("ArrowRight");

  await expect(primary.getByRole("button", { name: "Workbench" }))
    .toHaveAttribute("aria-current", "page");
  await expect(page).toHaveURL(/module=workbench.*view=chat/);
  await expect(page).toHaveTitle("Chat · Grace Code");

  const secondary = page.getByRole("navigation", { name: "Workbench views" });
  const chat = secondary.getByRole("tab", { name: "Chat" });
  await expect(chat).toHaveAttribute("tabindex", "0");
  await chat.press("ArrowRight");
  await expect(secondary.getByRole("tab", { name: "Plans" }))
    .toHaveAttribute("aria-selected", "true");
  await expect(page).toHaveURL(/view=plans/);
  await expect(page.getByText("Project + session", { exact: true })).toBeVisible();
});

test("keeps skip navigation and all modules reachable on a narrow viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/?module=overview&view=overview");

  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to main content" });
  await expect(skip).toBeFocused();
  await skip.press("Enter");
  await expect(page.locator("#main-workspace")).toBeFocused();

  const primary = page.getByRole("navigation", { name: "Primary modules" });
  await expect(primary.getByRole("button")).toHaveCount(4);
  await primary.getByRole("button", { name: "System" }).click();
  await expect(page).toHaveURL(/module=system.*view=overview/);
  await expect(page.getByRole("navigation", { name: "System views" }))
    .toBeVisible();
});
