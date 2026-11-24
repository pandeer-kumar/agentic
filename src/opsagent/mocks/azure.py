"""A rule-based Azure OpenAI chat completions endpoint for offline runs.

It speaks the real wire format and drives the real agent tools. It does not reason,
so it only proves plumbing, not model quality.
"""
from __future__ import annotations

import json
import re
import time
import uuid

from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI(title="Mock Azure OpenAI")
PREFERRED = ["category", "status", "severity", "priority", "service", "title", "name"]


def tool_call(name: str, **arguments) -> dict:
    return {"id": f"call_{uuid.uuid4().hex[:16]}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def turn(messages: list[dict]) -> tuple[str, list[dict]]:
    last = max(i for i, m in enumerate(messages) if m["role"] == "user")
    return messages[last]["content"], messages[last + 1:]


def outputs(recent: list[dict]) -> dict[str, list[dict]]:
    names = {c["id"]: c["function"]["name"] for m in recent for c in m.get("tool_calls") or []}
    found: dict[str, list[dict]] = {}
    for message in recent:
        if message["role"] == "tool":
            try:
                value = json.loads(message["content"])
            except json.JSONDecodeError:
                value = {"error": "truncated"}
            found.setdefault(names.get(message["tool_call_id"], "?"), []).append(value)
    return found


def group_column(profile: dict, hints: list[str]) -> str | None:
    columns = {c["name"]: c for c in profile["columns"]}
    for name in [*hints, *PREFERRED]:
        if name in columns and 1 < columns[name].get("distinct", 0) <= 30:
            return name
    return next((n for n, c in columns.items() if c["type"] == "VARCHAR" and 1 < c["distinct"] <= 30), None)


def time_column(profile: dict) -> str | None:
    columns = [c["name"] for c in profile["columns"] if c["type"].startswith("TIMESTAMP")]
    return next((c for c in ("created", "created_at", "updated") if c in columns), columns[0] if columns else None)


def decide(messages: list[dict]) -> dict:
    question, recent = turn(messages)
    lower = question.lower()
    seen = outputs(recent)
    if "resolve_intent" not in seen:
        return {"tool_calls": [tool_call("resolve_intent", query=question)]}
    resolved = seen["resolve_intent"][0]
    matches = resolved.get("matches") or []
    if not matches or not matches[0]["fetch"]:
        return {"content": "I could not map that request to a configured intent. Add it to intents.md or name a system."}
    best = matches[0]
    if "fetch_data" not in seen:
        return {"tool_calls": [tool_call("fetch_data", **r) for r in best["fetch"]]}

    failed = [f["error"] for f in seen["fetch_data"] if "error" in f]
    loaded = [f for f in seen["fetch_data"] if f.get("dataset")]
    window = f"last {resolved['time_window_hours']} hours"
    if not loaded:
        note = f" Source errors: {'; '.join(failed)}" if failed else ""
        return {"content": f"No records found for {best['intent']} in the {window}.{note}"}
    profile = max(loaded, key=lambda f: f["rows"])
    table, column = profile["dataset"], group_column(profile, best["group_by"])
    if not column:
        return {"content": f"Loaded {profile['rows']} records from {profile['system']} in the {window}."}
    sql = f'SELECT "{column}" AS label, count(*) AS records FROM "{table}" GROUP BY 1 ORDER BY records DESC, label'
    if "query_data" not in seen:
        return {"tool_calls": [tool_call("query_data", sql=sql)]}
    summary = seen["query_data"][0]
    if "error" in summary:
        return {"content": f"Analysis failed: {summary['error']}"}
    rows = summary["rows"]

    wants_chart = bool(re.search(r"\b(graph|chart|plot|trend|visuali[sz]e)\b", lower))
    if wants_chart and "create_chart" not in seen:
        moment = time_column(profile)
        if re.search(r"\b(trend|over time|per day|daily)\b", lower) and moment:
            chart_sql = (f"SELECT strftime(date_trunc('day', \"{moment}\"), '%Y-%m-%d') AS day, count(*) AS records "
                         f'FROM "{table}" GROUP BY 1 ORDER BY 1')
            return {"tool_calls": [tool_call("create_chart", sql=chart_sql, chart_type="line", x="day", y="records",
                                             title=f"{best['intent'].title()} per day")]}
        return {"tool_calls": [tool_call("create_chart", sql=sql, chart_type="hbar", x="label", y="records",
                                         title=f"{best['intent'].title()} by {column}")]}

    total = sum(r[1] for r in rows)
    lines = [f"- {r[0]}: {r[1]} ({100 * r[1] / total:.1f}%)" for r in rows[:8]]
    wants_report = bool(re.search(r"\b(report|analy[sz]e|analysis)\b", lower))
    if wants_report and "write_report" not in seen:
        body = (f"**Window:** {window}\n\n"
                f"**Records:** {total} from {profile['system']}\n\n## Breakdown by {column}\n\n" + "\n".join(lines))
        return {"tool_calls": [tool_call("write_report", title=f"{best['intent'].title()} report", markdown=body)]}

    systems = ", ".join(sorted({f["system"] for f in loaded}))
    note = f"\n\nSome sources failed: {'; '.join(failed)}" if failed else ""
    return {"content": f"Found {total} {best['intent']} records in the {window} (largest source: "
                       f"{profile['system']}; loaded from {systems}). By {column}:\n" + "\n".join(lines) + note}


@app.post("/openai/deployments/{deployment}/chat/completions")
async def completions(deployment: str, request: Request, api_version: str | None = None,
                      api_key: str | None = Header(default=None)):
    if not api_key:
        raise HTTPException(401, "Missing api-key header")
    if not request.query_params.get("api-version"):
        raise HTTPException(400, "Missing api-version")
    body = await request.json()
    decision = decide(body["messages"])
    message = {"role": "assistant", "content": decision.get("content")}
    if decision.get("tool_calls"):
        message["tool_calls"] = decision["tool_calls"]
    prompt = len(json.dumps(body["messages"])) // 4
    completion = len(json.dumps(message)) // 4
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion", "created": int(time.time()),
        "model": deployment,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if decision.get("tool_calls") else "stop"}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion},
    }


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8900, log_level="warning")


if __name__ == "__main__":
    main()
