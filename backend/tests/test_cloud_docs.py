"""Official cloud documentation as agent context (Microsoft Learn and AWS Knowledge MCP servers).

No network: the MCP search is faked. The result shapes below are what the real servers returned on 2026-10-21."""

import json
import os
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("ARCHFLOW_DB", ":memory:")

import pytest  # noqa: E402

from archflow import cloud_docs, llm  # noqa: E402
from archflow import agents  # noqa: E402
from archflow.agents import tracing  # noqa: E402
from archflow.cloud_docs import DocsRequest  # noqa: E402

LEARN_RESULT = SimpleNamespace(is_error=False, content=[], structured_content={"results": [
    {"title": "Use Azure Front Door to secure AKS workloads", "content": "Front Door routes to AKS through Private Link.",
     "contentUrl": "https://learn.microsoft.com/azure/architecture/example-scenario/aks-front-door/aks-front-door"},
    {"title": "Architecture best practices for Azure Front Door", "content": "Use WAF policies in prevention mode.",
     "contentUrl": "https://learn.microsoft.com/azure/well-architected/service-guides/azure-front-door"}]})
AWS_RESULT = SimpleNamespace(is_error=False, structured_content=None, content=[SimpleNamespace(text=json.dumps(
    {"content": {"result": [{"rank_order": 1, "title": "Event-driven architectures with DynamoDB and Lambda",
                             "context": "DynamoDB streams capture item changes; Lambda consumes them.",
                             "url": "https://aws.amazon.com/blogs/database/event-driven-dynamodb-lambda/"}]}}))])


@pytest.fixture()
def docs_on(monkeypatch):
    """Lookups enabled, with the MCP search faked; yields the list of (server, query) calls."""
    monkeypatch.setenv("ARCHFLOW_AI", "auto")
    monkeypatch.delenv("ARCHFLOW_DOCS", raising=False)
    monkeypatch.delenv("ARCHFLOW_DOCS_AZURE", raising=False)
    monkeypatch.delenv("ARCHFLOW_DOCS_AWS", raising=False)
    calls = []

    async def fake_search(target, query, limit):
        calls.append((target, query))
        return cloud_docs._parse(LEARN_RESULT if "learn.microsoft.com" in target else AWS_RESULT)

    monkeypatch.setattr(cloud_docs, "_search", fake_search)
    cloud_docs.clear()
    yield calls
    cloud_docs.clear()


# ---------------------------------------------------------------- detection and queries
def test_clouds_are_detected_from_the_request_and_the_canvas():
    assert cloud_docs.clouds("Orders API on AKS with Cosmos DB and Service Bus") == ["azure"]
    assert cloud_docs.clouds("API Gateway, Lambda and DynamoDB") == ["aws"]
    assert cloud_docs.clouds("Move from S3 to Azure Blob Storage") == ["azure", "aws"]
    assert cloud_docs.clouds("Postgres and Redis on Kubernetes") == []  # nothing cloud-specific: no lookup


def test_query_names_the_cloud_and_the_services_involved():
    q = cloud_docs.query_for("azure", "what are the risks?", "Gateway (API Management)\nOrders (AKS)\nDB (Cosmos DB)")
    assert q == "Azure API Management AKS Cosmos DB what are the risks?"
    assert cloud_docs.query_for("aws", "AWS Lambda cold starts", "Lambda") == "AWS Lambda cold starts"


def test_both_result_shapes_are_parsed():
    (first, second) = cloud_docs._parse(LEARN_RESULT)
    assert first["url"].endswith("aks-front-door") and "Private Link" in first["content"]
    (doc,) = cloud_docs._parse(AWS_RESULT)
    assert doc["title"].startswith("Event-driven") and doc["url"].startswith("https://aws.amazon.com/")


def test_search_tool_and_arguments_follow_each_server():
    learn = SimpleNamespace(name="microsoft_docs_search", input_schema={"properties": {"query": {}}})
    aws = SimpleNamespace(name="aws___search_documentation", input_schema={"properties": {"search_phrase": {},
                                                                                           "limit": {}, "topics": {}}})
    other = SimpleNamespace(name="aws___list_regions", input_schema={})
    assert cloud_docs._search_tool([other, aws]) is aws
    assert cloud_docs._arguments(learn, "q", 4) == {"query": "q"}
    assert cloud_docs._arguments(aws, "q", 4) == {"search_phrase": "q", "limit": 4,
                                                   "topics": ["reference_documentation"]}
    with pytest.raises(LookupError):
        cloud_docs._search_tool([other])


# ---------------------------------------------------------------- lookups
def test_references_block_cites_title_source_and_url(docs_on):
    block, calls = cloud_docs.references(DocsRequest("secure ingress", about="Front Door, AKS"))
    assert block.startswith('<untrusted_data source="cloud_docs"') and block.endswith("</cloud_docs>\n</untrusted_data>")
    assert "\n<cloud_docs>\n[1] Use Azure Front Door to secure AKS workloads (Microsoft Learn)\n" in block
    assert "https://learn.microsoft.com/azure/well-architected/service-guides/azure-front-door" in block
    assert [c["source"] for c in calls] == ["Microsoft Learn"] and calls[0]["results"] == 2
    assert docs_on == [("https://learn.microsoft.com/api/mcp", "Azure Front Door AKS secure ingress")]


def test_no_cloud_no_lookup_and_the_switches(docs_on, monkeypatch):
    assert cloud_docs.references(DocsRequest("caching strategy", about="Postgres, Redis")) == ("", [])
    monkeypatch.setenv("ARCHFLOW_DOCS_AZURE", "off")
    block, calls = cloud_docs.references(DocsRequest("ingress", about="AKS"))
    assert block == "" and calls[0]["skipped"] == "source off"
    monkeypatch.setenv("ARCHFLOW_DOCS", "off")
    assert cloud_docs.references(DocsRequest("ingress", about="AKS")) == ("", [])
    monkeypatch.delenv("ARCHFLOW_DOCS")
    monkeypatch.setenv("ARCHFLOW_AI", "offline")
    assert cloud_docs.references(DocsRequest("ingress", about="AKS")) == ("", [])
    assert docs_on == []


def test_parallel_agents_share_one_search(docs_on, monkeypatch):
    slow = cloud_docs._search

    async def slower(target, query, limit):
        time.sleep(0.2)
        return await slow(target, query, limit)

    monkeypatch.setattr(cloud_docs, "_search", slower)
    out = []
    threads = [threading.Thread(target=lambda: out.append(cloud_docs.search("aws", "Lambda retries")))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(docs_on) == 1 and sorted(info["cached"] for _, info in out) == [False, True, True, True]


def test_a_failing_source_is_skipped_for_a_while(docs_on, monkeypatch):
    async def down(target, query, limit):
        raise ConnectionError("unreachable")

    monkeypatch.setattr(cloud_docs, "_search", down)
    block, calls = cloud_docs.references(DocsRequest("ingress", about="AKS"))
    assert block == "" and "unreachable" in calls[0]["error"]
    _, calls = cloud_docs.references(DocsRequest("other question", about="AKS"))
    assert calls[0]["skipped"] == "source failed recently"


# ---------------------------------------------------------------- in the agents
def test_agents_get_the_documents_and_the_trace_records_the_lookup(docs_on, monkeypatch):
    seen = {}

    def fake_complete_json(system, messages, schema, **kw):
        seen.update(system=system, user=messages[-1]["content"])
        return {"summary": "Looks fine.", "findings": []}

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    with tracing.listen(lambda t: None) as traces:
        agents.run_json("review.specialist", {"type": "object"}, docs=DocsRequest("Well-Architected security",
                                                                                   about="Edge (Azure Front Door)"),
                        variables={"focus": "security", "checklist": "auth", "project_json": "{}", "memory": ""})
    assert "Official cloud documentation" in seen["user"] and "aks-front-door" in seen["user"]
    (tr,) = traces
    assert tr["prompt"] == "review.specialist@v2" and tr["metadata"]["docs"] == ["Microsoft Learn"]
    api = [s for s in tr["spans"] if s["kind"] == "api"]
    assert api[0]["name"] == "docs: Microsoft Learn" and api[0]["output"]["results"] == 2
    assert api[0]["input"]["query"] == "Azure Front Door Well-Architected security"


def test_without_a_cloud_the_prompt_has_no_documents(docs_on, monkeypatch):
    seen = {}
    monkeypatch.setattr(llm, "stream_text", lambda system, messages, **kw: seen.update(system=system) or iter(["ok"]))
    with tracing.listen(lambda t: None) as traces:
        out = list(agents.stream_text("assistant.ask", user="Where is the bottleneck?",
                                      docs=DocsRequest("Where is the bottleneck?", about="Postgres, Redis"),
                                      variables={"project_context": "", "memory": "", "summary": ""}))
    assert out == ["ok"] and "Official cloud documentation" not in seen["system"] and docs_on == []
    assert not [s for s in traces[0]["spans"] if s["kind"] == "api"]


def test_streamed_answers_cite_documents(docs_on, monkeypatch):
    seen = {}
    monkeypatch.setattr(llm, "stream_text", lambda system, messages, **kw: seen.update(system=system) or iter(["ok"]))
    list(agents.stream_text("assistant.ask", user="How should Lambda retry?",
                            docs=DocsRequest("How should Lambda retry?", about="Orders (AWS Lambda)"),
                            variables={"project_context": "", "memory": "", "summary": ""}))
    assert "Event-driven architectures with DynamoDB and Lambda (AWS Knowledge)" in seen["system"]
    assert "Markdown link" in seen["system"]
