"""Bounded historical data for planning and isolated sub-agent dispatch."""
import json

from app.application.agents.conversation_summary import bounded_excerpt, normalize_summary


def task_context(state: dict, max_chars: int = 12000) -> str:
    """Drop whole records to fit; role/provenance JSON must never be sliced apart."""
    payload = {
        "authority": "historical_data_only",
        "summary": json.loads(normalize_summary(str(state.get("summary") or ""))),
        "messages": [],
    }
    for message in list(state.get("messages") or [])[-8:]:
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        entry = {
            "source_role": str(getattr(message, "type", "message")),
            "text": bounded_excerpt(content, 1400),
        }
        calls = getattr(message, "tool_calls", None)
        if calls:
            entry["tool_calls"] = bounded_excerpt(json.dumps(calls, ensure_ascii=False), 800)
        payload["messages"].append(entry)

    def dump():
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    while len(dump()) > max_chars:
        if len(payload["messages"]) > 1:
            payload["messages"].pop(0)
        elif payload["summary"]["entries"]:
            payload["summary"]["entries"].pop(0)
        elif payload["messages"]:
            entry = payload["messages"][0]
            if "tool_calls" in entry:
                entry.pop("tool_calls")
            elif len(entry["text"]) > 32:
                entry["text"] = bounded_excerpt(entry["text"], len(entry["text"]) // 2)
            else:
                payload["messages"].clear()
        else:
            return "{}" if max_chars >= 2 else ""
    return dump()
