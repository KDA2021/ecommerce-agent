"""Bounded, source-attributed historical data stored in the existing string channel.

Entries are quotations, including any instructions or claims in those quotations.
Neither a source role nor exact quotation establishes authorization or tool truth.
"""
from __future__ import annotations

import hashlib
import json
import re

MAX_SUMMARY_CHARS = 6000
MAX_ENTRIES = 12
MAX_QUOTE_CHARS = 400
SOURCE_ROLES = frozenset({"human", "ai", "tool", "legacy"})
_NUMBER = re.compile(r"[+-]?\d+(?:[.,]\d+)*(?:[eE][+-]?\d+)?")


def bounded_excerpt(text: str, limit: int = MAX_QUOTE_CHARS) -> str:
    """Cut an excerpt without turning a partial amount/order number into a new one."""
    end = min(limit, len(text))
    for number in _NUMBER.finditer(text):
        if number.start() < end < number.end():
            end = number.start()
            break
    return text[:end]


def is_source_quote(text: str, source: str) -> bool:
    """Require an exact occurrence whose boundaries do not split a numeric token."""
    numbers = list(_NUMBER.finditer(source))
    start = source.find(text)
    while start >= 0:
        end = start + len(text)
        if not any(number.start() < boundary < number.end()
                   for number in numbers for boundary in (start, end)):
            return True
        start = source.find(text, start + 1)
    return False


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def strict_json_loads(text: str):
    """Reject duplicate keys rather than silently accepting overwritten fields."""
    return json.loads(text, object_pairs_hook=_unique_object)


def _dump(entries: list[dict]) -> str:
    return json.dumps(
        {"version": 2, "authority": "historical_data_only", "entries": entries},
        ensure_ascii=False, separators=(",", ":"),
    )


def serialize_summary(entries: list[dict]) -> str:
    """Keep recent distinct excerpts within both entry and serialized-size budgets."""
    bounded = []
    seen = set()
    for entry in reversed(entries):
        key = (entry["source_id"], entry["source_role"], bounded_excerpt(entry["text"]))
        if key in seen or not key[2]:
            continue
        seen.add(key)
        bounded.append({"source_id": key[0], "source_role": key[1], "text": key[2]})
        if len(bounded) == MAX_ENTRIES:
            break
    bounded.reverse()
    while bounded and len(_dump(bounded)) > MAX_SUMMARY_CHARS:
        bounded.pop(0)
    return _dump(bounded)


def summary_entries(summary: str) -> list[dict]:
    """Read v2 history; old or unsupported strings retain only legacy provenance."""
    if not isinstance(summary, str) or not summary:
        return []
    try:
        if len(summary) > MAX_SUMMARY_CHARS * 10:
            raise ValueError("Oversized historical summary")
        parsed = strict_json_loads(summary)
        if not isinstance(parsed, dict) or set(parsed) != {"version", "authority", "entries"}:
            raise ValueError("Unsupported summary schema")
        if type(parsed["version"]) is not int or parsed["version"] != 2:
            raise ValueError("Unsupported summary version")
        if parsed["authority"] != "historical_data_only" or not isinstance(parsed["entries"], list):
            raise ValueError("Unsupported historical authority")
        for entry in parsed["entries"]:
            if not isinstance(entry, dict) or set(entry) != {"source_id", "source_role", "text"}:
                raise ValueError("Invalid historical entry")
            if not isinstance(entry["source_id"], str) or not 0 < len(entry["source_id"]) <= 200:
                raise ValueError("Invalid historical source")
            if not isinstance(entry["source_role"], str) or entry["source_role"] not in SOURCE_ROLES:
                raise ValueError("Invalid historical role")
            if not isinstance(entry["text"], str) or not entry["text"]:
                raise ValueError("Invalid historical text")
        return parsed["entries"]
    except (ValueError, TypeError, RecursionError):
        return [{"source_id": "legacy:" + hashlib.sha256(summary.encode()).hexdigest()[:24],
                 "source_role": "legacy", "text": bounded_excerpt(summary)}]


def normalize_summary(summary: str) -> str:
    """Return v2 JSON without ever treating historical claims as current authority."""
    return serialize_summary(summary_entries(summary))
