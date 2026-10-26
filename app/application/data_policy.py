"""Deterministic sink policies for known credentials and common personal-data forms.

This is a bounded data filter, not a semantic prompt-injection classifier. Unknown,
unlabelled names/addresses and encoded secrets need upstream controls as well.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

_MASK = "[REDACTED]"
TRACE_TEXT_FIELDS = frozenset({"query", "reply", "matched_query", "reply_preview", "token"})
_PRIVATE_FIELDS = frozenset({
    "authorization", "proxyauthorization", "cookie", "setcookie", "apikey", "secret",
    "clientsecret", "password", "passwd", "credential", "credentials", "accesstoken",
    "refreshtoken", "authtoken", "phone", "phonenumber", "mobile", "telephone", "email",
    "address", "shippingaddress", "billingaddress", "recipient", "recipientname",
    "receiver", "receivername", "buyername", "fullname", "contactname", "收货人", "姓名",
    "手机号", "电话", "地址",
})
_SHIPPING_FIELDS = frozenset({"shipping", "shippinginfo", "shippingaddress", "delivery", "recipient"})
_PATTERNS = (
    re.compile(r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\w)\+\d[\d ()-]{6,20}\d(?!\d)"),
    re.compile(r"[\u4e00-\u9fff]{2,}(?:省|市)[\u4e00-\u9fff\d]{2,}(?:路|街|巷)\d+号"),
    re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|tvly-[A-Za-z0-9_-]{8,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(
        r"(?<![\w])(?:(?:[A-Za-z][A-Za-z0-9]*[_-])+)?"
        r"(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|auth[_ -]?token|token|secret|"
        r"credentials?|password|passwd|client[_ -]?secret|authorization|cookie|set-cookie)"
        r"[\"']?\s*[:=：]\s*[\"']?[^\s,;，；\"'\}\]]+", re.I,
    ),
    re.compile(r"(?:收货人|收件人|姓名|联系人|地址|收货地址)\s*[:=：]\s*[^\n,;，；]+"),
    re.compile(
        r"\b(?:recipient(?:[_ ]name)?|receiver(?:[_ ]name)?|full[_ ]name|"
        r"shipping[_ ]address|billing[_ ]address|address)\s*[:=：]\s*[^\n,;，；]+", re.I,
    ),
)


def _field_name(key: str) -> str:
    return re.sub(r"[_\s-]", "", key).casefold()


def text_fingerprint(text: str) -> dict:
    """Keep trace correlation without storing the source text."""
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], "length": len(text)}


@dataclass(frozen=True)
class DataPolicy:
    """Inject resolved secret values; this class never reads environment files."""

    secret_values: tuple[str, ...] = field(default_factory=tuple, repr=False)

    def sanitize_output(self, text: str) -> str:
        """Mask known literals and recognisable credential/PII patterns."""
        safe = text
        # Longest first so a configured prefix cannot expose a longer credential suffix.
        for secret in sorted({str(v) for v in self.secret_values if v}, key=len, reverse=True):
            safe = safe.replace(secret, _MASK)
        for pattern in _PATTERNS:
            safe = pattern.sub(_MASK, safe)
        return safe

    def contains_sensitive(self, text: str) -> bool:
        """External searches are denied rather than partially forwarded on a match."""
        return self.sanitize_output(text) != text

    def redact_trace(self, value: Any, *, _shipping: bool = False) -> Any:
        """Copy recursively; hash text fields and sensitive values at the trace sink."""
        if isinstance(value, dict):
            copied = {}
            for key, item in value.items():
                name = _field_name(key) if isinstance(key, str) else ""
                if (
                    (isinstance(key, str) and key.casefold() in TRACE_TEXT_FIELDS)
                    or name in _PRIVATE_FIELDS
                    or name.endswith(("apikey", "password", "secret", "accesstoken", "refreshtoken", "privatekey"))
                    or (_shipping and name == "name")
                ):
                    raw = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, default=str)
                    copied[key] = text_fingerprint(raw)
                else:
                    copied[key] = self.redact_trace(item, _shipping=_shipping or name in _SHIPPING_FIELDS)
            return copied
        if isinstance(value, (list, tuple)):
            return [self.redact_trace(item, _shipping=_shipping) for item in value]
        if isinstance(value, str) and self.contains_sensitive(value):
            return text_fingerprint(value)
        return value
