"""Validate authored scenarios against a frozen catalog before any live calls."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import yaml


def load_suite(path: Path, catalog: dict, *, split: str = "all") -> list[dict]:
    from scripts.eval_shopping import _hash

    if split not in ("all", "regression", "development", "acceptance"):
        raise ValueError("Unknown suite split")
    manifest = path.with_suffix(".manifest.json")
    if manifest.is_file():
        frozen = json.loads(manifest.read_text(encoding="utf-8"))
        if frozen["suite_sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError("Frozen suite hash mismatch")
        if frozen["catalog_sha256"] != _hash(catalog):
            raise ValueError("Frozen suite catalog hash mismatch")
    cases = yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Task IDs must be nonempty and unique")
    for case in cases:
        if case.get("split", "regression") not in ("regression", "development", "acceptance"):
            raise ValueError("Unknown task split")
        if not case["turns"] or not case["id"] or not case["category"]:
            raise ValueError("Task requires ID, category and turns")
        for turn in case["turns"]:
            expectation = turn["expectation"]
            if not turn["query"] or expectation["mode"] not in (
                "recommend",
                "no_match",
                "clarify",
                "ack",
            ):
                raise ValueError("Invalid turn query or expectation")
            eligible = expectation.get("eligible_ids", [])
            if len(eligible) != len(set(eligible)) or not set(eligible) <= catalog.keys():
                raise ValueError("Duplicate or unknown eligible product")
            if "oracle" in turn:
                rule = turn["oracle"]
                if not set(rule["candidate_ids"]) <= catalog.keys():
                    raise ValueError("Unknown oracle candidate")
                computed = {
                    pid
                    for pid in rule["candidate_ids"]
                    if catalog[pid]["currency"] == rule["currency"]
                    and Decimal(catalog[pid]["price"]) <= Decimal(rule["max_price"])
                    and rule["country"] in catalog[pid]["ships_to"]
                }
                if computed != set(eligible):
                    raise ValueError(f"Task {case['id']} oracle disagrees with eligible IDs")
                if (expectation["mode"] == "no_match") != (not computed):
                    raise ValueError("Task mode disagrees with oracle")
            if expectation["mode"] == "recommend" and not eligible:
                raise ValueError("Recommendation requires eligible products")
    selected = [
        case for case in cases if split == "all" or case.get("split", "regression") == split
    ]
    if not selected:
        raise ValueError("Empty task split")
    return selected
