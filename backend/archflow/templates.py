"""Starter templates: editable architectures, not pictures."""

from __future__ import annotations

from typing import Any

from .models import ProjectState
from .ops import apply_ops


def _node(id: str, type: str, name: str, tech: str, x: float, y: float, reqs: list[str] | None = None, desc: str = ""):
    return {"op": "add_node", "node": {"id": id, "type": type, "name": name, "technology": tech, "description": desc,
                                       "position": {"x": x, "y": y}, "requirement_ids": reqs or []}}


def _edge(s: str, t: str, protocol: str = "", label: str = ""):
    return {"op": "add_edge", "edge": {"source": s, "target": t, "protocol": protocol, "label": label}}


def _req(id: str, title: str, desc: str, kind: str = "functional", priority: str = "medium", actors=None,
         criteria=None, category: str = "", status: str = "draft"):
    return {"op": "add_requirement", "requirement": {
        "id": id, "title": title, "description": desc, "kind": kind, "priority": priority, "category": category,
        "actors": actors or [], "acceptance_criteria": criteria or [], "status": status}}


def ai_customer_support() -> list[dict[str, Any]]:
    return [
        _req("REQ-001", "Multi-channel customer chat", "Customers can chat with the support assistant via WhatsApp and web.",
             priority="high", actors=["customer"], status="approved",
             criteria=["Given a customer on WhatsApp, when they send a message, then they get an AI reply",
                       "Given a customer on the web widget, when they send a message, then they get an AI reply"]),
        _req("REQ-002", "Customer authentication", "Customers must authenticate before accessing order information.",
             priority="high", actors=["customer"], status="approved",
             criteria=["Unauthenticated users cannot retrieve order details"]),
        _req("REQ-003", "Order lookup", "The AI assistant can look up a customer's orders and their status.",
             priority="high", actors=["customer", "AI assistant"],
             criteria=["Given an authenticated customer, when asking 'where is my order', then the latest order status is returned"]),
        _req("REQ-004", "Human escalation", "Conversations can be escalated to a human support agent.",
             actors=["customer", "support agent"]),
        _req("REQ-005", "Response latency", "The system should be fast.", kind="non_functional", category="performance"),
    ]


def ai_customer_support_arch() -> list[dict[str, Any]]:
    return [
        _node("whatsapp", "client", "WhatsApp", "WhatsApp Business API", -260, 0, ["REQ-001"]),
        _node("web-app", "client", "Web App", "React", 60, 0, ["REQ-001"]),
        _node("api-gateway", "gateway", "API Gateway", "Kong", -100, 150, ["REQ-001", "REQ-002"]),
        _node("auth-service", "security", "Auth Service", "Keycloak", -420, 300, ["REQ-002"]),
        _node("support-agent", "agent", "Support Agent", "Python + Claude", -100, 300, ["REQ-001", "REQ-003", "REQ-004"],
              "Router agent that answers questions and calls tools."),
        _node("order-service", "service", "Order Service", "FastAPI", 220, 300, ["REQ-003"]),
        _node("llm", "llm", "Claude", "claude-opus-5", -260, 460),
        _node("orders-mcp", "mcp", "Orders MCP Server", "MCP", 60, 460, ["REQ-003"]),
        _node("orders-db", "database", "Orders DB", "PostgreSQL", 220, 600, ["REQ-003"]),
        _node("helpdesk", "external", "Helpdesk", "Zendesk", -500, 460, ["REQ-004"]),
        _edge("whatsapp", "api-gateway", "HTTPS webhook"),
        _edge("web-app", "api-gateway", "HTTPS"),
        _edge("api-gateway", "auth-service", "OIDC"),
        _edge("api-gateway", "support-agent", "REST"),
        _edge("support-agent", "llm", "Messages API"),
        _edge("support-agent", "orders-mcp", "MCP"),
        _edge("support-agent", "helpdesk", "REST", "escalation"),
        _edge("orders-mcp", "order-service", "REST"),
        _edge("order-service", "orders-db", "SQL"),
    ]


TEMPLATES = {
    "blank": ("Blank project", lambda: []),
    "ai-customer-support": ("AI customer-support platform", lambda: ai_customer_support() + ai_customer_support_arch()),
}


def build_template(key: str) -> ProjectState:
    _, ops = TEMPLATES.get(key, TEMPLATES["blank"])
    state, _ = apply_ops(ProjectState(), ops())
    return state
