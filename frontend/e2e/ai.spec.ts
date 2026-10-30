import { expect, test } from "@playwright/test";
import { assistant, openNewProject, realAI, send, tab } from "./helpers";

// Runs against the real model chain (keys from .env): E2E_REAL_AI=1 npm run e2e -- e2e/ai.spec.ts
test.skip(!realAI, "needs E2E_REAL_AI=1 and a provider key");

test("design, ask and edit: the AI asks in bubbles and suggests follow-ups (nothing hard-coded)", async ({ page }) => {
  await openNewProject(page, `AI ${Date.now()}`);

  // Design mode: starting points written by the LLM for this (empty) project
  await assistant(page, "Design");
  const design = page.locator(".command-bar");
  await expect(design.locator(".dc-starters-label .ai-tag")).toBeVisible();
  await design.locator(".dc-input input, .dc-input textarea").first().fill("A booking app for yoga studios with online payments");
  await design.locator(".dc-input button.primary").click();
  // The interviewer asks a multiple-choice question
  await expect(design.locator(".dc-msg.bot").last()).toBeVisible();
  await expect(design.locator(".dc-msg.bot button").first()).toBeVisible();
});

test("ask mode streams an answer and offers follow-ups; edit mode asks with tap-to-answer options", async ({ page }) => {
  await openNewProject(page, `AI ask ${Date.now()}`, "ai-customer-support");
  await assistant(page, "Ask");
  await send(page, "What happens to order lookups if Orders DB goes down?");
  const panel = page.locator(".command-bar");
  await expect(panel.locator(".cmd-entry.bot").last()).toContainText(/Orders DB/);
  const followups = panel.locator(".cmd-followups");
  await expect(followups.locator("button").first()).toBeVisible();
  await expect(followups).not.toContainText(/Thinking of follow-ups/);

  await assistant(page, "Edit");
  await send(page, "Make it highly available");
  const reply = panel.locator(".cmd-entry.bot").last();
  // Either it changed the canvas, or it asked in a bubble with answers to tap
  await expect(reply.locator(".cmd-changes li, .ask-bubble").first()).toBeVisible();
});

test("health: four specialist agents review in parallel; traces show who asked what", async ({ page }) => {
  await openNewProject(page, `AI review ${Date.now()}`, "ai-customer-support");
  await tab(page, "Health");
  await page.locator(".ai-review").getByRole("button", { name: "Review" }).click();
  await expect(page.locator(".ai-review .specialists .chip")).toHaveCount(4);
  await expect(page.locator(".finding").first()).toBeVisible({ timeout: 120_000 });
  await page.locator(".finding").first().getByRole("button", { name: "Fix with AI" }).click();
  await expect(page.locator(".command-bar .cmd-form input")).not.toHaveValue("");

  await page.getByRole("button", { name: "Back to all projects" }).click();
  await page.getByRole("button", { name: "Traces" }).click();
  const traces = page.getByRole("dialog", { name: /AI traces/ });
  const row = traces.locator(".trace-table tbody tr").filter({ hasText: "Review this architecture" }).first();
  await expect(row).toContainText("Olivia Owner");
  await row.click();
  await expect(traces.getByText(/Agents fired in parallel \(4\)/)).toBeVisible();
});
