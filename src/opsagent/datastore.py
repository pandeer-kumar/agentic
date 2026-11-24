"""Per-session analytical store. The LLM sees profiles and query results, never full payloads."""
from __future__ import annotations

import asyncio
import re
import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

NAME = re.compile(r"^[a-z][a-z0-9_]{0,50}$")
MAX_RESULT_ROWS = 200
MAX_CHART_ROWS = 2000


class DataError(ValueError):
    pass


def plain(value: Any) -> Any:
    if value is None or (isinstance(value, float) and value != value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "item"):
        return plain(value.item())
    return value


class SessionStore:
    def __init__(self, path: Path, timeout: float):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.lock = threading.Lock()
        self.conn = duckdb.connect(str(path), config={"threads": "2", "memory_limit": "1GB"})
        self.conn.execute("SET enable_external_access=false")
        self.conn.execute("SET lock_configuration=true")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS _catalog(name VARCHAR PRIMARY KEY, system VARCHAR, query_id VARCHAR, "
            "rows BIGINT, created TIMESTAMP DEFAULT current_timestamp)"
        )

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def _next_name(self, base: str) -> str:
        base = re.sub(r"[^a-z0-9_]", "_", base.lower()).strip("_")[:40] or "data"
        base = base if base[0].isalpha() else f"d_{base}"
        count = self.conn.execute("SELECT count(*) FROM _catalog WHERE name = ? OR name LIKE ?",
                                  [base, f"{base}_%"]).fetchall()[0][0]
        return f"{base}_{count + 1}"

    def add(self, system: str, query_id: str, records: list[dict], time_columns: list[str]) -> dict:
        with self.lock:
            name = self._next_name(system)
            if not records:
                return {"dataset": None, "rows": 0, "columns": []}
            frame = pd.DataFrame(records).astype(object).where(lambda f: f.notna(), None)
            for column in time_columns:
                if column in frame:
                    frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce").dt.tz_localize(None)
            frame = frame.dropna(axis=1, how="all").infer_objects()
            self.conn.register("_incoming", frame)
            try:
                self.conn.execute(f'CREATE TABLE "{name}" AS SELECT * FROM _incoming')
            finally:
                self.conn.unregister("_incoming")
            self.conn.execute("INSERT INTO _catalog(name, system, query_id, rows) VALUES (?, ?, ?, ?)",
                              [name, system, query_id, len(frame)])
            return self._profile(name)

    def _profile(self, name: str) -> dict:
        columns = self.conn.execute(f'DESCRIBE "{name}"').fetchall()
        rows = self.conn.execute(f'SELECT count(*) FROM "{name}"').fetchall()[0][0]
        info = []
        for column, kind, *_ in columns:
            quoted = '"' + column.replace('"', '""') + '"'
            distinct, nulls = self.conn.execute(
                f'SELECT count(DISTINCT {quoted}), count(*) - count({quoted}) FROM "{name}"').fetchall()[0]
            entry: dict[str, Any] = {"name": column, "type": kind, "distinct": distinct, "nulls": nulls}
            if kind.startswith("TIMESTAMP"):
                low, high = self.conn.execute(f'SELECT min({quoted}), max({quoted}) FROM "{name}"').fetchall()[0]
                entry["range"] = [plain(low), plain(high)]
            elif 0 < distinct <= 12:
                top = self.conn.execute(
                    f'SELECT {quoted}, count(*) c FROM "{name}" GROUP BY 1 ORDER BY c DESC, 1 LIMIT 12').fetchall()
                entry["top"] = {str(k): v for k, v in top}
            info.append(entry)
        sample = self.conn.execute(f'SELECT * FROM "{name}" LIMIT 3')
        headers = [d[0] for d in sample.description]
        samples = [{h: (str(plain(v))[:120] if isinstance(v, str) else plain(v)) for h, v in zip(headers, row)}
                   for row in sample.fetchall()]
        return {"dataset": name, "rows": rows, "columns": info, "sample": samples}

    def catalog(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT name, system, rows FROM _catalog ORDER BY created").fetchall()
            out = []
            for name, system, count in rows:
                columns = [c[0] for c in self.conn.execute(f'DESCRIBE "{name}"').fetchall()]
                out.append({"dataset": name, "system": system, "rows": count, "columns": columns})
            return out

    @staticmethod
    def _select(sql: str) -> str:
        try:
            statements = duckdb.extract_statements(sql)
        except duckdb.Error as exc:
            raise DataError(f"Invalid SQL: {exc}") from exc
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise DataError("Only one read-only SELECT statement is allowed")
        return statements[0].query.strip().rstrip(";")

    def _run(self, sql: str, limit: int, save_as: str | None, query_id: str) -> dict:
        query = self._select(sql)
        if save_as and not NAME.match(save_as):
            raise DataError("save_as must be lowercase letters, digits and underscores")
        with self.lock:
            cursor = self.conn
            timer = threading.Timer(self.timeout, cursor.interrupt)
            timer.start()
            try:
                if save_as:
                    exists = self.conn.execute("SELECT count(*) FROM _catalog WHERE name = ?", [save_as]).fetchall()[0][0]
                    if exists:
                        raise DataError(f"Dataset {save_as} already exists")
                    self.conn.execute(f'CREATE TABLE "{save_as}" AS {query}')
                    count = self.conn.execute(f'SELECT count(*) FROM "{save_as}"').fetchall()[0][0]
                    self.conn.execute("INSERT INTO _catalog(name, system, query_id, rows) VALUES (?, 'derived', ?, ?)",
                                      [save_as, query_id, count])
                    query = f'SELECT * FROM "{save_as}"'
                result = self.conn.execute(f"SELECT * FROM ({query}) LIMIT {limit + 1}")
                headers = [d[0] for d in result.description]
                rows = [[plain(v) for v in row] for row in result.fetchall()]
            except duckdb.InterruptException as exc:
                raise DataError(f"Query exceeded {self.timeout}s") from exc
            except duckdb.Error as exc:
                raise DataError(str(exc)[:600]) from exc
            finally:
                timer.cancel()
        return {"columns": headers, "rows": rows[:limit], "truncated": len(rows) > limit, "saved_as": save_as}

    async def query(self, sql: str, save_as: str | None = None, query_id: str = "",
                    limit: int = MAX_RESULT_ROWS) -> dict:
        return await asyncio.to_thread(self._run, sql, limit, save_as, query_id)

    async def add_async(self, system: str, query_id: str, records: list[dict], times: list[str]) -> dict:
        return await asyncio.to_thread(self.add, system, query_id, records, times)


class DataStore:
    def __init__(self, directory: Path, timeout: float = 20):
        self.directory = directory
        self.timeout = timeout
        self.sessions: dict[str, SessionStore] = {}
        self.lock = threading.Lock()

    def session(self, session_id: str) -> SessionStore:
        with self.lock:
            if session_id not in self.sessions:
                self.sessions[session_id] = SessionStore(self.directory / f"{session_id}.duckdb", self.timeout)
            return self.sessions[session_id]

    def close(self) -> None:
        with self.lock:
            for store in self.sessions.values():
                store.close()
            self.sessions.clear()


def build_chart(result: dict, chart_type: str, x: str, y: str, series: str | None, title: str) -> dict:
    """Builds a renderer-neutral chart. Series beyond eight fold into Other."""
    if chart_type not in {"bar", "hbar", "stacked_bar", "line", "area", "scatter"}:
        raise DataError("chart_type must be bar, hbar, stacked_bar, line, area or scatter")
    columns = result["columns"]
    missing = [c for c in (x, y, series) if c and c not in columns]
    if missing:
        raise DataError(f"Columns {missing} not in result {columns}")
    rows = [dict(zip(columns, row)) for row in result["rows"]]
    if not rows:
        raise DataError("Query returned no rows to chart")
    if any(r[y] is not None and not isinstance(r[y], (int, float)) for r in rows):
        raise DataError(f"y column '{y}' must be numeric")
    groups: dict[str, dict] = {}
    if series:
        totals: dict[str, float] = {}
        for row in rows:
            key = str(row[series])
            totals[key] = totals.get(key, 0) + (row[y] or 0)
        keep = set(sorted(totals, key=lambda k: (-totals[k], k))[:7 if len(totals) > 8 else 8])
    for row in rows:
        name = (str(row[series]) if str(row[series]) in keep else "Other") if series else y
        points = groups.setdefault(name, {})
        points[row[x]] = points.get(row[x], 0) + (row[y] or 0)
    names = sorted(n for n in groups if n != "Other") + (["Other"] if "Other" in groups else [])
    return {
        "type": chart_type, "title": title, "x_label": x, "y_label": y,
        "series": [{"name": n, "slot": i, "x": list(groups[n]), "y": list(groups[n].values())}
                   for i, n in enumerate(names)],
        "table": {"columns": columns, "rows": result["rows"][:200]},
    }
