"""End-to-end tests: real MCP subprocesses, real Azure SDK, mock endpoint, real Postgres."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid

import pytest
import uvicorn

from opsagent.agent import Agent
from opsagent.cleaners import CleanerRegistry
from opsagent.datastore import DataError, DataStore, build_chart
from opsagent.hub import MCPHub, ToolError
from opsagent.knowledge import KnowledgeBase, parse_hours
from opsagent.mocks.azure import app as azure_app
from opsagent.settings import ROOT, Settings
from opsagent.tracing import Tracer

CONFIG = ROOT / "config"
SYSTEMS = ["jira", "zendesk", "xmatters", "servicenow", "pagerduty", "datadog", "confluence", "github", "slack",
           "statuspage"]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def azure_url():
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(azure_app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture
async def stack(azure_url, tmp_path):
    settings = Settings(_env_file=None, azure_openai_endpoint=azure_url, azure_openai_api_key="test",
                        azure_openai_deployment="mock-gpt",
                        database_url=os.environ.get("DATABASE_URL",
                                                    "postgresql://agentic:agentic_dev@127.0.0.1:5433/agentic"))
    tracer = Tracer(settings.database_url)
    await tracer.start()
    cleaners = CleanerRegistry(CONFIG / "cleaners.yaml")
    hub = MCPHub(CONFIG / "servers.yaml", cleaners, settings.max_records_per_fetch)
    await hub.start()
    store = DataStore(tmp_path)
    agent = Agent(settings, hub, KnowledgeBase(CONFIG / "knowledge/intents.md", list(hub.connections)), cleaners,
                  store, tracer)
    created = []

    async def session():
        row = await tracer.create_session()
        created.append(str(row["id"]))
        return str(row["id"])

    yield agent, tracer, hub, session
    async with tracer.pool.connection() as conn:
        for session_id in created:
            await conn.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
    await hub.close()
    store.close()
    await tracer.close()


def test_cleaner_flattens_redacts_and_dedupes():
    cleaners = CleanerRegistry(CONFIG / "cleaners.yaml")
    issue = {"key": "NET-1", "self": "https://x", "fields": {
        "project": {"key": "NET"}, "summary": "Call +1 555 010 1234",
        "description": {"content": [{"content": [{"text": "mail a@b.com token=abc123 " + "z" * 2000}]}]},
        "created": "2026-01-01T10:00:00.000+0530", "status": {"name": "Open"}}}
    result = cleaners.clean("jira", [issue, issue])
    row = result.records[0]
    assert result.duplicates_removed == 1 and len(result.records) == 1
    assert "[PHONE]" in row["summary"] and "[EMAIL]" in row["description"] and "[SECRET]" in row["description"]
    assert "a@b.com" not in json.dumps(row) and len(row["description"]) <= 600
    assert row["created"] == "2026-01-01T04:30:00Z" and "self" not in row


def test_generic_cleaner_handles_unknown_system():
    cleaners = CleanerRegistry(CONFIG / "cleaners.yaml")
    raw = cleaners.extract("new", {"meta": {"n": 1}, "results": [
        {"id": 1, "url": "https://x", "owner": {"email": "a@b.com"}, "created_at": "2026-01-01T00:00:00Z"}]})
    row = cleaners.clean("new", raw).records[0]
    assert row["owner_email"] == "[EMAIL]" and "url" not in row


@pytest.mark.parametrize("question,intent,systems", [
    ("find out network changes in last 24 hour", "network changes", ["jira"]),
    ("what kind of issues are users facing", "customer issues", ["zendesk"]),
    ("what kind of issues users facing in zendesk", "customer issues", ["zendesk"]),
    ("any sev1 outage this week", "incidents", ["jira", "servicenow", "pagerduty", "statuspage"]),
])
def test_intent_resolution(question, intent, systems):
    knowledge = KnowledgeBase(CONFIG / "knowledge/intents.md", SYSTEMS)
    best = knowledge.resolve(question)["matches"][0]
    assert best["intent"] == intent and best["systems"] == systems


def test_network_intent_uses_board_mapping():
    best = KnowledgeBase(CONFIG / "knowledge/intents.md", SYSTEMS).resolve("network changes in last 6 hours")["matches"][0]
    assert best["boards"] == ["NET", "NOC"] and "-6h" in best["fetch"][0]["arguments"]["jql"]


def test_time_parsing():
    assert [parse_hours(q) for q in ("last 24 hour", "past 7 days", "2 weeks", "last week", "nothing")] == [
        24, 168, 336, 168, 24]


@pytest.mark.parametrize("sql", [
    "DROP TABLE _catalog", "DELETE FROM _catalog", "SELECT 1; DROP TABLE _catalog",
    "COPY _catalog TO 'x.csv'", "ATTACH 'x.db' AS y",
])
async def test_sql_guard_blocks_writes(tmp_path, sql):
    store = DataStore(tmp_path).session("guard")
    with pytest.raises(DataError):
        await store.query(sql)
    store.close()


async def test_sql_cannot_read_files(tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("a\n1\n")
    store = DataStore(tmp_path).session("files")
    with pytest.raises(DataError):
        await store.query(f"SELECT * FROM read_csv('{secret}')")
    store.close()


def test_chart_folds_extra_series():
    rows = [["d", f"s{i}", 10 - i] for i in range(10)]
    chart = build_chart({"columns": ["x", "kind", "v"], "rows": rows}, "bar", "x", "v", "kind", "T")
    assert len(chart["series"]) == 8 and chart["series"][-1]["name"] == "Other"
    assert sum(sum(s["y"]) for s in chart["series"]) == sum(r[2] for r in rows)


async def test_all_ten_servers_connect_and_write_tools_blocked(stack):
    _, _, hub, _ = stack
    assert sorted(s["system"] for s in hub.status() if s["connected"]) == sorted(SYSTEMS)
    with pytest.raises(ToolError, match="allowlist"):
        await hub.connections["jira"].call("create_issue", {"summary": "x"})


async def test_pagination_fetches_every_page(stack):
    _, _, hub, _ = stack
    records, _, pages, capped = await hub.fetch("jira", "search_issues", {"jql": "project in (NET, NOC)"})
    assert len(records) > 100 and pages == len(records) // 100 + 1 and not capped
    assert {r["fields"]["project"]["key"] for r in records} == {"NET", "NOC"}


async def test_network_query_counts_match_source_and_are_traced(stack):
    agent, tracer, hub, session = stack
    session_id = await session()
    result = await agent.run(session_id, "find out network changes in last 24 hour")
    assert result["status"] == "completed", result
    raw, *_ = await hub.fetch("jira", "search_issues", {
        "jql": "project in (NET, NOC) AND issuetype = Change AND updated >= -24h"})
    assert f"Found {len(raw)} network changes" in result["answer"]
    events = await tracer.query_events(result["query_id"])
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert {"query", "llm", "tool", "mcp", "cleanup"} <= {e["type"] for e in events}
    cleanup = next(e for e in events if e["type"] == "cleanup")["payload"]
    assert cleanup["reduction_pct"] > 80
    assert "@example.com" not in json.dumps([e["payload"] for e in events], default=str)


async def test_report_and_chart_without_system_signal(stack):
    agent, tracer, _, session = stack
    session_id = await session()
    report = await agent.run(session_id, "what kind of issues are users facing in last 7 days, create a report")
    chart = await agent.run(session_id, "chart customer issues per day trend for last 14 days")
    assert report["status"] == chart["status"] == "completed"
    assert report["artifacts"][0]["kind"] == "report" and "Login failure" in report["artifacts"][0]["content"]["markdown"]
    assert chart["artifacts"][0]["kind"] == "chart" and chart["artifacts"][0]["content"]["type"] == "line"
    queries = await tracer.session_queries(session_id)
    assert len(queries) == 2 and all(q["status"] == "completed" for q in queries)


async def test_one_failed_system_is_reported_not_fatal(stack):
    agent, tracer, hub, session = stack
    hub.connections["servicenow"].client = None
    result = await agent.run(await session(), "show incidents from the past week")
    assert result["status"] == "completed" and "servicenow is not connected" in result["answer"]
    events = await tracer.query_events(result["query_id"])
    assert any(e["type"] == "mcp" and e["status"] == "error" and "servicenow" in e["name"] for e in events)


async def test_llm_failure_marks_query_failed(stack):
    agent, tracer, _, session = stack
    original = agent.llm.chat.completions.create

    async def fail(**_):
        raise RuntimeError("llm down")
    agent.llm.chat.completions.create = fail
    try:
        result = await agent.run(await session(), "network changes")
    finally:
        agent.llm.chat.completions.create = original
    assert result["status"] == "failed"
    query = await tracer.get_query(result["query_id"])
    assert query["status"] == "failed" and "llm down" in query["error"]


async def test_http_transport(tmp_path):
    port = free_port()
    process = subprocess.Popen([sys.executable, "-m", "opsagent.mocks.server", "jira", "--http", str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    config = tmp_path / "servers.yaml"
    config.write_text(json.dumps({"servers": {"jira": {
        "transport": "http", "url": f"http://127.0.0.1:{port}/mcp", "headers": {"X-Test": uuid.uuid4().hex},
        "allow": ["search_*"]}}}))
    try:
        for _ in range(100):
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        hub = MCPHub(config, CleanerRegistry(CONFIG / "cleaners.yaml"))
        await hub.start()
        records, *_ = await hub.fetch("jira", "search_issues", {"jql": "project = NET"})
        await hub.close()
        assert records
    finally:
        process.terminate()
        process.wait(5)
