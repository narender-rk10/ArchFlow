import { request, type FullConfig } from "@playwright/test";
import { mkdirSync } from "node:fs";

export const OWNER = { email: "owner@e2e.test", name: "Olivia Owner", password: "correct horse battery staple" };

/** The first account on a fresh server owns every organization; its session is shared by the tests. */
export default async function globalSetup(config: FullConfig) {
  const baseURL = config.projects[0].use.baseURL!;
  const ctx = await request.newContext({ baseURL, extraHTTPHeaders: { Origin: baseURL } });
  const r = await ctx.post("/api/auth/signup", { data: OWNER });
  if (!r.ok() && r.status() !== 409) throw new Error(`signup failed: ${r.status()} ${await r.text()}`);
  if (r.status() === 409) await ctx.post("/api/auth/login", { data: { email: OWNER.email, password: OWNER.password } });
  mkdirSync("e2e/.auth", { recursive: true });
  await ctx.storageState({ path: "e2e/.auth/owner.json" });
  await ctx.dispose();
}
