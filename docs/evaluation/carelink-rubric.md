# CareLink: evaluation rubric

What a correct ArchFlow design for `carelink-requirements.md` must contain. Used by `scripts/eval_carelink.py`
(scores are keyword checks on the generated model, then reviewed by hand).

## High-level design (HLD)

### Required components (any reasonable name; matched by keywords)
| Area | Expected component | Why |
|---|---|---|
| Clients | Patient mobile app, Patient web, Doctor/admin web | 4 user types, 3 surfaces |
| Edge | API gateway (+ WAF/CDN) | single entry, auth, rate limits |
| Identity | Auth/identity service (OTP, MFA, SSO) | FR1, staff MFA, tenant SSO |
| Scheduling | Appointment/booking service + slot store with holds (Redis or DB locks) | FR2, no double booking |
| Payments | Payment service → Razorpay and Stripe, webhook handler (idempotent) | FR3 |
| Video | Video/consultation service + SFU (LiveKit/managed) + recording storage (S3) | FR4 |
| AI | Triage service → Claude via Bedrock, with PII redaction + guardrails | FR5 |
| Records | Clinical records/EHR service + PostgreSQL + document storage (S3) | FR6 |
| Prescriptions | E-prescription service (signing) + pharmacy partner integration | FR7 |
| Notifications | Notification service + queue + SMS/WhatsApp/push/email providers | FR8 |
| Reporting | Reporting/analytics service + warehouse or read replica | FR9 |
| Audit | Audit log service/store (append-only) | FR10 |
| Platform | Message bus (SQS/SNS/Kafka/EventBridge), secrets vault, observability/tracing | NFRs |

### Required properties
- No client connects directly to a database or storage.
- The LLM is reached only through the triage service with a guardrail/redaction step.
- PostgreSQL is the primary database; **no MongoDB**.
- Payments use **both** Razorpay and Stripe.
- Region split (ap-south-1 / eu-central-1) is captured as a requirement or decision.
- Requirements: the 10 functional requirements plus non-functional requirements with the numbers above
  (p95 300 ms / 1 s / 3 s, 99.95%, 50k concurrent, retention periods).

## Low-level design (LLD) of the Appointment service
- Endpoints: search slots, hold a slot, confirm booking, cancel/reschedule, list my appointments.
- Tables: appointments, slots (or availability), holds or a status column with expiry; foreign keys to patient/doctor.
- Events: appointment booked / cancelled (for notifications and reporting).
- Sequence: client → gateway → appointment service → database/cache, payment confirmation path.

## Conversation behaviour
- Asks clarifying questions for genuinely open points (e.g. SFU choice, data residency routing, SSO scope).
- Follow-up edits apply to the right components, including references like "it".
