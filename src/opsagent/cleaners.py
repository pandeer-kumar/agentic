"""Turns large, nested system payloads into small, flat, redacted rows."""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<![\w.])\+?\d[\d ()-]{8,}\d(?![\w.])")
SECRET = re.compile(
    r"(?i)\b(bearer\s+[\w.~+/=-]{8,}|(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+)"
)
TAG = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)
SPACE = re.compile(r"\s+")
NOISE_KEY = re.compile(r"(^|_|\.)(url|uri|href|self|avatar\w*|icon\w*|links?|html\w*|thumbnail)($|_|\.)", re.I)
TIME_KEY = re.compile(r"(^|_)(created|updated|resolved|time|timestamp|date)(_at)?$|_at$", re.I)
MAX_GENERIC_COLUMNS = 30


@dataclass
class CleanResult:
    records: list[dict[str, Any]]
    time_columns: list[str]
    raw_records: int
    duplicates_removed: int
    raw_bytes: int
    clean_bytes: int
    profile: str


def get_path(value: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def redact(text: str) -> str:
    text = SECRET.sub("[SECRET]", text)
    text = EMAIL.sub("[EMAIL]", text)
    return PHONE.sub("[PHONE]", text)


def rich_text(value: Any) -> str:
    """Extract text from strings, HTML, and Atlassian document format."""
    if value is None:
        return ""
    if isinstance(value, str):
        return SPACE.sub(" ", html.unescape(TAG.sub(" ", value))).strip()
    if isinstance(value, dict):
        own = value.get("text") if isinstance(value.get("text"), str) else ""
        return " ".join(p for p in [own, rich_text(value.get("content"))] if p).strip()
    if isinstance(value, list):
        return " ".join(p for p in map(rich_text, value) if p)
    return str(value)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def to_utc(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = value / 1000 if value > 1e11 else value
            parsed = datetime.fromtimestamp(seconds, timezone.utc)
        else:
            text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", str(value).replace("Z", "+00:00"))
            parsed = datetime.fromisoformat(text)
            parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def scalar(value: Any, limit: int) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return truncate(redact(value), limit)
    return truncate(redact(json.dumps(value, default=str)), limit)


def find_records(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    candidates = [v for v in payload.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
    if candidates:
        return [r for r in max(candidates, key=len) if isinstance(r, dict)]
    for value in payload.values():
        if isinstance(value, dict) and (found := find_records(value)):
            return found
    return [payload] if payload else []


def flatten(record: dict, prefix: str = "", depth: int = 3) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if NOISE_KEY.search(str(key)):
            continue
        if isinstance(value, dict) and depth > 1:
            out.update(flatten(value, f"{name}_", depth - 1))
        elif isinstance(value, list):
            if value and all(isinstance(v, (str, int, float)) for v in value):
                out[name] = ", ".join(map(str, value[:10]))
            elif value:
                out[f"{name}_count"] = len(value)
        elif not isinstance(value, dict) and value not in (None, ""):
            out[name] = value
    return out


class CleanerRegistry:
    def __init__(self, path: Path):
        config = yaml.safe_load(path.read_text()) or {}
        self.max_text = int((config.get("defaults") or {}).get("max_text_chars", 600))
        self.systems: dict[str, dict] = config.get("systems") or {}

    def pagination(self, system: str) -> dict | None:
        return self.systems.get(system, {}).get("pagination")

    def extract(self, system: str, payload: Any) -> list[dict]:
        profile = self.systems.get(system)
        if profile and isinstance(payload, dict):
            found = get_path(payload, profile["records"])
            if isinstance(found, list):
                return [r for r in found if isinstance(r, dict)]
        return find_records(payload)

    def clean(self, system: str, raw_records: list[dict], raw_bytes: int | None = None) -> CleanResult:
        raw_bytes = raw_bytes if raw_bytes is not None else len(json.dumps(raw_records, default=str))
        profile = self.systems.get(system)
        if profile:
            rows, times = self._clean_profile(profile, raw_records), list(profile.get("times", []))
            id_field = next((k for k, v in profile["fields"].items() if v == profile.get("id")), None)
            name = system
        else:
            rows, times = self._clean_generic(raw_records)
            id_field = next((c for c in ("id", "key", "uuid") if rows and c in rows[0]), None)
            name = "generic"
        unique, removed = self._dedupe(rows, id_field)
        return CleanResult(
            unique, times, len(raw_records), removed, raw_bytes,
            len(json.dumps(unique, default=str)), name,
        )

    def _clean_profile(self, profile: dict, records: list[dict]) -> list[dict]:
        text, times = set(profile.get("text", [])), set(profile.get("times", []))
        rows = []
        for record in records:
            row = {}
            for column, source in profile["fields"].items():
                value = get_path(record, source)
                if column in times:
                    row[column] = to_utc(value)
                elif column in text:
                    row[column] = truncate(redact(rich_text(value)), self.max_text)
                else:
                    row[column] = scalar(value, self.max_text)
            rows.append(row)
        return rows

    def _clean_generic(self, records: list[dict]) -> tuple[list[dict], list[str]]:
        flat = [flatten(r) for r in records]
        counts: dict[str, int] = {}
        for row in flat:
            for key in row:
                counts[key] = counts.get(key, 0) + 1
        columns = sorted(counts, key=lambda k: (-counts[k], k))[:MAX_GENERIC_COLUMNS]
        times = [c for c in columns if TIME_KEY.search(c)]
        rows = []
        for row in flat:
            clean = {}
            for column in columns:
                value = row.get(column)
                if column in times:
                    clean[column] = to_utc(value)
                elif isinstance(value, str):
                    clean[column] = truncate(redact(rich_text(value)), self.max_text)
                else:
                    clean[column] = value
            rows.append(clean)
        return rows, times

    @staticmethod
    def _dedupe(rows: list[dict], id_field: str | None) -> tuple[list[dict], int]:
        seen, unique = set(), []
        for row in rows:
            key = row.get(id_field) if id_field else None
            key = key if key is not None else json.dumps(row, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                unique.append(row)
        return unique, len(rows) - len(unique)
