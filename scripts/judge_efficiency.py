"""Small, local cost controls for evidence extraction, independent of Agent state."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace


def project_catalog(catalog: dict, task: dict, reply: str) -> list[dict]:
    """Keep a complete identity/price index and full facts for any mentioned item.

    Shared brands intentionally expand to all family members. Matching is generous:
    false matches cost context, whereas false negatives can hide required evidence.
    This experimental projection must be calibrated before replacing full context.
    """
    context = json.dumps(
        {"query": task.get("query"), "history": task.get("history"), "reply": reply},
        ensure_ascii=False,
    ).casefold()
    index_fields = {
        "product_id",
        "title",
        "brand",
        "price",
        "currency",
        "aliases",
        "sku_prices",
        "observed_prices",
    }
    result = []
    for pid, item in catalog.items():
        names = [pid, item["title"], item["brand"], *item.get("aliases", [])]
        mentioned = any(name and name.casefold() in context for name in names)
        projected = (
            dict(item) if mentioned else {k: v for k, v in item.items() if k in index_fields}
        )
        projected["product_id"] = pid
        result.append(copy.deepcopy(projected))
    return result


class CachedJudge:
    """Cache valid raw extraction, never final grades or persistence checks.

    The complete request plus non-secret model configuration form the key.
    Valid failing extractions are cached too. Errors/invalid schemas are not.
    """

    def __init__(self, judge, directory: Path | None, model_identity: dict):
        self.judge = judge
        self.directory = directory
        self.model_identity = copy.deepcopy(model_identity)
        self.hits = self.misses = self.corrupt = self.write_errors = 0

    def stats(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "corrupt": self.corrupt,
            "write_errors": self.write_errors,
        }

    async def ainvoke(self, prompt, **kwargs):
        from scripts.shopping_grading import parse_extraction

        request = json.dumps(
            {"schema": 1, "messages": prompt, "model": self.model_identity},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        key = hashlib.sha256(request.encode()).hexdigest()
        path = self.directory / f"{key}.json" if self.directory is not None else None
        if path is not None and path.is_file():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
                raw = stored["raw"]
                if (
                    stored["key"] != key
                    or stored["schema"] != 1
                    or stored["raw_sha256"] != hashlib.sha256(raw.encode()).hexdigest()
                ):
                    raise ValueError("Cache integrity mismatch")
                parse_extraction(raw)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self.corrupt += 1
            else:
                self.hits += 1
                return SimpleNamespace(content=raw)
        self.misses += 1
        response = await self.judge.ainvoke(prompt, **kwargs)
        raw = response.content
        try:
            parse_extraction(raw)
        except (ValueError, TypeError):
            return response
        if path is not None:
            temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(
                    json.dumps(
                        {
                            "schema": 1,
                            "key": key,
                            "raw": raw,
                            "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                os.replace(temporary, path)
            except OSError:
                self.write_errors += 1
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return response
