"""Component catalog: concrete services from every major vendor, mapped to cloud-neutral kinds.

Every entry maps to one canvas node type (how it is drawn) and one *kind* (what it is, e.g.
``db.relational``). The kind is what makes conversion possible: "move this to Azure" swaps each component
for the Azure entry of the same kind.

The catalog is plain data so it can grow (and later load from the database / a public registry) without
code changes. Vendors: generic, aws, azure, gcp, oss (self-hostable open source), saas.
"""

from __future__ import annotations

import re
from typing import Any

VENDORS = {
    "generic": "Generic",
    "aws": "AWS",
    "azure": "Azure",
    "gcp": "Google Cloud",
    "oss": "Open source",
    "saas": "SaaS",
}
CLOUD_TARGETS = ("aws", "azure", "gcp", "oss")
# Kinds that are the same on every platform (your own apps, MCP servers): never converted, never "skipped".
PORTABLE_KINDS = {"client.web", "client.mobile", "ai.mcp"}

# kind → (label, category, node type)
KINDS: dict[str, tuple[str, str, str]] = {
    "client.web": ("Web app", "Clients", "client"),
    "client.mobile": ("Mobile app", "Clients", "client"),
    "edge.cdn": ("CDN", "Networking", "gateway"),
    "edge.dns": ("DNS", "Networking", "gateway"),
    "edge.lb": ("Load balancer", "Networking", "gateway"),
    "edge.gateway": ("API gateway", "Networking", "gateway"),
    "compute.container": ("Containers", "Compute", "service"),
    "compute.kubernetes": ("Kubernetes", "Compute", "service"),
    "compute.function": ("Serverless functions", "Compute", "service"),
    "compute.vm": ("Virtual machines", "Compute", "service"),
    "db.relational": ("Relational database", "Databases", "database"),
    "db.document": ("Document / NoSQL database", "Databases", "database"),
    "db.warehouse": ("Data warehouse", "Databases", "database"),
    "db.vector": ("Vector database", "Databases", "database"),
    "db.search": ("Search engine", "Databases", "database"),
    "cache.kv": ("Cache", "Databases", "cache"),
    "msg.queue": ("Message queue", "Messaging", "queue"),
    "msg.stream": ("Event streaming", "Messaging", "queue"),
    "msg.pubsub": ("Pub/sub & events", "Messaging", "queue"),
    "storage.object": ("Object storage", "Storage", "storage"),
    "storage.file": ("File storage", "Storage", "storage"),
    "security.identity": ("Identity & auth", "Security", "security"),
    "security.secrets": ("Secrets manager", "Security", "security"),
    "security.waf": ("Web application firewall", "Security", "security"),
    "ai.llm": ("LLM / foundation models", "AI", "llm"),
    "ai.agent": ("Agent runtime", "AI", "agent"),
    "ai.ml": ("ML platform", "AI", "service"),
    "ai.mcp": ("MCP server", "AI", "mcp"),
    "ops.monitoring": ("Monitoring & logs", "Operations", "service"),
    "ops.workflow": ("Workflow orchestration", "Operations", "service"),
    "ops.email": ("Email / notifications", "Integration", "external"),
    "ops.payments": ("Payments", "Integration", "external"),
    "ops.messaging": ("SMS / chat messaging", "Integration", "external"),
}

# (id, vendor, kind, name)
_ENTRIES: list[tuple[str, str, str, str]] = [
    # ---- AWS
    ("aws-cloudfront", "aws", "edge.cdn", "Amazon CloudFront"),
    ("aws-route53", "aws", "edge.dns", "Amazon Route 53"),
    ("aws-alb", "aws", "edge.lb", "Elastic Load Balancing"),
    ("aws-api-gateway", "aws", "edge.gateway", "Amazon API Gateway"),
    ("aws-ecs-fargate", "aws", "compute.container", "Amazon ECS on Fargate"),
    ("aws-eks", "aws", "compute.kubernetes", "Amazon EKS"),
    ("aws-lambda", "aws", "compute.function", "AWS Lambda"),
    ("aws-ec2", "aws", "compute.vm", "Amazon EC2"),
    ("aws-rds-postgres", "aws", "db.relational", "Amazon RDS for PostgreSQL"),
    ("aws-aurora", "aws", "db.relational", "Amazon Aurora"),
    ("aws-dynamodb", "aws", "db.document", "Amazon DynamoDB"),
    ("aws-redshift", "aws", "db.warehouse", "Amazon Redshift"),
    ("aws-opensearch", "aws", "db.search", "Amazon OpenSearch Service"),
    ("aws-opensearch-vector", "aws", "db.vector", "OpenSearch vector engine"),
    ("aws-elasticache", "aws", "cache.kv", "Amazon ElastiCache"),
    ("aws-sqs", "aws", "msg.queue", "Amazon SQS"),
    ("aws-msk", "aws", "msg.stream", "Amazon MSK (Kafka)"),
    ("aws-eventbridge", "aws", "msg.pubsub", "Amazon EventBridge"),
    ("aws-sns", "aws", "msg.pubsub", "Amazon SNS"),
    ("aws-s3", "aws", "storage.object", "Amazon S3"),
    ("aws-efs", "aws", "storage.file", "Amazon EFS"),
    ("aws-cognito", "aws", "security.identity", "Amazon Cognito"),
    ("aws-secrets-manager", "aws", "security.secrets", "AWS Secrets Manager"),
    ("aws-waf", "aws", "security.waf", "AWS WAF"),
    ("aws-bedrock", "aws", "ai.llm", "Amazon Bedrock"),
    ("aws-bedrock-agents", "aws", "ai.agent", "Amazon Bedrock Agents"),
    ("aws-sagemaker", "aws", "ai.ml", "Amazon SageMaker"),
    ("aws-cloudwatch", "aws", "ops.monitoring", "Amazon CloudWatch"),
    ("aws-step-functions", "aws", "ops.workflow", "AWS Step Functions"),
    ("aws-ses", "aws", "ops.email", "Amazon SES"),
    # ---- Azure
    ("azure-front-door", "azure", "edge.cdn", "Azure Front Door"),
    ("azure-dns", "azure", "edge.dns", "Azure DNS"),
    ("azure-load-balancer", "azure", "edge.lb", "Azure Load Balancer"),
    ("azure-apim", "azure", "edge.gateway", "Azure API Management"),
    ("azure-container-apps", "azure", "compute.container", "Azure Container Apps"),
    ("azure-aks", "azure", "compute.kubernetes", "Azure Kubernetes Service"),
    ("azure-functions", "azure", "compute.function", "Azure Functions"),
    ("azure-vm", "azure", "compute.vm", "Azure Virtual Machines"),
    ("azure-postgres", "azure", "db.relational", "Azure Database for PostgreSQL"),
    ("azure-sql", "azure", "db.relational", "Azure SQL Database"),
    ("azure-cosmos-db", "azure", "db.document", "Azure Cosmos DB"),
    ("azure-synapse", "azure", "db.warehouse", "Azure Synapse Analytics"),
    ("azure-ai-search", "azure", "db.search", "Azure AI Search"),
    ("azure-ai-search-vector", "azure", "db.vector", "Azure AI Search (vectors)"),
    ("azure-redis", "azure", "cache.kv", "Azure Cache for Redis"),
    ("azure-service-bus", "azure", "msg.queue", "Azure Service Bus"),
    ("azure-event-hubs", "azure", "msg.stream", "Azure Event Hubs"),
    ("azure-event-grid", "azure", "msg.pubsub", "Azure Event Grid"),
    ("azure-blob", "azure", "storage.object", "Azure Blob Storage"),
    ("azure-files", "azure", "storage.file", "Azure Files"),
    ("azure-entra-id", "azure", "security.identity", "Microsoft Entra ID"),
    ("azure-key-vault", "azure", "security.secrets", "Azure Key Vault"),
    ("azure-waf", "azure", "security.waf", "Azure Web Application Firewall"),
    ("azure-openai", "azure", "ai.llm", "Azure OpenAI / AI Foundry"),
    ("azure-ai-agent-service", "azure", "ai.agent", "Azure AI Agent Service"),
    ("azure-ml", "azure", "ai.ml", "Azure Machine Learning"),
    ("azure-monitor", "azure", "ops.monitoring", "Azure Monitor"),
    ("azure-logic-apps", "azure", "ops.workflow", "Azure Logic Apps"),
    ("azure-communication-services", "azure", "ops.email", "Azure Communication Services"),
    # ---- Google Cloud
    ("gcp-cloud-cdn", "gcp", "edge.cdn", "Cloud CDN"),
    ("gcp-cloud-dns", "gcp", "edge.dns", "Cloud DNS"),
    ("gcp-load-balancing", "gcp", "edge.lb", "Cloud Load Balancing"),
    ("gcp-api-gateway", "gcp", "edge.gateway", "Google Cloud API Gateway"),
    ("gcp-cloud-run", "gcp", "compute.container", "Cloud Run"),
    ("gcp-gke", "gcp", "compute.kubernetes", "Google Kubernetes Engine"),
    ("gcp-cloud-functions", "gcp", "compute.function", "Cloud Run functions"),
    ("gcp-compute-engine", "gcp", "compute.vm", "Compute Engine"),
    ("gcp-cloud-sql", "gcp", "db.relational", "Cloud SQL for PostgreSQL"),
    ("gcp-alloydb", "gcp", "db.relational", "AlloyDB"),
    ("gcp-firestore", "gcp", "db.document", "Firestore"),
    ("gcp-bigquery", "gcp", "db.warehouse", "BigQuery"),
    ("gcp-vertex-search", "gcp", "db.search", "Vertex AI Search"),
    ("gcp-vertex-vector", "gcp", "db.vector", "Vertex AI Vector Search"),
    ("gcp-memorystore", "gcp", "cache.kv", "Memorystore"),
    ("gcp-cloud-tasks", "gcp", "msg.queue", "Cloud Tasks"),
    ("gcp-managed-kafka", "gcp", "msg.stream", "Managed Service for Apache Kafka"),
    ("gcp-pubsub", "gcp", "msg.pubsub", "Pub/Sub"),
    ("gcp-cloud-storage", "gcp", "storage.object", "Cloud Storage"),
    ("gcp-filestore", "gcp", "storage.file", "Filestore"),
    ("gcp-identity-platform", "gcp", "security.identity", "Identity Platform"),
    ("gcp-secret-manager", "gcp", "security.secrets", "Secret Manager"),
    ("gcp-cloud-armor", "gcp", "security.waf", "Cloud Armor"),
    ("gcp-vertex-gemini", "gcp", "ai.llm", "Vertex AI (Gemini)"),
    ("gcp-agent-engine", "gcp", "ai.agent", "Vertex AI Agent Engine"),
    ("gcp-vertex-ml", "gcp", "ai.ml", "Vertex AI Training & Prediction"),
    ("gcp-cloud-monitoring", "gcp", "ops.monitoring", "Cloud Monitoring & Logging"),
    ("gcp-workflows", "gcp", "ops.workflow", "Workflows"),
    # ---- Open source / self-hosted
    ("oss-react", "oss", "client.web", "React web app"),
    ("oss-react-native", "oss", "client.mobile", "React Native app"),
    ("oss-nginx", "oss", "edge.lb", "NGINX"),
    ("oss-kong", "oss", "edge.gateway", "Kong Gateway"),
    ("oss-docker", "oss", "compute.container", "Docker containers"),
    ("oss-kubernetes", "oss", "compute.kubernetes", "Kubernetes"),
    ("oss-knative", "oss", "compute.function", "Knative"),
    ("oss-postgresql", "oss", "db.relational", "PostgreSQL"),
    ("oss-mysql", "oss", "db.relational", "MySQL"),
    ("oss-mongodb", "oss", "db.document", "MongoDB"),
    ("oss-clickhouse", "oss", "db.warehouse", "ClickHouse"),
    ("oss-elasticsearch", "oss", "db.search", "Elasticsearch / OpenSearch"),
    ("oss-pgvector", "oss", "db.vector", "pgvector"),
    ("oss-qdrant", "oss", "db.vector", "Qdrant"),
    ("oss-redis", "oss", "cache.kv", "Redis / Valkey"),
    ("oss-rabbitmq", "oss", "msg.queue", "RabbitMQ"),
    ("oss-kafka", "oss", "msg.stream", "Apache Kafka"),
    ("oss-nats", "oss", "msg.pubsub", "NATS"),
    ("oss-minio", "oss", "storage.object", "MinIO"),
    ("oss-keycloak", "oss", "security.identity", "Keycloak"),
    ("oss-vault", "oss", "security.secrets", "HashiCorp Vault"),
    ("oss-litellm", "oss", "ai.llm", "LiteLLM gateway"),
    ("oss-langgraph", "oss", "ai.agent", "LangGraph agents"),
    ("oss-mlflow", "oss", "ai.ml", "MLflow"),
    ("oss-mcp-server", "oss", "ai.mcp", "MCP server"),
    ("oss-prometheus-grafana", "oss", "ops.monitoring", "Prometheus + Grafana"),
    ("oss-temporal", "oss", "ops.workflow", "Temporal"),
    # ---- SaaS
    ("saas-cloudflare", "saas", "edge.cdn", "Cloudflare"),
    ("saas-vercel", "saas", "client.web", "Vercel"),
    ("saas-supabase", "saas", "db.relational", "Supabase Postgres"),
    ("saas-neon", "saas", "db.relational", "Neon Postgres"),
    ("saas-pinecone", "saas", "db.vector", "Pinecone"),
    ("saas-snowflake", "saas", "db.warehouse", "Snowflake"),
    ("saas-upstash", "saas", "cache.kv", "Upstash Redis"),
    ("saas-confluent", "saas", "msg.stream", "Confluent Cloud"),
    ("saas-auth0", "saas", "security.identity", "Auth0"),
    ("saas-clerk", "saas", "security.identity", "Clerk"),
    ("saas-anthropic", "saas", "ai.llm", "Anthropic Claude API"),
    ("saas-openai", "saas", "ai.llm", "OpenAI API"),
    ("saas-gemini", "saas", "ai.llm", "Gemini API"),
    ("saas-datadog", "saas", "ops.monitoring", "Datadog"),
    ("saas-sendgrid", "saas", "ops.email", "SendGrid"),
    ("saas-stripe", "saas", "ops.payments", "Stripe"),
    ("saas-twilio", "saas", "ops.messaging", "Twilio"),
    ("saas-whatsapp", "saas", "ops.messaging", "WhatsApp Business Platform"),
    ("saas-slack", "saas", "ops.messaging", "Slack"),
]

# Generic building blocks (no vendor), one per node type — what the palette showed before.
_GENERIC: list[tuple[str, str, str, str]] = [
    ("generic-client", "client", "Client", "Clients"),
    ("generic-gateway", "gateway", "Gateway", "Networking"),
    ("generic-service", "service", "Service", "Compute"),
    ("generic-database", "database", "Database", "Databases"),
    ("generic-cache", "cache", "Cache", "Databases"),
    ("generic-queue", "queue", "Queue", "Messaging"),
    ("generic-storage", "storage", "Storage", "Storage"),
    ("generic-agent", "agent", "AI Agent", "AI"),
    ("generic-llm", "llm", "LLM", "AI"),
    ("generic-mcp", "mcp", "MCP", "AI"),
    ("generic-external", "external", "External system", "Integration"),
    ("generic-security", "security", "Security", "Security"),
    ("generic-note", "note", "Note", "Annotations"),
]


def _build() -> list[dict[str, Any]]:
    items = [{"id": cid, "vendor": "generic", "kind": "", "name": name, "type": ntype, "category": cat,
              "kind_label": ""} for cid, ntype, name, cat in _GENERIC]
    for cid, vendor, kind, name in _ENTRIES:
        label, category, ntype = KINDS[kind]
        items.append({"id": cid, "vendor": vendor, "kind": kind, "name": name, "type": ntype,
                      "category": category, "kind_label": label})
    return items


CATALOG: list[dict[str, Any]] = _build()
BY_ID: dict[str, dict[str, Any]] = {c["id"]: c for c in CATALOG}


def catalog_payload() -> dict[str, Any]:
    return {"vendors": VENDORS, "kinds": {k: {"label": v[0], "category": v[1], "type": v[2]} for k, v in KINDS.items()},
            "items": CATALOG}


_STOP = {"amazon", "aws", "azure", "microsoft", "google", "gcp", "for", "the", "and", "on", "of", "a"}
# Words that describe a category rather than a product; never enough on their own to identify a service.
_GENERIC_WORDS = {"api", "gateway", "service", "services", "database", "db", "cache", "queue", "storage", "function",
                  "functions", "app", "apps", "container", "containers", "load", "balancer", "balancing", "engine",
                  "search", "monitoring", "logging", "platform", "cloud", "managed", "web", "file", "files", "dns",
                  "cdn", "machine", "learning", "ai", "agent", "agents", "vector", "virtual", "machines", "analytics",
                  "server", "sql", "identity", "firewall", "application", "communication", "manager", "secret", "secrets"}
# Extra names people commonly use for open-source components.
_ALIASES = {
    "oss-kafka": ["kafka"], "oss-redis": ["redis", "valkey"], "oss-postgresql": ["postgres", "postgresql"],
    "oss-mysql": ["mysql"], "oss-mongodb": ["mongo", "mongodb"], "oss-kubernetes": ["k8s", "kubernetes"],
    "oss-docker": ["docker"], "oss-nginx": ["nginx"], "oss-elasticsearch": ["elasticsearch", "opensearch"],
    "oss-react": ["react"], "oss-react-native": ["react native"], "oss-kong": ["kong"], "oss-minio": ["minio"],
    "oss-rabbitmq": ["rabbitmq"], "oss-keycloak": ["keycloak"], "oss-qdrant": ["qdrant"], "oss-pgvector": ["pgvector"],
}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP}


def _signatures():
    out = []
    for c in CATALOG:
        if c["vendor"] == "generic":
            continue
        names = [n for n in re.split(r"\s*/\s*", re.sub(r"\(.*?\)", "", c["name"])) if n] + _ALIASES.get(c["id"], [])
        for n in names:
            sig = _tokens(n)
            if sig:
                out.append((c, sig, sig - _GENERIC_WORDS))
    return out


_SIGNATURES = _signatures()


def match(node: dict[str, Any]) -> dict[str, Any] | None:
    """Find the catalog entry for a node: by catalog_id, else by the service named in its technology
    (falling back to its name), preferring distinctive product words over category words."""
    cid = node.get("catalog_id")
    if cid and cid in BY_ID:
        return BY_ID[cid]
    tech = _tokens(node.get("technology", ""))
    full = tech | _tokens(node.get("name", ""))
    best, best_score = None, (0, 0, 0)
    for entry, sig, distinctive in _SIGNATURES:
        if distinctive and distinctive <= tech:
            score = (3, len(sig & tech), -len(sig - tech))
        elif sig <= tech:
            score = (2, len(sig), 0)
        elif distinctive and sig <= full:
            score = (1, len(sig), -len(sig - full))
        else:
            continue
        if score > best_score:
            best, best_score = entry, score
    return best


def equivalent(entry: dict[str, Any], target: str) -> dict[str, Any] | None:
    """The first entry of the same kind from the target vendor."""
    if not entry.get("kind"):
        return None
    return next((c for c in CATALOG if c["vendor"] == target and c["kind"] == entry["kind"]), None)


def convert_ops(state, target: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Operations that swap every recognised component for the target vendor's equivalent.

    Returns (ops, skipped) where skipped lists components with no known equivalent.
    """
    if target not in CLOUD_TARGETS:
        raise ValueError(f"target must be one of {', '.join(CLOUD_TARGETS)}")
    ops: list[dict[str, Any]] = []
    skipped: list[str] = []
    for node in state.nodes.values():
        data = node.model_dump()
        entry = match(data)
        if not entry or entry["vendor"] == target or entry["vendor"] == "generic" or entry["kind"] in PORTABLE_KINDS:
            continue
        if entry["vendor"] == "saas" and entry["kind"].startswith(("ops.", "ai.llm")):
            continue  # third-party services (Stripe, Twilio, model APIs) stay as they are
        eq = equivalent(entry, target)
        if not eq:
            skipped.append(node.name)
            continue
        changes: dict[str, Any] = {"technology": eq["name"], "vendor": eq["vendor"], "catalog_id": eq["id"]}
        # Rename only when the component was simply named after the old service.
        if node.name.strip().lower() in (entry["name"].lower(), node.technology.strip().lower()):
            changes["name"] = eq["name"]
        ops.append({"op": "update_node", "id": node.id, "changes": changes})
    return ops, skipped
