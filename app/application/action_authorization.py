"""Stable bindings between a confirmed preview and its eventual business action."""
from __future__ import annotations

import hashlib
import json


def action_authorization(
    action: str, arguments: dict, *, buyer_id: str, session_id: str,
) -> dict:
    """Bind the action and owner to a UTF-8 canonical JSON argument digest."""
    canonical = json.dumps(
        arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return {
        "action": action,
        "buyer_id": buyer_id,
        "session_id": session_id,
        "arguments_sha256": hashlib.sha256(canonical).hexdigest(),
    }
