"""Durable session, query and event tracking in PostgreSQL."""
from __future__ import annotations

import json
import uuid
from collections import defaultdict
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

MAX_PAYLOAD_CHARS = 20000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id UUID PRIMARY KEY, title TEXT NOT NULL DEFAULT 'New session',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS queries (
  id UUID PRIMARY KEY, session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  question TEXT NOT NULL, answer TEXT, status TEXT NOT NULL DEFAULT 'running', error TEXT,
  prompt_tokens INT NOT NULL DEFAULT 0, completion_tokens INT NOT NULL DEFAULT 0,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(), ended_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS queries_session ON queries(session_id, started_at);
CREATE TABLE IF NOT EXISTS events (
  id BIGSERIAL PRIMARY KEY, session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  query_id UUID NOT NULL REFERENCES queries(id) ON DELETE CASCADE, seq INT NOT NULL,
  type TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL, duration_ms INT,
  payload JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (query_id, seq)
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id);
CREATE INDEX IF NOT EXISTS events_failures ON events(status) WHERE status <> 'ok';
CREATE TABLE IF NOT EXISTS artifacts (
  id UUID PRIMARY KEY, session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  query_id UUID NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, title TEXT NOT NULL, content JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def bounded(payload: dict) -> dict:
    text = json.dumps(payload, default=str).replace("\\u0000", "")
    if len(text) <= MAX_PAYLOAD_CHARS:
        return json.loads(text)
    return {"truncated": True, "original_chars": len(text), "preview": text[:MAX_PAYLOAD_CHARS]}


class Tracer:
    def __init__(self, url: str):
        self.pool = AsyncConnectionPool(url, min_size=1, max_size=10, open=False,
                                        kwargs={"row_factory": dict_row, "autocommit": True})
        self.sequences: dict[str, int] = defaultdict(int)

    async def start(self) -> None:
        await self.pool.open(wait=True, timeout=15)
        async with self.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_lock(727001)")
            try:
                await conn.execute(SCHEMA)
            finally:
                await conn.execute("SELECT pg_advisory_unlock(727001)")

    async def close(self) -> None:
        await self.pool.close()

    async def _one(self, sql: str, args: tuple = ()) -> dict | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(sql, args)
            return await cursor.fetchone()

    async def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(sql, args)
            return await cursor.fetchall()

    async def create_session(self, title: str = "New session") -> dict:
        return await self._one("INSERT INTO sessions(id, title) VALUES (%s, %s) RETURNING *",
                               (str(uuid.uuid4()), title))

    async def get_session(self, session_id: str) -> dict | None:
        return await self._one("SELECT * FROM sessions WHERE id = %s", (session_id,))

    async def list_sessions(self, limit: int = 50) -> list[dict]:
        return await self._all("""
          SELECT s.*, count(q.id)::int AS queries,
                 count(q.id) FILTER (WHERE q.status = 'failed')::int AS failed
          FROM sessions s LEFT JOIN queries q ON q.session_id = s.id
          GROUP BY s.id ORDER BY s.updated_at DESC LIMIT %s""", (limit,))

    async def start_query(self, session_id: str, question: str) -> str:
        query_id = str(uuid.uuid4())
        async with self.pool.connection() as conn:
            await conn.execute("INSERT INTO queries(id, session_id, question) VALUES (%s, %s, %s)",
                               (query_id, session_id, question))
            await conn.execute("""UPDATE sessions SET updated_at = now(),
                 title = CASE WHEN title = 'New session' THEN left(%s, 70) ELSE title END WHERE id = %s""",
                               (question, session_id))
        return query_id

    async def event(self, session_id: str, query_id: str, type_: str, name: str, status: str = "ok",
                    duration_ms: int | None = None, payload: dict | None = None) -> dict:
        self.sequences[query_id] += 1
        return await self._one("""INSERT INTO events(session_id, query_id, seq, type, name, status, duration_ms, payload)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                               (session_id, query_id, self.sequences[query_id], type_, name, status, duration_ms,
                                Jsonb(bounded(payload or {}))))

    async def finish_query(self, query_id: str, status: str, answer: str | None, error: str | None,
                           prompt_tokens: int, completion_tokens: int) -> None:
        self.sequences.pop(query_id, None)
        async with self.pool.connection() as conn:
            await conn.execute("""UPDATE queries SET status=%s, answer=%s, error=%s, prompt_tokens=%s,
                 completion_tokens=%s, ended_at=now() WHERE id=%s""",
                               (status, answer, error, prompt_tokens, completion_tokens, query_id))

    async def add_artifact(self, session_id: str, query_id: str, kind: str, title: str, content: Any) -> dict:
        return await self._one("""INSERT INTO artifacts(id, session_id, query_id, kind, title, content)
             VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                               (str(uuid.uuid4()), session_id, query_id, kind, title, Jsonb(bounded(content)
                                if kind != "chart" else json.loads(json.dumps(content, default=str)))))

    async def history(self, session_id: str, limit: int = 6) -> list[dict]:
        rows = await self._all("""SELECT question, answer FROM queries
             WHERE session_id = %s AND status = 'completed' ORDER BY started_at DESC LIMIT %s""", (session_id, limit))
        return list(reversed(rows))

    async def session_queries(self, session_id: str) -> list[dict]:
        return await self._all("""SELECT q.*, count(e.id)::int AS events,
               count(e.id) FILTER (WHERE e.status <> 'ok')::int AS failed_events
             FROM queries q LEFT JOIN events e ON e.query_id = q.id
             WHERE q.session_id = %s GROUP BY q.id ORDER BY q.started_at""", (session_id,))

    async def get_query(self, query_id: str) -> dict | None:
        return await self._one("SELECT * FROM queries WHERE id = %s", (query_id,))

    async def query_events(self, query_id: str) -> list[dict]:
        return await self._all("SELECT * FROM events WHERE query_id = %s ORDER BY seq", (query_id,))

    async def session_events(self, session_id: str) -> list[dict]:
        return await self._all("SELECT * FROM events WHERE session_id = %s ORDER BY id", (session_id,))

    async def query_artifacts(self, query_id: str) -> list[dict]:
        return await self._all("SELECT * FROM artifacts WHERE query_id = %s ORDER BY created_at", (query_id,))

    async def get_artifact(self, artifact_id: str) -> dict | None:
        return await self._one("SELECT * FROM artifacts WHERE id = %s", (artifact_id,))

    async def recover_interrupted(self) -> int:
        """Marks queries left running by a crash so traces never look in progress forever."""
        rows = await self._all("""UPDATE queries SET status='failed', error='Server stopped during query',
             ended_at=now() WHERE status='running' RETURNING id""")
        return len(rows)
