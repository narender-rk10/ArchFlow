"""Domain completeness checklists: what a system of a given kind is expected to contain.

Plain data plus a few small functions, so the lists can grow without touching the analysis code:

* ``DOMAINS`` — detectable domains (delivery marketplace, e-commerce, SaaS, fintech, AI assistant, multi-agent,
  IoT, content/media). Each has detection keywords (matched against the idea and the requirements), the
  *capabilities* it needs and the *functional areas* its requirements should cover.
* ``CAPABILITIES`` — what "having" a capability means: component types, catalog kinds or name/technology
  keywords that satisfy it, plus a default component (per cloud) that the offline composer and the deterministic
  repair add when it is missing, and how that component is wired in.
* ``AREAS`` — functional requirement areas with a detection pattern and a requirement stub that has measurable
  acceptance criteria.

Everything here is deterministic and cheap; ``analysis.validate_architecture`` turns missing capabilities into
``completeness`` issues and the design chat feeds them into generation and repair.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# --------------------------------------------------------------------------- capabilities
# words: regex over "name technology description" of top-level components.
# kinds: catalog kinds (catalog.match) that satisfy it. types: node types that satisfy it on their own.
# flow: a flow condition computed by the analysis (e.g. "realtime_return": a realtime channel with a return path).
# component: what to add when missing — tech per cloud (aws/azure/gcp/agnostic), layer, and how to attach it:
#   called   (core service → new), consumer (event bus → new, else core service → new),
#   observer (every service/gateway → new), client (new → API gateway), external (core service → new),
#   storage  (core service → new), keys (data-owning services → new), realtime (see review.attach).
CAPABILITIES: dict[str, dict[str, Any]] = {
    "notifications": {
        "label": "Notifications (push / SMS / email)",
        "why": "Users must learn about order, delivery and account events without polling.",
        "words": r"notif|\bsms\b|e-?mail|twilio|sendgrid|\bfcm\b|apns|\bsns\b|\bses\b|pinpoint|firebase cloud messaging|"
                 r"onesignal|notification hubs|communication services|mailgun|postmark|novu",
        "kinds": ["ops.email", "ops.messaging"],
        "component": {"key": "notifications", "name": "Notification Service", "type": "service", "layer": 2,
                      "tech": {"aws": "Amazon SNS + Pinpoint", "azure": "Azure Notification Hubs",
                               "gcp": "Firebase Cloud Messaging", "agnostic": "Novu with FCM/APNs and Twilio"},
                      "description": "Push, SMS and email notifications driven by domain events",
                      "attach": "consumer", "near": r"order|deliver|core|app"},
    },
    "maps_geo": {
        "label": "Maps, geocoding and routing provider",
        "why": "Addresses must be geocoded, ETAs and routes computed, and couriers shown on a map.",
        "words": r"\bmaps?\b|geocod|routing|\broute\b|mapbox|here (maps|api)|location service|places api|\bosrm\b|"
                 r"graphhopper|\beta\b|distance matrix",
        "kinds": [],
        "component": {"key": "maps", "name": "Maps & Routing API", "type": "external", "layer": 3,
                      "tech": {"aws": "Amazon Location Service", "azure": "Azure Maps", "gcp": "Google Maps Platform",
                               "agnostic": "Mapbox"},
                      "description": "Geocoding, routing and ETA calculation", "attach": "external",
                      "near": r"deliver|dispatch|assign|track|order|core|app"},
    },
    "payments": {
        "label": "Payment processing",
        "why": "Checkout needs a PCI-compliant payment provider.",
        "words": r"payment|stripe|adyen|braintree|paypal|checkout\.com|mollie|razorpay|square\b",
        "kinds": ["ops.payments"],
        "component": {"key": "payments", "name": "Payment Provider", "type": "external", "layer": 3,
                      "tech": {"aws": "Stripe", "azure": "Stripe", "gcp": "Stripe", "agnostic": "Stripe"},
                      "description": "Card payments, refunds and marketplace payouts (Stripe Connect)",
                      "attach": "external", "near": r"order|payment|checkout|billing|core|app"},
    },
    "payouts": {
        "label": "Payouts to sellers / couriers",
        "why": "A marketplace has to pay restaurants, sellers or couriers, with statements and reconciliation.",
        "words": r"payout|stripe connect|disburs|settlement|split payment|adyen for platforms|mangopay|ledger",
        "kinds": [],
        "component": {"key": "payouts", "name": "Payouts & Settlement Service", "type": "service", "layer": 2,
                      "tech": {"aws": "Stripe Connect", "azure": "Stripe Connect", "gcp": "Stripe Connect",
                               "agnostic": "Stripe Connect"},
                      "description": "Scheduled payouts to partners via Stripe Connect, with settlement reports",
                      "attach": "consumer", "near": r"order|payment|billing|core|app"},
    },
    "refunds": {
        "label": "Refunds and cancellations",
        "why": "Cancelled or failed orders must be refunded through the payment provider.",
        "words": r"refund|cancell?ation|chargeback|dispute",
        "kinds": [],
        "component": None,  # a responsibility of the payment flow; repaired by describing it, not by a new box
        "describe": {"near": r"payment|order|checkout|billing|core|app",
                     "text": "Handles cancellations and refunds through the payment provider."},
    },
    "live_tracking": {
        "label": "Live tracking with a return path to clients",
        "why": "Location updates must flow from couriers through the backend back to the customer's app.",
        "words": r"web ?sockets?|mqtt|socket\.io|\bsse\b|server[- ]sent|signalr|iot core|pusher|\bably\b|appsync|realtime|real-time",
        "kinds": [],
        "flow": "realtime_return",
        "component": {"key": "realtime", "name": "Realtime Gateway", "type": "gateway", "layer": 1,
                      "tech": {"aws": "API Gateway WebSocket API", "azure": "Azure Web PubSub", "gcp": "Cloud Run WebSockets",
                               "agnostic": "Socket.IO on Kubernetes"},
                      "description": "Authenticated WebSocket channel that pushes live updates to clients",
                      "attach": "realtime", "near": r"track|deliver|location|dispatch|order|core|app"},
    },
    "observability": {
        "label": "Observability (metrics, logs, tracing)",
        "why": "Without metrics, logs and traces, incidents in a distributed system can't be detected or diagnosed.",
        "words": r"observab|monitor|tracing|telemetry|opentelemetry|\botel\b|prometheus|grafana|datadog|new relic|"
                 r"cloudwatch|x-ray|azure monitor|application insights|cloud logging|cloud monitoring|elk|loki|jaeger|"
                 r"tempo|sentry|honeycomb|langfuse|langsmith|arize",
        "kinds": ["ops.monitoring"],
        "component": {"key": "observability", "name": "Observability Stack", "type": "service", "layer": 3,
                      "tech": {"aws": "Amazon CloudWatch + X-Ray", "azure": "Azure Monitor + Application Insights",
                               "gcp": "Cloud Monitoring + Cloud Trace", "agnostic": "OpenTelemetry + Grafana LGTM"},
                      "description": "Metrics, logs, traces and alerting for every service", "attach": "observer"},
    },
    "media_storage": {
        "label": "Object storage for media",
        "why": "Images and documents belong in object storage, not in the database.",
        "words": r"\bs3\b|blob|object storage|cloud storage|minio|\bgcs\b|bucket",
        "types": ["storage"],
        "kinds": ["storage.object"],
        "component": {"key": "media", "name": "Media Storage", "type": "storage", "layer": 4,
                      "tech": {"aws": "Amazon S3", "azure": "Azure Blob Storage", "gcp": "Cloud Storage",
                               "agnostic": "MinIO"},
                      "description": "Images and documents, served through the CDN", "attach": "storage",
                      "near": r"catalog|menu|product|content|media|restaurant|core|app"},
    },
    "cdn": {
        "label": "CDN for static assets and media",
        "why": "Media and static assets should be cached at the edge, close to users.",
        "words": r"\bcdn\b|cloudfront|front door|cloud cdn|cloudflare|akamai|fastly",
        "kinds": ["edge.cdn"],
        "component": {"key": "cdn", "name": "CDN", "type": "gateway", "layer": 1,
                      "tech": {"aws": "Amazon CloudFront", "azure": "Azure Front Door", "gcp": "Cloud CDN",
                               "agnostic": "Cloudflare"},
                      "description": "Edge cache for static assets and media", "attach": "cdn"},
    },
    "admin_tooling": {
        "label": "Admin / support back-office",
        "why": "Operations and support staff need tooling to manage partners, orders, refunds and disputes.",
        "words": r"admin|back[- ]?office|support (console|portal|tool)|operations (console|portal)|backoffice|helpdesk|zendesk|intercom",
        "kinds": [],
        "component": {"key": "admin", "name": "Admin & Support Console", "type": "client", "layer": 0,
                      "tech": {"aws": "React", "azure": "React", "gcp": "React", "agnostic": "React"},
                      "description": "Back-office for operations and customer support (SSO, role-based)",
                      "attach": "client"},
    },
    "fraud": {
        "label": "Fraud and abuse detection",
        "why": "Payments and promotions attract fraud; risky orders and accounts must be scored.",
        "words": r"fraud|risk scor|radar|sift|abuse|anomaly",
        "kinds": [],
        "component": {"key": "fraud", "name": "Fraud Detection", "type": "service", "layer": 2,
                      "tech": {"aws": "Stripe Radar", "azure": "Stripe Radar", "gcp": "Stripe Radar", "agnostic": "Stripe Radar"},
                      "description": "Scores orders, payments and accounts for fraud before fulfilment",
                      "attach": "called", "near": r"order|payment|checkout|core|app"},
    },
    "search": {
        "label": "Search",
        "why": "Users find products, listings or content through full-text / faceted search.",
        "words": r"search|elasticsearch|opensearch|algolia|meilisearch|typesense|\bsolr\b",
        "kinds": ["db.search"],
        "component": {"key": "search", "name": "Search Index", "type": "database", "layer": 4,
                      "tech": {"aws": "Amazon OpenSearch Service", "azure": "Azure AI Search", "gcp": "Vertex AI Search",
                               "agnostic": "OpenSearch"},
                      "description": "Full-text and faceted search", "attach": "storage",
                      "near": r"catalog|product|search|listing|content|core|app"},
    },
    "key_management": {
        "label": "Key management / secrets",
        "why": "Encryption of personal data needs managed keys (and enables crypto-shredding for erasure).",
        "words": r"\bkms\b|key management|key vault|\bvault\b|secrets? manager|secret manager|\bhsm\b",
        "kinds": ["security.secrets"],
        "component": {"key": "kms", "name": "Key Management", "type": "security", "layer": 1,
                      "tech": {"aws": "AWS KMS", "azure": "Azure Key Vault", "gcp": "Cloud KMS", "agnostic": "HashiCorp Vault"},
                      "description": "Encryption keys and secrets; per-subject keys allow crypto-shredding",
                      "attach": "keys"},
    },
    "audit_log": {
        "label": "Audit log",
        "why": "Regulated and B2B systems must record who did what, immutably.",
        "words": r"audit",
        "kinds": [],
        "component": {"key": "audit", "name": "Audit Log", "type": "database", "layer": 4,
                      "tech": {"aws": "Amazon QLDB", "azure": "Azure SQL Ledger", "gcp": "Cloud Logging (locked bucket)",
                               "agnostic": "PostgreSQL (append-only)"},
                      "description": "Append-only record of security-relevant actions", "attach": "storage",
                      "near": r"user|account|core|app"},
    },
    "billing": {
        "label": "Subscription billing",
        "why": "A SaaS product has plans, subscriptions, invoices and usage metering.",
        "words": r"billing|subscription|invoice|chargebee|stripe billing|metering|\bplans?\b",
        "kinds": [],
        "component": {"key": "billing", "name": "Billing", "type": "external", "layer": 3,
                      "tech": {"aws": "Stripe Billing", "azure": "Stripe Billing", "gcp": "Stripe Billing",
                               "agnostic": "Stripe Billing"},
                      "description": "Plans, subscriptions, invoices and usage metering", "attach": "external",
                      "near": r"account|tenant|user|core|app"},
    },
    "ledger": {
        "label": "Double-entry ledger",
        "why": "Money movements must be recorded in an immutable, balanced ledger.",
        "words": r"ledger",
        "kinds": [],
        "component": {"key": "ledger", "name": "Ledger DB", "type": "database", "layer": 4,
                      "tech": {"aws": "Amazon Aurora PostgreSQL", "azure": "Azure SQL Ledger", "gcp": "Cloud Spanner",
                               "agnostic": "PostgreSQL"},
                      "description": "Double-entry, append-only ledger of all money movements", "attach": "storage",
                      "near": r"payment|account|wallet|transfer|core|app"},
    },
    "kyc": {
        "label": "Identity verification (KYC/AML)",
        "why": "Financial products must verify customers and screen transactions.",
        "words": r"\bkyc\b|\baml\b|onfido|persona|jumio|sumsub|identity verification",
        "kinds": [],
        "component": {"key": "kyc", "name": "KYC Provider", "type": "external", "layer": 3,
                      "tech": {"aws": "Onfido", "azure": "Onfido", "gcp": "Onfido", "agnostic": "Onfido"},
                      "description": "Identity verification and AML screening", "attach": "external",
                      "near": r"account|user|onboard|core|app"},
    },
    "llm": {
        "label": "LLM",
        "why": "An AI assistant needs a model.",
        "types": ["llm"],
        "words": r"\bllm\b|gpt|claude|gemini|bedrock|openai|vertex ai|llama|mistral",
        "kinds": ["ai.llm"],
        "component": None,
    },
    "vector_store": {
        "label": "Vector store for retrieval",
        "why": "Retrieval-augmented answers need an embedding index over the documents.",
        "words": r"vector|pgvector|pinecone|qdrant|weaviate|milvus|chroma|embedding|ai search|opensearch",
        "kinds": ["db.vector"],
        "component": {"key": "vector", "name": "Vector Index", "type": "database", "layer": 4,
                      "tech": {"aws": "OpenSearch Serverless", "azure": "Azure AI Search", "gcp": "AlloyDB pgvector",
                               "agnostic": "pgvector"},
                      "description": "Embeddings for retrieval", "attach": "storage", "near": r"agent|assistant|rag|core|app"},
    },
    "ingestion": {
        "label": "Document ingestion pipeline",
        "why": "Documents must be chunked, embedded and kept in sync with their sources.",
        "words": r"ingest|indexer|embedding (pipeline|job|worker)|crawler|chunk|sync (job|worker)",
        "kinds": [],
        "component": {"key": "ingestion", "name": "Ingestion Pipeline", "type": "service", "layer": 2,
                      "tech": {"aws": "AWS Lambda", "azure": "Azure Functions", "gcp": "Cloud Run jobs",
                               "agnostic": "Python worker (scheduled)"},
                      "description": "Scheduled job: chunks, embeds and indexes source documents", "attach": "ingest"},
    },
    "guardrails": {
        "label": "AI guardrails",
        "why": "Prompt injection, PII leakage and unsafe output must be filtered.",
        "words": r"guardrail|prompt[- ]injection|content (safety|filter)|moderation|llama guard|nemo guardrails|bedrock guardrails",
        "kinds": [],
        "component": {"key": "guardrails", "name": "AI Guardrails", "type": "security", "layer": 2,
                      "tech": {"aws": "Amazon Bedrock Guardrails", "azure": "Azure AI Content Safety",
                               "gcp": "Vertex AI safety filters", "agnostic": "NeMo Guardrails"},
                      "description": "Prompt-injection, PII and output filters around the model", "attach": "guard"},
    },
    "agent_runtime": {
        "label": "Agent orchestrator",
        "why": "Multiple agents need an orchestrator that plans, delegates and tracks state.",
        "types": ["agent"],
        "words": r"orchestrat|agent",
        "kinds": ["ai.agent"],
        "component": None,
    },
    "tools": {
        "label": "Tool access (MCP)",
        "why": "Agents act on systems through permission-checked tools.",
        "types": ["mcp"],
        "words": r"\bmcp\b|tool server|tools? api",
        "kinds": ["ai.mcp"],
        "component": {"key": "tools", "name": "MCP Tool Server", "type": "mcp", "layer": 3,
                      "tech": {"aws": "MCP", "azure": "MCP", "gcp": "MCP", "agnostic": "MCP"},
                      "description": "Exposes business capabilities to agents, permission-checked", "attach": "tools"},
    },
    "agent_memory": {
        "label": "Agent memory / state store",
        "why": "Long-running agent work needs durable state and memory.",
        "words": r"memory|state store|checkpoint|conversation (store|history)",
        "kinds": [],
        "component": {"key": "agent-memory", "name": "Agent Memory", "type": "database", "layer": 4,
                      "tech": {"aws": "Amazon DynamoDB", "azure": "Azure Cosmos DB", "gcp": "Firestore", "agnostic": "PostgreSQL"},
                      "description": "Conversation state, checkpoints and long-term memory", "attach": "storage",
                      "near": r"agent|orchestrat|core|app"},
    },
    "human_review": {
        "label": "Human approval for risky actions",
        "why": "Agents' high-impact actions should wait for a human.",
        "words": r"approval|human[- ]in[- ]the[- ]loop|review queue|escalat",
        "kinds": [],
        "component": None,
        "describe": {"near": r"agent|orchestrat", "text": "High-impact actions wait for human approval."},
    },
    "device_ingest": {
        "label": "Device ingestion (MQTT / IoT hub)",
        "why": "Devices connect through an authenticated, scalable ingestion endpoint.",
        "words": r"mqtt|iot core|iot hub|device gateway|emqx|hivemq|mosquitto",
        "kinds": [],
        "component": {"key": "device-gateway", "name": "Device Gateway", "type": "gateway", "layer": 1,
                      "tech": {"aws": "AWS IoT Core", "azure": "Azure IoT Hub", "gcp": "EMQX on GKE", "agnostic": "EMQX"},
                      "description": "MQTT ingestion with per-device certificates", "attach": "realtime"},
    },
    "timeseries": {
        "label": "Time-series store",
        "why": "Telemetry is queried by time range and must be downsampled and retained cheaply.",
        "words": r"time[- ]?series|timescale|influx|timestream|questdb|clickhouse|bigtable",
        "kinds": [],
        "component": {"key": "timeseries", "name": "Time-series DB", "type": "database", "layer": 4,
                      "tech": {"aws": "Amazon Timestream", "azure": "Azure Data Explorer", "gcp": "Bigtable",
                               "agnostic": "TimescaleDB"},
                      "description": "Telemetry by time range, with retention and downsampling", "attach": "storage",
                      "near": r"telemetry|ingest|device|stream|core|app"},
    },
    "stream_processing": {
        "label": "Event stream",
        "why": "High-volume events need a durable, replayable stream.",
        "types": ["queue"],
        "words": r"kafka|kinesis|event hubs|pub/?sub|stream",
        "kinds": ["msg.stream", "msg.pubsub"],
        "component": None,
    },
    "media_processing": {
        "label": "Media processing (transcoding / thumbnails)",
        "why": "Uploaded media must be transcoded, resized and scanned.",
        "words": r"transcod|mediaconvert|media services|thumbnail|image (processing|resiz)|ffmpeg|imgproxy|transformer",
        "kinds": [],
        "component": {"key": "media-processing", "name": "Media Processing", "type": "service", "layer": 2,
                      "tech": {"aws": "AWS Elemental MediaConvert", "azure": "ffmpeg on Azure Container Apps",
                               "gcp": "Transcoder API", "agnostic": "ffmpeg workers"},
                      "description": "Transcoding, thumbnails and malware scanning of uploads", "attach": "consumer",
                      "near": r"upload|media|content|core|app"},
    },
    "moderation": {
        "label": "Content moderation",
        "why": "User-generated content must be moderated.",
        "words": r"moderat|trust (and|&) safety|rekognition|content safety",
        "kinds": [],
        "component": {"key": "moderation", "name": "Content Moderation", "type": "service", "layer": 2,
                      "tech": {"aws": "Amazon Rekognition", "azure": "Azure AI Content Safety", "gcp": "Cloud Vision SafeSearch",
                               "agnostic": "Moderation workers + review queue"},
                      "description": "Automated and human moderation of user content", "attach": "consumer",
                      "near": r"upload|media|content|core|app"},
    },
    "tenant_isolation": {
        "label": "Tenant isolation",
        "why": "Each customer organization's data must be isolated and every query scoped to its tenant.",
        "words": r"tenant",
        "kinds": [],
        "component": None,
        "describe": {"near": r"account|user|tenant|core|app|gateway",
                     "text": "Every request is scoped to its tenant (row-level security / tenant id on every query)."},
    },
    "sso": {
        "label": "Enterprise SSO (SAML / OIDC)",
        "why": "B2B customers expect single sign-on with their own identity provider.",
        "words": r"\bsso\b|saml|oidc|entra|okta|auth0|workos|keycloak|cognito|identity platform",
        "kinds": ["security.identity"],
        "component": None,
    },
}

# --------------------------------------------------------------------------- functional areas (requirements breadth)
# match: regex over the text of existing requirements; a match means the area is covered.
AREAS: dict[str, dict[str, Any]] = {
    "menu_management": {"match": r"menu|catalog|listing|product (management|catalog)",
                        "title": "Partners manage their menu and availability",
                        "description": "Restaurants/sellers create and edit items, prices, photos, options and opening hours.",
                        "actors": ["Restaurant"],
                        "criteria": ["A menu change is visible to customers within 60 s",
                                     "Items can be marked unavailable in ≤ 2 clicks"]},
    "ordering": {"match": r"checkout|place (an )?order|order placement|ordering|cart",
                 "title": "Customers place and pay for orders",
                 "description": "Customers build a cart, see fees and ETA, pay and receive an order confirmation.",
                 "actors": ["Customer"],
                 "criteria": ["Checkout completes in ≤ 3 steps", "Order confirmation shown within 2 s of successful payment"]},
    "cancellations": {"match": r"cancel",
                      "title": "Order cancellation rules",
                      "description": "Customers, partners and support can cancel an order according to its state, with the right fees.",
                      "actors": ["Customer", "Restaurant", "Support agent"],
                      "criteria": ["Cancellation before acceptance is free and confirmed within 5 s",
                                   "Every cancellation records who cancelled and why"]},
    "refunds": {"match": r"refund|chargeback",
                "title": "Refunds",
                "description": "Full and partial refunds are issued through the payment provider and reflected in partner payouts.",
                "actors": ["Customer", "Support agent"],
                "criteria": ["A refund is initiated within 1 minute of approval", "Partial refunds adjust the partner payout"]},
    "ratings": {"match": r"rating|review|feedback",
                "title": "Ratings and reviews",
                "description": "Customers rate the order and the courier/seller after completion; abusive reviews are moderated.",
                "actors": ["Customer"],
                "criteria": ["Rating prompt shown within 10 minutes of delivery", "Ratings are visible on partner profiles within 5 minutes"]},
    "payouts": {"match": r"payout|settlement|disburs|earnings",
                "title": "Payouts to partners",
                "description": "Restaurants/sellers and couriers are paid out on a schedule with itemized statements.",
                "actors": ["Restaurant", "Courier"],
                "criteria": ["Weekly payouts with a downloadable statement", "Payout totals reconcile with orders, fees and refunds to the cent"]},
    "notifications": {"match": r"notif|push|\bsms\b|e-?mail",
                      "title": "Order and delivery notifications",
                      "description": "Customers, partners and couriers receive push/SMS/email notifications for key events.",
                      "actors": ["Customer", "Restaurant", "Courier"],
                      "criteria": ["Status-change push notification delivered within 5 s (p95)",
                                   "Users can opt out per channel"]},
    "live_tracking": {"match": r"track|live location|real[- ]?time location",
                      "title": "Live order tracking",
                      "description": "Customers follow order status and the courier's position on a map.",
                      "actors": ["Customer", "Courier"],
                      "criteria": ["Courier position on the customer's map is ≤ 5 s old (p95)", "ETA is updated at least every 30 s"]},
    "assignment": {"match": r"assign|dispatch|match(ing)? (courier|driver)",
                   "title": "Courier assignment",
                   "description": "Ready orders are offered to nearby available couriers; unaccepted offers are re-dispatched.",
                   "actors": ["Courier"],
                   "criteria": ["95 % of orders get a courier within 2 minutes", "An offer not accepted within 30 s is re-dispatched"]},
    "support": {"match": r"support|dispute|help ?desk|admin|back[- ]?office",
                "title": "Support and operations console",
                "description": "Support staff look up orders, contact parties, issue refunds and resolve disputes.",
                "actors": ["Support agent"],
                "criteria": ["Any order is found by id, phone or email in ≤ 2 s", "Every support action is audit-logged"]},
    "search": {"match": r"search|browse|discover",
               "title": "Search and discovery",
               "description": "Users find items by text, filters and location.",
               "actors": ["Customer"],
               "criteria": ["Search results return in < 300 ms (p95)", "Filters by category, price and distance"]},
    "returns": {"match": r"return",
                "title": "Returns",
                "description": "Customers request returns; refunds are issued when the item is received.",
                "actors": ["Customer", "Support agent"],
                "criteria": ["A return label is generated within 1 minute", "Refund issued within 24 h of receipt"]},
    "inventory": {"match": r"inventory|stock",
                  "title": "Inventory",
                  "description": "Stock levels are tracked and reserved at checkout so items aren't oversold.",
                  "actors": ["Seller"],
                  "criteria": ["No order is accepted for an out-of-stock item", "Stock reservation expires after 15 minutes"]},
    "onboarding": {"match": r"onboard|sign[- ]?up|registration|invite",
                   "title": "Account onboarding",
                   "description": "Users and organizations sign up, verify and invite team members.",
                   "actors": ["Admin"],
                   "criteria": ["Sign-up to first value in < 5 minutes", "Invites expire after 7 days"]},
    "billing": {"match": r"billing|subscription|invoice|plan",
                "title": "Plans and billing",
                "description": "Organizations choose a plan, are invoiced and can upgrade or cancel.",
                "actors": ["Admin"],
                "criteria": ["Plan changes take effect immediately with proration", "Invoices are emailed within 1 h of period end"]},
    "roles": {"match": r"\brole|permission|rbac|access control",
              "title": "Roles and permissions",
              "description": "Organization admins assign roles; every action is authorized by role.",
              "actors": ["Admin"],
              "criteria": ["Unauthorized actions return 403 and are logged", "Role changes take effect within 60 s"]},
    "audit": {"match": r"audit",
              "title": "Audit trail",
              "description": "Security-relevant actions are recorded immutably and can be exported.",
              "actors": ["Admin"],
              "criteria": ["Audit entries are written within 5 s and cannot be modified", "Audit log export covers the last 12 months"]},
    "transfers": {"match": r"transfer|payment|transaction",
                  "title": "Money movements",
                  "description": "Customers move money; each movement is recorded in a double-entry ledger.",
                  "actors": ["Customer"],
                  "criteria": ["Every transaction is idempotent by client key", "Ledger balances reconcile daily with zero difference"]},
    "kyc": {"match": r"\bkyc\b|verif(y|ication) (identity|customer)|identity verification",
            "title": "Customer verification (KYC)",
            "description": "Customers are verified before they can move money; sanctions are screened.",
            "actors": ["Customer"],
            "criteria": ["90 % of verifications complete automatically within 5 minutes",
                         "Unverified accounts cannot send money"]},
    "statements": {"match": r"statement|history|export",
                   "title": "Statements and transaction history",
                   "description": "Customers see and export their transaction history.",
                   "actors": ["Customer"],
                   "criteria": ["History loads in < 1 s for the last 12 months", "CSV/PDF export available"]},
    "knowledge_sources": {"match": r"document|knowledge|source|ingest|upload",
                          "title": "Knowledge sources are ingested and kept in sync",
                          "description": "Admins connect document sources; changes are re-indexed automatically.",
                          "actors": ["Admin"],
                          "criteria": ["A changed document is searchable within 15 minutes", "Deleted documents disappear from answers within 15 minutes"]},
    "cited_answers": {"match": r"answer|cite|citation|question",
                      "title": "Answers cite their sources",
                      "description": "The assistant answers from the indexed sources and cites them.",
                      "actors": ["User"],
                      "criteria": ["Every answer links at least one source passage", "p95 answer latency < 8 s"]},
    "feedback": {"match": r"feedback|thumbs|rating|evaluat",
                 "title": "Answer feedback and evaluation",
                 "description": "Users rate answers; quality is evaluated against a test set before releases.",
                 "actors": ["User"],
                 "criteria": ["Thumbs up/down on every answer", "Release blocked if eval score drops > 5 %"]},
    "agent_tasks": {"match": r"task|goal|delegat|plan",
                    "title": "Agents plan and delegate tasks",
                    "description": "An orchestrator breaks a goal into tasks and delegates them to specialized agents.",
                    "actors": ["User"],
                    "criteria": ["Each task has an owner agent and a status", "A failed task is retried at most 2 times then escalated"]},
    "approvals": {"match": r"approv|human[- ]in[- ]the[- ]loop|confirm",
                  "title": "Human approval of high-impact actions",
                  "description": "Actions above a risk threshold wait for a human to approve.",
                  "actors": ["Operator"],
                  "criteria": ["No high-impact tool call executes without approval", "Approval requests time out after 24 h"]},
    "device_provisioning": {"match": r"provision|register (a )?device|onboard",
                            "title": "Device provisioning",
                            "description": "Devices are registered with unique credentials and can be revoked.",
                            "actors": ["Operator"],
                            "criteria": ["A device is provisioned in < 1 minute", "A revoked device is disconnected within 60 s"]},
    "alerts": {"match": r"alert|threshold|alarm",
               "title": "Alerts on telemetry",
               "description": "Users define thresholds; breaches trigger notifications.",
               "actors": ["Operator"],
               "criteria": ["Alert delivered within 30 s of the breach", "Alerts are de-duplicated per device"]},
    "uploads": {"match": r"upload|publish|post",
                "title": "Content upload and publishing",
                "description": "Creators upload media, which is processed and published.",
                "actors": ["Creator"],
                "criteria": ["Uploads up to 2 GB resume after network loss", "Media is playable within 5 minutes of upload"]},
    "moderation": {"match": r"moderat|report abuse|flag",
                   "title": "Content moderation and reporting",
                   "description": "Content is screened automatically; users can report abuse for human review.",
                   "actors": ["Moderator"],
                   "criteria": ["Reported content is reviewed within 24 h", "Auto-flagged content is hidden until reviewed"]},
}

# --------------------------------------------------------------------------- domains
DOMAINS: dict[str, dict[str, Any]] = {
    "delivery": {
        "label": "Delivery marketplace",
        "keywords": [r"courier", r"\brider", r"\bdrivers?\b", r"deliver(y|ies)", r"dispatch", r"last[- ]mile", r"restaurant",
                     r"food", r"grocer", r"parcel", r"ride[- ]?hail"],
        "min_hits": 2,
        "capabilities": {"notifications": "warning", "maps_geo": "warning", "payments": "warning", "payouts": "warning",
                         "refunds": "info", "live_tracking": "warning", "observability": "warning",
                         "media_storage": "info", "admin_tooling": "info", "fraud": "info"},
        "areas": ["menu_management", "ordering", "cancellations", "refunds", "ratings", "payouts", "notifications",
                  "live_tracking", "assignment", "support"],
    },
    "marketplace": {
        "label": "Two-sided marketplace",
        "keywords": [r"marketplace", r"sellers?\b", r"vendors?\b", r"buyers?\b", r"listings?\b", r"two[- ]sided", r"hosts?\b",
                     r"merchants?\b"],
        "min_hits": 2,
        "capabilities": {"payments": "warning", "payouts": "warning", "refunds": "info", "notifications": "warning",
                         "search": "info", "media_storage": "info", "admin_tooling": "info", "fraud": "info",
                         "observability": "warning"},
        "areas": ["ordering", "refunds", "ratings", "payouts", "notifications", "search", "support"],
    },
    "ecommerce": {
        "label": "E-commerce",
        "keywords": [r"e-?commerce", r"online (shop|store)", r"\bcart\b", r"checkout", r"\bshop\b", r"products?\b",
                     r"inventory", r"\bsku"],
        "min_hits": 2,
        "capabilities": {"payments": "warning", "refunds": "info", "search": "warning", "notifications": "warning",
                         "media_storage": "info", "cdn": "info", "observability": "warning", "admin_tooling": "info",
                         "fraud": "info"},
        "areas": ["ordering", "search", "inventory", "returns", "refunds", "notifications", "ratings", "support"],
    },
    "saas": {
        "label": "B2B SaaS",
        "keywords": [r"\bsaas\b", r"\bb2b\b", r"multi[- ]?tenan", r"tenants?\b", r"subscriptions?\b", r"workspaces?\b",
                     r"organi[sz]ations?\b"],
        "min_hits": 2,
        "capabilities": {"sso": "warning", "billing": "warning", "tenant_isolation": "warning", "audit_log": "info",
                         "notifications": "info", "observability": "warning", "admin_tooling": "info"},
        "areas": ["onboarding", "roles", "billing", "audit", "notifications"],
    },
    "fintech": {
        "label": "Fintech / payments",
        "keywords": [r"fintech", r"wallet", r"\bbank", r"ledger", r"money transfer", r"\bloans?\b", r"trading", r"remittance",
                     r"\bkyc\b", r"payments? (app|platform)"],
        "min_hits": 1,
        "capabilities": {"ledger": "warning", "key_management": "warning", "audit_log": "warning", "fraud": "warning",
                         "kyc": "warning", "notifications": "info", "observability": "warning"},
        "areas": ["transfers", "kyc", "statements", "audit", "notifications"],
    },
    "ai_assistant": {
        "label": "AI assistant",
        "keywords": [r"chat ?bot", r"\bassistant", r"copilot", r"conversational ai", r"\bai agent"],
        "min_hits": 1,
        "capabilities": {"llm": "warning", "guardrails": "warning", "observability": "warning"},
        "areas": ["feedback"],
    },
    "rag": {
        "label": "RAG / knowledge assistant",
        "keywords": [r"\brag\b", r"retrieval", r"knowledge (base|assistant)", r"question answering",
                     r"over (the |company |internal |our )?(documents|docs|knowledge)", r"semantic search"],
        "min_hits": 1,
        "capabilities": {"llm": "warning", "vector_store": "warning", "ingestion": "warning", "guardrails": "warning",
                         "observability": "warning", "media_storage": "info"},
        "areas": ["knowledge_sources", "cited_answers", "feedback"],
    },
    "multi_agent": {
        "label": "Multi-agent system",
        "keywords": [r"multi[- ]agent", r"autonomous agents?", r"agents? (that|which) (act|use tools)", r"orchestrat\w* agents",
                     r"agentic", r"swarm"],
        "min_hits": 1,
        "capabilities": {"agent_runtime": "warning", "llm": "warning", "tools": "warning", "agent_memory": "warning",
                         "guardrails": "warning", "human_review": "info", "observability": "warning"},
        "areas": ["agent_tasks", "approvals"],
    },
    "iot": {
        "label": "IoT / telemetry",
        "keywords": [r"\biot\b", r"sensors?\b", r"telemetry", r"connected devices", r"device fleet", r"smart (home|meter|factory)",
                     r"\bscada\b", r"wearables?"],
        "min_hits": 1,
        "capabilities": {"device_ingest": "warning", "stream_processing": "warning", "timeseries": "warning",
                         "notifications": "info", "observability": "warning"},
        "areas": ["device_provisioning", "alerts"],
    },
    "media": {
        "label": "Content / media platform",
        "keywords": [r"\bvideos?\b", r"streaming (platform|service)", r"podcast", r"photos?\b", r"\bmedia\b", r"\bcms\b",
                     r"user[- ]generated", r"creators?\b", r"\bblog"],
        "min_hits": 2,
        "capabilities": {"media_storage": "warning", "cdn": "warning", "media_processing": "warning", "search": "info",
                         "moderation": "info", "observability": "warning"},
        "areas": ["uploads", "moderation", "search", "notifications"],
    },
}

_SEV_RANK = {"info": 0, "warning": 1, "error": 2}


def detect_domains(text: str) -> list[str]:
    """Domains whose keywords appear in ``text`` (idea + requirements), strongest first."""
    low = (text or "").lower()
    scored = []
    for key, d in DOMAINS.items():
        hits = sum(1 for k in d["keywords"] if re.search(k, low))
        if hits >= d.get("min_hits", 2):
            scored.append((-hits, key))
    return [k for _, k in sorted(scored)]


def expected_capabilities(domains: Iterable[str]) -> dict[str, str]:
    """capability id → strongest severity across the detected domains."""
    out: dict[str, str] = {}
    for d in domains:
        for cap, sev in DOMAINS[d]["capabilities"].items():
            if cap not in out or _SEV_RANK[sev] > _SEV_RANK[out[cap]]:
                out[cap] = sev
    return out


def node_text(node: Any) -> str:
    get = node.get if isinstance(node, dict) else (lambda k, d="": getattr(node, k, d))
    return f"{get('name', '')} {get('technology', '')} {get('description', '')}".lower()


def satisfied(cap_id: str, nodes: list[Any], flows: dict[str, bool] | None = None) -> bool:
    """Whether ``nodes`` (top-level Node objects or component dicts) provide the capability."""
    from . import catalog  # local: keeps this module plain data at import time

    cap = CAPABILITIES[cap_id]
    if cap.get("flow"):
        return bool((flows or {}).get(cap["flow"]))
    types = set(cap.get("types") or [])
    kinds = set(cap.get("kinds") or [])
    pat = re.compile(cap["words"], re.I) if cap.get("words") else None
    for n in nodes:
        get = n.get if isinstance(n, dict) else (lambda k, d="", n=n: getattr(n, k, d))
        if get("type", "") in types:
            return True
        if pat and pat.search(node_text(n)):
            return True
        if kinds:
            entry = catalog.match({"technology": get("technology", ""), "name": get("name", ""),
                                   "catalog_id": get("catalog_id", "")})
            if entry and entry["kind"] in kinds:
                return True
    return False


def missing_capabilities(text: str, nodes: list[Any], flows: dict[str, bool] | None = None) -> list[dict[str, Any]]:
    """[{id, label, why, severity, domains}] for capabilities the detected domains expect but ``nodes`` lack."""
    domains = detect_domains(text)
    out = []
    for cap_id, sev in expected_capabilities(domains).items():
        if satisfied(cap_id, nodes, flows):
            continue
        cap = CAPABILITIES[cap_id]
        out.append({"id": cap_id, "label": cap["label"], "why": cap["why"], "severity": sev,
                    "domains": [DOMAINS[d]["label"] for d in domains if cap_id in DOMAINS[d]["capabilities"]]})
    return out


def missing_areas(text: str, requirements: Iterable[dict[str, Any]]) -> list[str]:
    """Functional areas of the detected domains that no requirement covers (area ids, in checklist order)."""
    domains = detect_domains(text)
    req_text = " ".join(f"{r.get('title', '')} {r.get('description', '')} {' '.join(r.get('acceptance_criteria') or [])}"
                        for r in requirements).lower()
    seen, out = set(), []
    for d in domains:
        for area in DOMAINS[d]["areas"]:
            if area in seen:
                continue
            seen.add(area)
            if not re.search(AREAS[area]["match"], req_text):
                out.append(area)
    return out


def requirement_stub(area: str) -> dict[str, Any]:
    a = AREAS[area]
    return {"title": a["title"], "description": a["description"], "kind": "functional", "priority": "medium",
            "category": "", "actors": list(a["actors"]), "acceptance_criteria": list(a["criteria"]), "source": "checklist"}
