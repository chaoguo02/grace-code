import { defineConfig } from "playwright/test";
import { existsSync } from "node:fs";

const systemBrowsers = process.platform === "win32"
  ? [
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  ]
  : [];
const localBrowserPath = process.env.PLAYWRIGHT_BROWSER_PATH
  || systemBrowsers.find((path) => existsSync(path));

export default defineConfig({
  testDir: "./e2e",
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "on-first-retry",
    launchOptions: localBrowserPath
      ? { executablePath: localBrowserPath }
      : undefined,
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 5173",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
    cwd: process.cwd(),
  },
});
