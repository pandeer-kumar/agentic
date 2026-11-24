"""Maps a user's words to systems, boards and fetch recipes using a markdown file."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

FIELD = re.compile(r"^-\s*(\w+):\s*(.+)$")
FETCH = re.compile(r"^(\w+)\.(\w+)\s+(\{.*\})$")
WORD = re.compile(r"[a-z0-9-]+")
STOP = {"the", "in", "of", "for", "last", "past", "find", "out", "show", "me", "what", "any", "and", "all",
        "to", "a", "an", "is", "are", "by", "from", "hour", "hours", "day", "days", "week", "kind", "create",
        "report", "graph", "chart", "with", "on", "at", "user", "users", "issue", "issues", "change", "changes"}


@dataclass
class Intent:
    name: str
    aliases: list[str] = field(default_factory=list)
    systems: list[str] = field(default_factory=list)
    boards: list[str] = field(default_factory=list)
    fetch: list[str] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    notes: str = ""


def parse_hours(query: str) -> int:
    text = query.lower()
    if match := re.search(r"(\d+)\s*(h|hr|hrs|hour|hours|d|day|days|w|week|weeks)\b", text):
        unit = match[2][0]
        return min(int(match[1]) * {"h": 1, "d": 24, "w": 168}[unit], 8760) or 24
    if "yesterday" in text:
        return 48
    if re.search(r"\b(this|last|past) week\b", text):
        return 168
    if re.search(r"\b(this|last|past) month\b", text):
        return 720
    return 24


class KnowledgeBase:
    def __init__(self, path: Path, systems: list[str]):
        self.systems = systems
        self.intents = self._parse(path.read_text())

    @staticmethod
    def _parse(text: str) -> list[Intent]:
        intents: list[Intent] = []
        current = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("## "):
                current = Intent(line[3:].strip())
                intents.append(current)
            elif current and (match := FIELD.match(line)):
                key, value = match[1], match[2].strip()
                if key == "fetch":
                    current.fetch.append(value)
                elif key == "notes":
                    current.notes = value
                elif key in ("aliases", "systems", "boards", "group_by"):
                    setattr(current, key, [v.strip().lower() if key != "boards" else v.strip()
                                           for v in value.split(",") if v.strip()])
        return intents

    def _score(self, intent: Intent, query: str, tokens: set[str]) -> int:
        score = 0
        for phrase in [intent.name, *intent.aliases]:
            if re.search(rf"\b{re.escape(phrase.lower())}\b", query):
                score = max(score, 3 * len(phrase.split()))
        names = set(WORD.findall(" ".join([intent.name, *intent.aliases]).lower())) - STOP
        return score + len(tokens & names)

    @staticmethod
    def _recipes(intent: Intent, hours: int, only: set[str]) -> list[dict]:
        recipes = []
        for recipe in intent.fetch:
            if not (match := FETCH.match(recipe.replace("{hours}", str(hours)))):
                continue
            if only and match[1] not in only:
                continue
            recipes.append({"system": match[1], "tool": match[2], "arguments": json.loads(match[3])})
        return recipes

    def resolve(self, query: str, limit: int = 3) -> dict:
        text = query.lower()
        tokens = set(WORD.findall(text)) - STOP
        hours = parse_hours(query)
        explicit = [s for s in self.systems if re.search(rf"\b{re.escape(s)}\b", text)]
        scored = sorted(((self._score(i, text, tokens), i) for i in self.intents),
                        key=lambda pair: pair[0], reverse=True)
        matches = []
        for score, intent in scored:
            if score <= 0 or len(matches) >= limit:
                continue
            if explicit and not set(explicit) & set(intent.systems):
                continue
            matches.append({
                "intent": intent.name, "score": score, "systems": intent.systems, "boards": intent.boards,
                "fetch": self._recipes(intent, hours, set(explicit)),
                "group_by": intent.group_by, "notes": intent.notes,
            })
        return {
            "time_window_hours": hours, "explicit_systems": explicit, "matches": matches,
            "guidance": "Use the best match. If none fits, choose systems from the catalog and call find_tools.",
        }
