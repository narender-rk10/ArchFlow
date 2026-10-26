/**
 * End-to-end tests: the real API, collab server and web app on their own ports with a fresh SQLite database.
 *
 *   npm run e2e                 # offline AI (deterministic): built-in parser, templates, heuristic comparison
 *   E2E_REAL_AI=1 npm run e2e   # also the AI flows (design chat, Ask, follow-ups, review) with the keys in .env
 *
 * Nothing here touches the dev database or the dev servers (ports 8765, 12345 and 5174).
 */
import { defineConfig, devices } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";

const API = 8765;
const COLLAB = 12345;
const WEB = 5174;
const TOKEN = "e2e-internal-token";
const realAI = process.env.E2E_REAL_AI === "1";
// One database per run (the config is loaded by every worker, so the path comes from the main process).
process.env.E2E_DB ??= join(tmpdir(), `archflow-e2e-${process.pid}-${Date.now()}.db`);

const offlineKeys = realAI ? {} : {
  ARCHFLOW_AI: "offline", GEMINI_API_KEY: "", GEMINI_API_KEYS: "", GOOGLE_API_KEY: "", ANTHROPIC_API_KEY: "",
  OPENAI_API_KEY: "", JEV_API_KEY: "",
};

export default defineConfig({
  testDir: "e2e",
  timeout: realAI ? 180_000 : 60_000,
  expect: { timeout: realAI ? 60_000 : 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"], ["html", { open: "never" }]],
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: `http://localhost:${WEB}`,
    storageState: "e2e/.auth/owner.json",
    acceptDownloads: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    viewport: { width: 1440, height: 900 },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: [
    {
      command: `uv run uvicorn archflow.main:app --port ${API}`,
      cwd: "../backend",
      url: `http://localhost:${API}/api/health`,
      timeout: 120_000,
      reuseExistingServer: false,
      env: {
        ...(process.env as Record<string, string>),
        ARCHFLOW_DB: process.env.E2E_DB!, DATABASE_URL: "", ARCHFLOW_INTERNAL_TOKEN: TOKEN,
        ARCHFLOW_COLLAB_URL: `http://localhost:${COLLAB}`, ARCHFLOW_ALLOWED_ORIGINS: `http://localhost:${WEB}`,
        ARCHFLOW_TRACES: "full", PYTHONIOENCODING: "utf-8", ...offlineKeys,
      },
    },
    {
      command: "npx tsx src/server.ts",
      cwd: "../collab",
      url: `http://localhost:${COLLAB}`,
      timeout: 60_000,
      reuseExistingServer: false,
      env: { ...(process.env as Record<string, string>), COLLAB_PORT: String(COLLAB), ARCHFLOW_API: `http://localhost:${API}`, ARCHFLOW_INTERNAL_TOKEN: TOKEN },
    },
    {
      command: `npx vite --port ${WEB} --strictPort`,
      url: `http://localhost:${WEB}`,
      timeout: 60_000,
      reuseExistingServer: false,
      env: { ...(process.env as Record<string, string>), ARCHFLOW_API: `http://localhost:${API}`, ARCHFLOW_COLLAB: `ws://localhost:${COLLAB}` },
    },
  ],
});
