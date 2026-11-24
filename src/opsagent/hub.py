"""Connects to all configured MCP servers and fetches complete, cleaned data."""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

log = logging.getLogger(__name__)
VAR = re.compile(r"\$\{(\w+)\}")


def expand(value: Any) -> Any:
    if isinstance(value, str):
        return VAR.sub(lambda m: sys.executable if m[1] == "PYTHON" else os.environ.get(m[1], ""), value)
    if isinstance(value, list):
        return [expand(v) for v in value]
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    return value


class ToolError(RuntimeError):
    pass


@dataclass
class Connection:
    name: str
    config: dict
    timeout: float
    client: Any = None
    tools: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    reconnect: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    def target(self) -> Any:
        if self.config.get("transport") == "http":
            client = create_mcp_http_client(headers=self.config.get("headers") or None)
            return streamable_http_client(self.config["url"], http_client=client)
        env = {**os.environ, **(self.config.get("env") or {})}
        return StdioServerParameters(command=self.config["command"], args=self.config.get("args", []), env=env)

    def allowed(self, tool: str) -> bool:
        return any(fnmatch.fnmatch(tool, p) for p in self.config.get("allow", []))

    async def run(self) -> None:
        delay = 1.0
        while not self.stop.is_set():
            try:
                async with Client(self.target(), read_timeout_seconds=self.timeout) as client:
                    tools, cursor = {}, None
                    while True:
                        page = await client.list_tools(cursor=cursor)
                        tools.update({t.name: t for t in page.tools})
                        cursor = page.next_cursor
                        if not cursor:
                            break
                    self.client, self.tools, self.error = client, tools, None
                    self.ready.set()
                    delay = 1.0
                    self.reconnect.clear()
                    stop = asyncio.create_task(self.stop.wait())
                    again = asyncio.create_task(self.reconnect.wait())
                    await asyncio.wait({stop, again}, return_when=asyncio.FIRST_COMPLETED)
                    stop.cancel()
                    again.cancel()
            except Exception as exc:  # A bad server must never take down the others.
                self.error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("MCP server %s failed: %s", self.name, self.error)
            finally:
                self.client = None
                self.ready.set()
            if not self.stop.is_set():
                try:
                    await asyncio.wait_for(self.stop.wait(), delay)
                except TimeoutError:
                    delay = min(delay * 2, 30)

    async def call(self, tool: str, arguments: dict) -> Any:
        if not self.allowed(tool):
            raise ToolError(f"{self.name}.{tool} is blocked by the allowlist")
        if tool not in self.tools:
            raise ToolError(f"{self.name} has no tool '{tool}'. Available: {sorted(self.tools)}")
        if self.client is None:
            raise ToolError(f"{self.name} is not connected: {self.error or 'starting'}")
        try:
            result = await self.client.call_tool(tool, arguments)
        except Exception as exc:
            self.reconnect.set()
            raise ToolError(f"{self.name}.{tool} call failed: {exc}") from exc
        text = "\n".join(b.text for b in result.content if getattr(b, "text", None))
        if result.is_error:
            raise ToolError(f"{self.name}.{tool} returned an error: {text[:500]}")
        if result.structured_content is not None:
            return result.structured_content
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}


class MCPHub:
    def __init__(self, path: Path, cleaners, max_records: int = 5000, timeout: float = 60):
        self.cleaners = cleaners
        self.max_records = max_records
        config = expand(yaml.safe_load(path.read_text()) or {})
        self.connections = {
            name: Connection(name, cfg, timeout) for name, cfg in (config.get("servers") or {}).items()
        }

    async def start(self, wait: float = 30) -> None:
        for connection in self.connections.values():
            connection.task = asyncio.create_task(connection.run(), name=f"mcp-{connection.name}")
        waits = [c.ready.wait() for c in self.connections.values()]
        try:
            await asyncio.wait_for(asyncio.gather(*waits), wait)
        except TimeoutError:
            log.warning("Some MCP servers were not ready after %ss", wait)

    async def close(self) -> None:
        for connection in self.connections.values():
            connection.stop.set()
        tasks = [c.task for c in self.connections.values() if c.task]
        if tasks:
            await asyncio.wait(tasks, timeout=10)

    def status(self) -> list[dict]:
        return [{
            "system": c.name, "connected": c.client is not None, "error": c.error,
            "description": c.config.get("description", ""),
            "tools": sorted(t for t in c.tools if c.allowed(t)),
        } for c in self.connections.values()]

    def find_tools(self, system: str | None = None, keyword: str | None = None) -> list[dict]:
        needle = (keyword or "").lower()
        out = []
        for connection in self.connections.values():
            if system and connection.name != system:
                continue
            for tool in connection.tools.values():
                text = f"{tool.name} {tool.description or ''}".lower()
                if connection.allowed(tool.name) and needle in text:
                    out.append({"system": connection.name, "tool": tool.name,
                                "description": (tool.description or "")[:300],
                                "input_schema": tool.input_schema})
        return out

    def _connection(self, system: str) -> Connection:
        if system not in self.connections:
            raise ToolError(f"Unknown system '{system}'. Known: {sorted(self.connections)}")
        return self.connections[system]

    async def fetch(self, system: str, tool: str, arguments: dict) -> tuple[list[dict], int, int, bool]:
        """Returns raw records, raw bytes, pages fetched, and whether the cap truncated them."""
        connection = self._connection(system)
        paging = self.cleaners.pagination(system)
        schema = (connection.tools[tool].input_schema or {}).get("properties", {}) if tool in connection.tools else {}
        if not paging or paging.get("limit_arg") not in schema:
            payload = await connection.call(tool, arguments)
            records = self.cleaners.extract(system, payload)
            capped = len(records) > self.max_records
            return records[: self.max_records], len(json.dumps(payload, default=str)), 1, capped

        size = int(paging.get("page_size", 100))
        records: list[dict] = []
        raw_bytes = pages = 0
        offset = int(arguments.get(paging.get("offset_arg", ""), 0) or 0)
        page_number = int(arguments.get(paging.get("page_arg", ""), 1) or 1)
        while True:
            args = {**arguments, paging["limit_arg"]: size}
            if paging["style"] == "offset":
                args[paging["offset_arg"]] = offset
            else:
                args[paging["page_arg"]] = page_number
            payload = await connection.call(tool, args)
            batch = self.cleaners.extract(system, payload)
            raw_bytes += len(json.dumps(payload, default=str))
            pages += 1
            records.extend(batch)
            if len(records) >= self.max_records:
                more = len(records) > self.max_records or self._has_more(paging, payload, offset, batch)
                return records[: self.max_records], raw_bytes, pages, more
            if not batch or not self._has_more(paging, payload, offset, batch):
                return records, raw_bytes, pages, False
            offset += len(batch)
            page_number += 1

    @staticmethod
    def _has_more(paging: dict, payload: dict, offset: int, batch: list) -> bool:
        if paging["style"] == "offset":
            return offset + len(batch) < int(payload.get(paging["total"], 0) or 0)
        return bool(payload.get(paging["next"]))
