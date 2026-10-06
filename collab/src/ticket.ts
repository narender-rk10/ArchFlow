/**
 * Collab tickets, issued by the API (backend archflow/collab_sync.py sign_ticket) and verified here.
 *
 * Format: `<base64url(json claims)>.<base64url(HMAC-SHA256("collab-ticket." + body, ARCHFLOW_INTERNAL_TOKEN))>`
 * Claims: uid, name, color, doc (`<projectId>:<branch>`), ro (read-only), exp (unix seconds, ~2 minutes).
 */
import { createHmac, timingSafeEqual } from "node:crypto";

export interface TicketClaims { uid: string; name: string; color: string; doc: string; ro: boolean; exp: number }

const sign = (secret: string, body: string) => createHmac("sha256", secret).update(`collab-ticket.${body}`).digest();

/** Verify signature (constant time), expiry and document. Throws on any mismatch. */
export function verifyTicket(secret: string, ticket: string, documentName: string, now = Date.now() / 1000): TicketClaims {
  if (!secret) throw new Error("ARCHFLOW_INTERNAL_TOKEN is not set");
  const [body, sig] = (ticket ?? "").split(".");
  if (!body || !sig) throw new Error("missing or malformed ticket");
  const expected = sign(secret, body);
  const given = Buffer.from(sig, "base64url");
  if (given.length !== expected.length || !timingSafeEqual(given, expected)) throw new Error("bad ticket signature");
  const claims = JSON.parse(Buffer.from(body, "base64url").toString("utf8")) as TicketClaims;
  if (typeof claims.exp !== "number" || claims.exp < now) throw new Error("ticket expired");
  if (claims.doc !== documentName) throw new Error("ticket is for another document");
  return claims;
}

/** Mint a ticket (dev scripts only: selftest / peer-demo run next to the server and know the shared secret). */
export function signTicket(secret: string, claims: Omit<TicketClaims, "exp">, ttlSeconds = 120): string {
  const body = Buffer.from(JSON.stringify({ ...claims, exp: Math.floor(Date.now() / 1000) + ttlSeconds })).toString("base64url");
  return `${body}.${sign(secret, body).toString("base64url")}`;
}
