# CareLink: telehealth platform for clinic networks

CareLink lets clinic networks in India and the EU run video consultations, prescriptions and follow-up care. Each clinic network is a tenant; tenants must never see each other's data.

## Users
- **Patients** (mobile app iOS/Android and web): book, pay, join video visits, chat, see records and prescriptions.
- **Doctors** (web): manage availability, run visits, write notes and e-prescriptions.
- **Clinic admins** (web): onboard doctors, set fees, view reports.
- **Pharmacies** (partner API): receive e-prescriptions and report fulfilment.

## Functional requirements
1. **Registration and login**: patients sign up with phone OTP; staff use email + password with mandatory MFA; clinic networks may use their own SSO (SAML/OIDC).
2. **Appointment booking**: patients search doctors by specialty, language and fee, see live slots and book. A slot is held for 10 minutes during payment; double booking must be impossible.
3. **Payments**: pay before the visit with UPI and cards in India (Razorpay) and cards/SEPA in the EU (Stripe). Refunds on cancellation more than 24 h before the visit. Payment webhooks must be processed exactly once.
4. **Video consultations**: 1:1 video with optional interpreter (3 participants), in-call chat and file sharing, works on 3G with adaptive bitrate. Recording only with patient consent; recordings kept 30 days.
5. **AI symptom triage**: before booking, a patient describes symptoms in chat; an LLM suggests urgency (emergency / urgent / routine) and a specialty. Emergency answers must show the local emergency number immediately and never book a slot. The AI must not give a diagnosis; every triage conversation is stored and reviewable by a doctor.
6. **Clinical notes and records**: doctors write structured visit notes (SOAP). Patients see a timeline of visits, notes, prescriptions and uploaded reports (PDF/images up to 20 MB).
7. **E-prescriptions**: doctors issue digitally signed prescriptions; the patient picks a partner pharmacy; the pharmacy receives it via API and reports dispensed / unavailable.
8. **Notifications**: reminders 24 h and 15 min before a visit by push, SMS and WhatsApp; email receipts. Patients choose channels.
9. **Clinic reporting**: daily dashboards of visits, revenue, no-shows and average wait time per doctor; CSV export.
10. **Audit**: every access to a patient record (who, when, why) is logged and visible to the clinic admin and to the patient.

## Non-functional requirements
- **Scale**: 2,000 tenants, 5 million patients, 50,000 concurrent video visits at peak (Monday mornings IST).
- **Latency**: slot search p95 < 300 ms; booking confirmation p95 < 1 s; triage first response < 3 s.
- **Availability**: booking and video 99.95% monthly; reporting 99.5%.
- **Compliance**: GDPR (EU) and India DPDP Act; health data of EU patients must stay in the EU region and Indian patients' data in India. HIPAA-style safeguards: encryption at rest and in transit, least-privilege access.
- **Security**: OWASP ASVS L2; secrets in a vault; all staff actions require MFA.
- **Data retention**: clinical records 10 years; triage chats 3 years; video recordings 30 days unless consent extended.
- **Observability**: distributed tracing across services; alert when booking error rate > 1% for 5 minutes.
- **Cost**: video infrastructure must scale down at night; target < ₹4 per video-minute.
- **Offline**: the patient app shows upcoming visits and prescriptions offline.

## Constraints
- Cloud: AWS (Mumbai ap-south-1 and Frankfurt eu-central-1).
- Backend: Python (FastAPI) for services; React web; React Native mobile.
- Primary database PostgreSQL; no MongoDB.
- Video: use a managed WebRTC SFU or self-host LiveKit; no peer-to-peer for 3-party calls.
- LLM: Claude via AWS Bedrock (data must not leave the region); prompts and outputs must pass PII redaction and safety guardrails.

## Out of scope (v1)
Insurance claims, lab ordering, in-person clinic queueing.
