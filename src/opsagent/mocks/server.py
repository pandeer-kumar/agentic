"""Mock MCP server. Run: python -m opsagent.mocks.server <system> [--http PORT]

Payloads are deliberately bloated to exercise the cleanup layer.
"""
from __future__ import annotations

import argparse
import json
import random
import re
from datetime import datetime, timedelta, timezone

from mcp.server.mcpserver import MCPServer

NOW = datetime.now(timezone.utc)
SERVICES = ["checkout", "auth", "search", "payments", "mobile-api", "core-network"]
STATUSES = ["Open", "In Progress", "Scheduled", "Completed", "Failed"]
PRIORITIES = ["Low", "Medium", "High", "Critical"]
TEAMS = ["Network Core", "Edge", "Platform", "SRE", "Security"]
CHANGES = {
    "NET": ["Update BGP policy on {d}", "Replace firewall rule set on {d}", "Upgrade router firmware at {d}"],
    "NOC": ["Rotate load balancer pool in {d}", "DNS failover test for {d}", "Switch maintenance in {d}"],
    "DBA": ["Add index on orders in {d}", "Run schema migration in {d}"],
    "APP": ["Release checkout build for {d}", "Deploy auth hotfix to {d}"],
    "INC": ["Elevated errors in {d}", "Latency spike in {d}"],
}
SITES = ["dc-east-1", "dc-west-2", "eu-central", "ap-south"]
CATEGORIES = {
    "Login failure": ["Cannot log in after password reset", "SSO loop on sign in"],
    "Payment failure": ["Card declined at checkout", "Charged twice for one order"],
    "Slow performance": ["Dashboard takes minutes to load", "Search is timing out"],
    "Mobile crash": ["App crashes on launch", "App freezes on order screen"],
    "Data sync": ["Orders missing from report", "Profile changes not saved"],
    "Notifications": ["Not receiving email alerts", "Duplicate push notifications"],
}
CATEGORY_WEIGHTS = [30, 24, 18, 14, 9, 5]


def stamp(rng: random.Random, days: int) -> datetime:
    return NOW - timedelta(seconds=rng.randint(60, days * 86400))


def bloat(rng: random.Random) -> dict:
    return {
        "self": f"https://mock.invalid/rest/{rng.randint(1, 10**9)}",
        "avatarUrls": {f"{s}x{s}": f"https://mock.invalid/avatar/{s}.png" for s in (16, 24, 32, 48)},
        "_links": {"web": "https://mock.invalid/w", "api": "https://mock.invalid/a"},
        "audit": [{"at": NOW.isoformat(), "blob": "x" * 180} for _ in range(4)],
    }


def jira_data() -> list[dict]:
    rng = random.Random("jira")
    issues = []
    for number in range(1, 1201):
        project = rng.choices(list(CHANGES), [30, 20, 12, 20, 18])[0]
        created = stamp(rng, 14)
        updated = min(NOW, created + timedelta(minutes=rng.randint(0, 600)))
        site = rng.choice(SITES)
        summary = rng.choice(CHANGES[project]).format(d=site)
        issues.append({
            "id": str(100000 + number), "key": f"{project}-{number}", **bloat(rng),
            "fields": {
                "project": {"key": project, **bloat(rng)},
                "issuetype": {"name": "Incident" if project == "INC" else "Change"},
                "summary": summary,
                "description": {"type": "doc", "content": [{"type": "paragraph", "content": [
                    {"type": "text", "text": f"{summary}. Contact owner{number}@example.com or +1 555 010 {number:04d}. "},
                    {"type": "text", "text": "Rollback plan attached. " * 12},
                ]}]},
                "status": {"name": rng.choice(STATUSES), **bloat(rng)},
                "priority": {"name": rng.choices(PRIORITIES, [30, 40, 22, 8])[0]},
                "customfield_10010": {"value": rng.choice(TEAMS)},
                "components": [{"name": rng.choice(SERVICES)}],
                "created": created.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                "updated": updated.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                "comment": {"comments": [{"body": "Reviewed. " * 20, "author": bloat(rng)} for _ in range(2)]},
            },
        })
    return issues


def zendesk_data() -> list[dict]:
    rng = random.Random("zendesk")
    tickets = []
    for number in range(1, 801):
        category = rng.choices(list(CATEGORIES), CATEGORY_WEIGHTS)[0]
        subject = rng.choice(CATEGORIES[category])
        created = stamp(rng, 14)
        tickets.append({
            "id": number, "url": f"https://mock.invalid/tickets/{number}.json", **bloat(rng),
            "subject": subject,
            "description": f"<div><p>{subject}.</p><p>Reach me at user{number}@example.com.</p>"
                           f"<style>.x{{color:red}}</style>{'Steps tried: reinstall. ' * 10}</div>",
            "custom_fields": {"category": category},
            "priority": rng.choices(["low", "normal", "high", "urgent"], [20, 50, 22, 8])[0],
            "status": rng.choice(["new", "open", "pending", "solved"]),
            "via": {"channel": rng.choice(["web", "email", "chat", "api"])},
            "satisfaction_rating": {"score": rng.choice(["good", "bad", "unoffered"])},
            "created_at": created.isoformat().replace("+00:00", "Z"),
            "updated_at": min(NOW, created + timedelta(hours=rng.randint(0, 20))).isoformat().replace("+00:00", "Z"),
        })
    return tickets


def xmatters_data() -> list[dict]:
    rng = random.Random("xmatters")
    return [{
        "eventId": f"xm-{number}", **bloat(rng),
        "name": f"{rng.choice(SERVICES)} alert threshold breached",
        "priority": rng.choices(["LOW", "MEDIUM", "HIGH"], [30, 45, 25])[0],
        "status": rng.choice(["ACTIVE", "SUSPENDED", "TERMINATED"]),
        "properties": {"service": rng.choice(SERVICES)},
        "recipients": [{"targetName": rng.choice(TEAMS), **bloat(rng)}],
        "metrics": {"ackMinutes": rng.randint(1, 45)},
        "created": stamp(rng, 14).isoformat(),
    } for number in range(1, 301)]


def generic_data(system: str) -> list[dict]:
    rng = random.Random(system)
    return [{
        "id": f"{system}-{number}", **bloat(rng),
        "title": f"{rng.choice(SERVICES)} {system} event",
        "details": {"service": rng.choice(SERVICES), "severity": rng.choice(["info", "warning", "critical"]),
                    "owner": {"team": rng.choice(TEAMS), "email": f"team{number}@example.com"}},
        "status": rng.choice(["open", "resolved"]),
        "tags": ["ops", system],
        "created_at": stamp(rng, 14).isoformat(),
    } for number in range(1, 151)]


def within(value: str, hours: int) -> bool:
    parsed = datetime.fromisoformat(re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value.replace("Z", "+00:00")))
    return parsed >= NOW - timedelta(hours=hours)


def filter_jql(issues: list[dict], jql: str) -> list[dict]:
    result = issues
    if match := re.search(r"project\s+in\s*\(([^)]+)\)", jql, re.I):
        keys = {k.strip().strip("'\"") for k in match[1].split(",")}
        result = [i for i in result if i["fields"]["project"]["key"] in keys]
    elif match := re.search(r"project\s*=\s*['\"]?(\w+)", jql, re.I):
        result = [i for i in result if i["fields"]["project"]["key"] == match[1]]
    if match := re.search(r"issuetype\s*=\s*['\"]?(\w+)", jql, re.I):
        result = [i for i in result if i["fields"]["issuetype"]["name"].lower() == match[1].lower()]
    if match := re.search(r"(updated|created)\s*>=\s*-(\d+)([hd])", jql, re.I):
        hours = int(match[2]) * (24 if match[3].lower() == "d" else 1)
        result = [i for i in result if within(i["fields"][match[1].lower()], hours)]
    return result


def build(system: str, **server_args) -> MCPServer:
    server = MCPServer(f"mock-{system}", **server_args)
    if system == "jira":
        issues = jira_data()

        @server.tool(structured_output=False)
        def search_issues(jql: str, start_at: int = 0, max_results: int = 100) -> str:
            """Search Jira issues with JQL. Supports project, issuetype, updated and created filters."""
            found = filter_jql(issues, jql)
            size = min(max_results, 100)
            return json.dumps({"startAt": start_at, "maxResults": size, "total": len(found),
                               "issues": found[start_at:start_at + size]})

        @server.tool(structured_output=False)
        def create_issue(summary: str) -> str:
            """Create a Jira issue. Exists to prove the allowlist blocks write tools."""
            return json.dumps({"created": summary})
    elif system == "zendesk":
        tickets = zendesk_data()

        @server.tool(structured_output=False)
        def search_tickets(hours: int = 24, page: int = 1, per_page: int = 100) -> str:
            """Search Zendesk support tickets created in the last N hours."""
            found = [t for t in tickets if within(t["created_at"], hours)]
            size = min(per_page, 100)
            start = (page - 1) * size
            return json.dumps({"count": len(found), "tickets": found[start:start + size],
                               "next_page": page + 1 if start + size < len(found) else None})
    else:
        rows = xmatters_data() if system == "xmatters" else generic_data(system)
        key = "data" if system == "xmatters" else "items"
        field = "created" if system == "xmatters" else "created_at"

        @server.tool(structured_output=False)
        def list_events(hours: int = 24) -> str:
            """List events from this system in the last N hours."""
            return json.dumps({key: [r for r in rows if within(r[field], hours)]})
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("system")
    parser.add_argument("--http", type=int)
    args = parser.parse_args()
    if args.http:
        build(args.system).run("streamable-http", host="127.0.0.1", port=args.http)
    else:
        build(args.system).run("stdio")


if __name__ == "__main__":
    main()
