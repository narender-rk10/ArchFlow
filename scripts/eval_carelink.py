"""Evaluate ArchFlow's AI design on docs/evaluation/carelink-requirements.md (rubric: carelink-rubric.md).

Drives a running ArchFlow over its REST API, like a user in the assistant panel:

    python scripts/eval_carelink.py start                    # new project + send the requirements
    python scripts/eval_carelink.py say  <pid> <cid> "text"   # follow-up turn (answers, edits)
    python scripts/eval_carelink.py score <pid>               # HLD score against the rubric
    python scripts/eval_carelink.py lld  <pid> <component>    # generate + score the low-level design

Auth: ARCHFLOW_SESSION=<archflow_session cookie value> (sign in with the browser or curl first).
ARCHFLOW_API defaults to http://localhost:8000. Snapshots are written next to --out (default: ./eval-out).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

API = os.environ.get("ARCHFLOW_API", "http://localhost:8000")
ROOT = Path(__file__).resolve().parent.parent
REQS = ROOT / "docs" / "evaluation" / "carelink-requirements.md"
OUT = Path(os.environ.get("EVAL_OUT", "eval-out"))


def client() -> httpx.Client:
    session = os.environ.get("ARCHFLOW_SESSION")
    if not session:
        sys.exit("Set ARCHFLOW_SESSION to your archflow_session cookie value.")
    return httpx.Client(base_url=API, timeout=600, cookies={"archflow_session": session},
                        headers={"Origin": "http://localhost:5173"})


def turn(c: httpx.Client, pid: str, cid: str, text: str) -> dict:
    r = c.post(f"/api/projects/{pid}/conversations/{cid}/command", json={"text": text, "selection": []})
    r.raise_for_status()
    msg = r.json()["message"]
    data = msg["data"]
    print(f"\n=== AI ({data.get('engine')}, applied={data.get('applied')}) ===")
    print(msg["content"] or "(no explanation)")
    for s in data.get("summary") or []:
        print(f"  + {s}")
    if data.get("questions"):
        print("Questions:")
        for q in data["questions"]:
            print(f"  ? {q}")
    if data.get("error"):
        print("ERROR:", data["error"])
    return msg


# --------------------------------------------------------------------------- HLD rubric
COMPONENTS = {  # area → keyword groups (any group matches; all words in a group must appear in name+tech+desc)
    "patient mobile app": [["mobile"], ["react native"], ["ios"], ["android"]],
    "patient/doctor web": [["web"], ["portal"], ["dashboard", "react"]],
    "api gateway": [["gateway"], ["api", "edge"]],
    "auth / identity (OTP, MFA, SSO)": [["auth"], ["identity"], ["cognito"], ["keycloak"], ["sso"]],
    "appointment / booking": [["appointment"], ["booking"], ["scheduling"]],
    "slot holds (cache/locks)": [["redis"], ["elasticache"], ["cache"], ["lock"]],
    "payment service": [["payment"]],
    "razorpay": [["razorpay"]],
    "stripe": [["stripe"]],
    "video / consultation service": [["video"], ["consultation"], ["webrtc"]],
    "SFU (LiveKit/managed)": [["livekit"], ["sfu"], ["chime"], ["media server"], ["webrtc"]],
    "triage service": [["triage"]],
    "LLM (Claude via Bedrock)": [["bedrock"], ["claude"]],
    "guardrail / PII redaction": [["guardrail"], ["redact"], ["pii"], ["safety"]],
    "clinical records / EHR": [["record"], ["ehr"], ["clinical"], ["notes"]],
    "PostgreSQL": [["postgres"], ["aurora"], ["rds"]],
    "document / recording storage (S3)": [["s3"], ["object storage"], ["storage"]],
    "e-prescription service": [["prescription"]],
    "pharmacy integration": [["pharmacy"]],
    "notification service": [["notification"]],
    "SMS / WhatsApp / push providers": [["sms"], ["whatsapp"], ["push"], ["sns"], ["twilio"], ["pinpoint"]],
    "message bus / queue": [["queue"], ["sqs"], ["kafka"], ["eventbridge"], ["sns"], ["bus"], ["kinesis"]],
    "reporting / analytics": [["report"], ["analytics"], ["warehouse"], ["redshift"]],
    "audit log": [["audit"]],
    "secrets vault": [["secret"], ["vault"], ["kms"]],
    "observability / tracing": [["observability"], ["tracing"], ["opentelemetry"], ["cloudwatch"], ["x-ray"], ["grafana"], ["datadog"]],
}
NFR_NUMBERS = {  # label → accepted spellings (compared without spaces, lower-case)
    "slot search p95 300 ms": ["300ms", "0.3s"], "booking p95 1 s": ["<1s", "1second", "1000ms"],
    "triage 3 s": ["3s", "3second", "3000ms"], "99.95%": ["99.95"], "50k concurrent": ["50,000", "50k", "50000"],
    "10-year records": ["10year"], "30-day recordings": ["30day"], "alert at 1%": ["1%"]}


def text_of(n: dict) -> str:
    return " ".join(str(n.get(k) or "") for k in ("name", "technology", "description", "type")).lower()


def score_hld(state: dict) -> dict:
    nodes = [n for n in state["nodes"].values() if not n.get("parent_id") and n["type"] != "note"]
    texts = {n["id"]: text_of(n) for n in nodes}
    found, missing = {}, []
    for area, groups in COMPONENTS.items():
        hits = [n["name"] for n in nodes if any(all(w in texts[n["id"]] for w in g) for g in groups)]
        if hits:
            found[area] = hits
        else:
            missing.append(area)
    by_id = {n["id"]: n for n in state["nodes"].values()}
    edges = list(state["edges"].values())
    clients = {n["id"] for n in nodes if n["type"] == "client"}
    stores = {n["id"] for n in nodes if n["type"] in ("database", "storage", "cache")}
    llms = {n["id"] for n in nodes if n["type"] == "llm"}
    into = lambda nid: [by_id[e["source"]] for e in edges if e["target"] == nid and e["source"] in by_id]  # noqa: E731
    checks = {
        "no client → database/storage": not [e for e in edges if e["source"] in clients and e["target"] in stores],
        "no MongoDB": not any("mongo" in t for t in texts.values()),
        "both Razorpay and Stripe": "razorpay" in found and "stripe" in found,
        "LLM only reached via triage/guardrail": bool(llms) and all(
            all(any(w in text_of(s) for w in ("triage", "guardrail", "redact", "pii", "agent")) for s in into(l))
            for l in llms),
        "every component connected": all(any(e["source"] == n["id"] or e["target"] == n["id"] for e in edges) for n in nodes),
    }
    reqs = list(state["requirements"].values())
    req_text = " ".join(f"{r['title']} {r.get('description', '')} {' '.join(r.get('acceptance_criteria') or [])}" for r in reqs)
    flat = re.sub(r"\s+", "", req_text.lower())
    nfr_hits = [k for k, alts in NFR_NUMBERS.items() if any(x in flat for x in alts)]
    region = bool(re.search(r"ap-south-1|eu-central-1|mumbai|frankfurt|residen", req_text, re.I))
    traced = sum(1 for r in reqs if any(r["id"] in (n.get("requirement_ids") or []) for n in nodes))
    return {
        "components": {"found": len(found), "of": len(COMPONENTS), "missing": missing, "matches": found},
        "checks": checks,
        "requirements": {"total": len(reqs), "functional": sum(r["kind"] == "functional" for r in reqs),
                         "non_functional": sum(r["kind"] == "non_functional" for r in reqs),
                         "nfr_numbers_found": nfr_hits, "nfr_numbers_missing": [k for k in NFR_NUMBERS if k not in nfr_hits],
                         "data_residency_captured": region, "traced_to_components": f"{traced}/{len(reqs)}"},
        "size": {"components": len(nodes), "connections": len(edges)},
    }


def print_score(s: dict) -> None:
    c = s["components"]
    print(f"\nComponents: {c['found']}/{c['of']} rubric areas covered ({s['size']['components']} components, "
          f"{s['size']['connections']} connections)")
    for area in c["missing"]:
        print(f"  ✗ missing: {area}")
    print("Checks:")
    for k, v in s["checks"].items():
        print(f"  {'✓' if v else '✗'} {k}")
    r = s["requirements"]
    print(f"Requirements: {r['total']} ({r['functional']} functional, {r['non_functional']} non-functional), "
          f"traced {r['traced_to_components']}, data residency {'✓' if r['data_residency_captured'] else '✗'}")
    print(f"  NFR numbers found {len(r['nfr_numbers_found'])}/{len(NFR_NUMBERS)}; missing: {r['nfr_numbers_missing']}")


# --------------------------------------------------------------------------- LLD rubric
LLD_ENDPOINTS = {  # label → (method or None, words in the path, path must not contain an id)
    "search slots": (None, ["slot", "availab", "search"], False), "hold slot": (None, ["hold", "reserve", "lock"], False),
    "confirm booking": ("post", ["book", "appointment", "confirm"], False),
    "cancel / reschedule": (None, ["cancel", "reschedul"], False),
    "list my appointments": ("get", ["book", "appointment"], True)}
LLD_TABLES = {"appointments / bookings": ["appointment", "booking"], "slots / availability": ["slot", "availab", "schedule"]}


def score_lld(c: httpx.Client, pid: str, node_id: str) -> None:
    views = c.get(f"/api/projects/{pid}/nodes/{node_id}/views").json()
    eps = [f"{e['method']} {e['path']}".lower() for e in views["api"]["endpoints"]]
    tables = [t["name"].lower() for t in views["er"]["tables"]]
    state = c.get(f"/api/projects/{pid}").json()["state"]
    kids = [n for n in state["nodes"].values() if n.get("parent_id") == node_id]
    events = [n["name"] for n in kids if n["type"] == "event"]
    print(f"\nLLD of {views['name']}: {len(kids)} elements, {len(eps)} endpoints, {len(tables)} tables, {len(events)} events")
    for e in eps:
        print(f"  {e}")
    print("  tables:", tables, "| events:", events)
    for label, (method, words, no_id) in LLD_ENDPOINTS.items():
        ok = any((method is None or e.startswith(method + " ")) and any(w in e for w in words)
                 and not (no_id and e.rstrip("/").endswith("}")) for e in eps)
        print(f"  {'✓' if ok else '✗'} endpoint: {label}")
    for label, words in LLD_TABLES.items():
        ok = any(any(w in t for w in words) for t in tables)
        print(f"  {'✓' if ok else '✗'} table: {label}")
    print(f"  {'✓' if events else '✗'} domain events")
    print(f"  {'✓' if views['sequence']['mermaid'] else '✗'} sequence diagram; {'✓' if views['er']['mermaid'] else '✗'} ER diagram")
    (OUT / f"lld-{node_id}.json").write_text(json.dumps({"views": views, "elements": kids}, indent=1), encoding="utf-8")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    cmd, *args = sys.argv[1:] or ["help"]
    c = client()
    if cmd == "start":
        p = c.post("/api/projects", json={"name": "Eval: CareLink telehealth", "template": "blank"}).json()
        conv = c.post(f"/api/projects/{p['id']}/conversations", json={"kind": "assistant"}).json()
        print(f"project {p['id']}  conversation {conv['id']}")
        text = REQS.read_text(encoding="utf-8")
        turn(c, p["id"], conv["id"], "Design the system for these requirements (requirements and architecture):\n\n" + text)
    elif cmd == "say":
        pid, cid, text = args
        turn(c, pid, cid, text)
    elif cmd == "score":
        (pid,) = args
        state = c.get(f"/api/projects/{pid}").json()["state"]
        s = score_hld(state)
        print_score(s)
        (OUT / f"hld-{pid}.json").write_text(json.dumps({"score": s, "state": state}, indent=1), encoding="utf-8")
        a = c.get(f"/api/projects/{pid}/analysis").json()
        issues = a.get("architecture_issues") or a.get("issues") or []
        print(f"ArchFlow health: {json.dumps(a['health'])[:300]}")
        for i in issues[:12]:
            print(f"  [{i.get('severity')}] {i.get('message')}")
    elif cmd == "lld":
        pid, node = args
        r = c.post(f"/api/projects/{pid}/nodes/{node}/lld", json={"replace": True})
        r.raise_for_status()
        print("generated:", r.json().get("counts"), "engine:", r.json().get("engine"))
        score_lld(c, pid, node)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
