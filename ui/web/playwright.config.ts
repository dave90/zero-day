import { defineConfig } from "@playwright/test";
import path from "node:path";
export default defineConfig({
  testDir: "../tests/browser",
  fullyParallel: false,
  workers: 1,
  timeout: 45000,
  use: {
    baseURL: "http://127.0.0.1:4320",
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command:
      `uv run --no-sync --project ../server zero-web --port 4320 --data-root ../.local/browser-tests-${process.pid}`,
    url: "http://127.0.0.1:4320/api/v1/health",
    env: { ZERO_WEB_TEST_MODE: "1" },
    reuseExistingServer: false,
    gracefulShutdown: { signal: "SIGTERM", timeout: 25000 },
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
