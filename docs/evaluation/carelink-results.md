# CareLink evaluation: results (2026-10-08)

Input: [`carelink-requirements.md`](carelink-requirements.md) sent to the assistant (Edit mode) of a blank project;
scored with [`carelink-rubric.md`](carelink-rubric.md) by `scripts/eval_carelink.py`, then reviewed by hand.
Model: Gemini 3.1 Pro (reasoning group) through ArchFlow's LLM gateway.

## High-level design

| | First pass | After follow-ups |
|---|---|---|
| Rubric areas covered | 26/26 | 26/26 |
| Components / connections | 25 / 41 | 40 / 51 |
| Requirements (functional + non-functional) | 17 (10 + 7), all traced | 17, all traced |
| NFR numbers captured (p95 300 ms / 1 s / 3 s, 99.95%, 50k, retention, alert) | 8/8 | 8/8 |
| No client → database, no MongoDB, both PSPs, LLM behind the agent | ✓ | ✓ |
| ArchFlow health warnings | 0 (should have been 1, see bug 1) | 0 |

Manual review of the first pass:
- Correct: three clients, API gateway, auth, booking + Redis for holds, payments with Razorpay and Stripe, LiveKit SFU,
  triage agent → Claude on Bedrock, records, e-prescriptions + pharmacy API, notifications through EventBridge,
  reporting, audit, S3, Secrets Manager, CloudWatch/X-Ray, offline requirement for the app.
- Weak: one **shared "Core DB" for 9 services**; data residency only as a requirement (no regional cells); payment
  providers and messaging providers merged into single nodes; guardrails folded into the agent's description;
  reporting querying the primary database; the model named "Claude 3".

Follow-up questions asked by the AI (5): regional isolation, pharmacy verification, interpreter identity,
emergency-number location. All relevant; one was asked twice (bug 2).

Follow-up edits (all applied correctly, including references to earlier turns):
1. Answers → Region Router + Tenant Directory in front of the gateway (clients re-routed); Core DB replaced by one
   PostgreSQL database per service.
2. Payment Gateways split into Stripe and Razorpay + Payment Webhook Handler with Redis idempotency; "the model it
   uses" resolved to Claude, now reached only through Bedrock Guardrails.
3. "The handler you just added" → dead-letter queue on the webhook handler; reporting reads a Booking DB replica.

## Low-level design (Booking Service)

| | Before fix | After fix |
|---|---|---|
| Endpoints | 4: search, lock, book, get | 6: search, book (held), confirm, get, cancel, list a patient's bookings |
| Tables | schedules, bookings (status, expires_at) | slots (incl. language), bookings (`slot_id` unique, `held_until`, status) |
| Events | BookingConfirmed | BookingHeld, BookingConfirmed, BookingCancelled |
| Cancellation (FR3: refunds > 24 h) | missing | covered (REQ-003 linked) |

Remaining LLD note: a plain unique key on `bookings.slot_id` would also block re-booking a cancelled slot; it needs
a partial unique index on active bookings (the column format cannot express that yet).

## Bugs found and fixed
1. **Shared-database check skipped** whenever one reader was an agent (here 9 services shared Core DB unflagged).
   Now counts services, agents, MCP servers and components; warning from 3 readers (`analysis.py`).
2. **Duplicate clarifying questions** across the command's rounds. Later rounds are told which questions were
   asked, and rewordings are dropped (`ai.dedupe_questions`).
3. **False "LLM exposed" warning** when a guardrail (security component) sits in front of the model.
4. **LLD missed lifecycle operations**: the prompt only saw the component's own requirements (cancellation lives
   under payments) and capped endpoints at 6. It now gets the other requirements as context and must cover the full
   entity lifecycle, state-change events and invariants (`lld.py`).
5. Scoring script: numbers matched only with exact spacing ("300 ms" vs "300ms"); fixed.

Tests: 262 passed, 16 skipped (3 new tests for bugs 1-3).

## Not yet covered (ideas for the next round)
- Regional cells are drawn as one stack plus a router; a "region" grouping on the canvas would show two stacks.
- Constraints such as "no MongoDB" are respected but not enforced by a policy check.
- Partial unique indexes / check constraints in the LLD table format.
