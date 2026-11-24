"""FastAPI backend: chat streaming, session and query traces, and the web UI."""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .agent import Agent
from .cleaners import CleanerRegistry
from .datastore import DataStore
from .hub import MCPHub
from .knowledge import KnowledgeBase
from .settings import Settings
from .tracing import Tracer

WEB = Path(__file__).parent / "web"


class QueryIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


def to_json(value) -> str:
    return json.dumps(value, default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v))


def json_response(value) -> Response:
    return Response(to_json(value), media_type="application/json")


def valid_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(404, "Not found") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    tracer = Tracer(settings.database_url)
    await tracer.start()
    await tracer.recover_interrupted()
    cleaners = CleanerRegistry(settings.config_dir / "cleaners.yaml")
    hub = MCPHub(settings.config_dir / "servers.yaml", cleaners, settings.max_records_per_fetch,
                 settings.mcp_timeout_seconds)
    await hub.start()
    knowledge = KnowledgeBase(settings.config_dir / "knowledge" / "intents.md", list(hub.connections))
    store = DataStore(settings.data_dir / "sessions", settings.sql_timeout_seconds)
    app.state.tracer, app.state.hub = tracer, hub
    app.state.agent = Agent(settings, hub, knowledge, cleaners, store, tracer)
    app.state.running = set()
    yield
    if app.state.running:
        await asyncio.wait(app.state.running, timeout=30)
    await hub.close()
    store.close()
    await tracer.close()


app = FastAPI(title="OpsAgent", lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/vendor/plotly.min.js")
async def plotly_js():
    from plotly.offline import get_plotlyjs
    return Response(get_plotlyjs(), media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/health")
async def health():
    systems = app.state.hub.status()
    return {"status": "ok", "connected": sum(s["connected"] for s in systems), "total": len(systems)}


@app.get("/api/systems")
async def systems():
    return app.state.hub.status()


@app.post("/api/sessions", status_code=201)
async def create_session():
    return json_response(await app.state.tracer.create_session())


@app.get("/api/sessions")
async def sessions():
    return json_response(await app.state.tracer.list_sessions())


async def require_session(session_id: str) -> str:
    session_id = valid_id(session_id)
    if not await app.state.tracer.get_session(session_id):
        raise HTTPException(404, "Session not found")
    return session_id


@app.get("/api/sessions/{session_id}")
async def session_detail(session_id: str):
    session_id = await require_session(session_id)
    tracer = app.state.tracer
    queries = await tracer.session_queries(session_id)
    for query in queries:
        query["artifacts"] = await tracer.query_artifacts(str(query["id"]))
    return json_response({"session": await tracer.get_session(session_id), "queries": queries})


@app.get("/api/sessions/{session_id}/events")
async def session_events(session_id: str):
    session_id = await require_session(session_id)
    return json_response(await app.state.tracer.session_events(session_id))


@app.post("/api/sessions/{session_id}/query")
async def ask(session_id: str, body: QueryIn):
    session_id = await require_session(session_id)
    queue: asyncio.Queue = asyncio.Queue()

    # The agent runs independently so a closed browser tab cannot cut a trace short.
    task = asyncio.create_task(app.state.agent.run(session_id, body.question, queue.put))
    app.state.running.add(task)
    task.add_done_callback(app.state.running.discard)

    async def stream():
        while True:
            getter = asyncio.create_task(queue.get())
            finished, _ = await asyncio.wait({getter, task}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
            if getter in finished:
                item = getter.result()
                yield f"data: {to_json(item)}\n\n"
                if item.get("event") == "done":
                    return
                continue
            getter.cancel()
            if task in finished:
                if not queue.empty():
                    continue
                error = str(task.exception()) if task.exception() else "Query ended without a result"
                yield f"data: {to_json({'event': 'done', 'status': 'failed', 'error': error})}\n\n"
                return
            yield ": keepalive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/queries/{query_id}")
async def query_detail(query_id: str):
    query_id = valid_id(query_id)
    tracer = app.state.tracer
    query = await tracer.get_query(query_id)
    if not query:
        raise HTTPException(404, "Query not found")
    return json_response({"query": query, "events": await tracer.query_events(query_id),
                          "artifacts": await tracer.query_artifacts(query_id)})


@app.get("/api/artifacts/{artifact_id}/download")
async def download(artifact_id: str):
    artifact = await app.state.tracer.get_artifact(valid_id(artifact_id))
    if not artifact or artifact["kind"] != "report":
        raise HTTPException(404, "Report not found")
    return PlainTextResponse(artifact["content"]["markdown"], media_type="text/markdown", headers={
        "Content-Disposition": f'attachment; filename="report-{artifact["id"]}.md"'})


def main() -> None:
    import uvicorn
    settings = Settings()
    uvicorn.run("opsagent.api:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
