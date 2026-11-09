"""Compare full/compact judge input sizes offline; characters are not tokens."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def profile_checkpoint(checkpoint: dict, catalog: dict) -> dict:
    from scripts.eval_shopping import _hash, observed_catalog
    from scripts.shopping_grading import judge_prompt

    if checkpoint.get("metadata", {}).get("catalog_sha256") not in (None, _hash(catalog)):
        raise ValueError("Catalog hash mismatch")
    sizes = {mode: {"characters": 0, "utf8_bytes": 0} for mode in ("full", "compact")}
    count, rows = 0, []
    for run in checkpoint["runs"]:
        history = []
        for turn in run["turns"]:
            if turn.get("reply") and not turn.get("error"):
                row = {"task_id": run["task_id"], "route": run["route"], "turn": turn["turn"]}
                for mode in sizes:
                    prompt = judge_prompt(
                        {"query": turn["query"], "history": history},
                        turn["reply"],
                        observed_catalog(catalog, turn.get("tool_events", [])),
                        catalog_mode=mode,
                    )
                    text = "".join(m["content"] for m in prompt)
                    sizes[mode]["characters"] += len(text)
                    sizes[mode]["utf8_bytes"] += len(text.encode())
                    row[mode + "_characters"] = len(text)
                rows.append(row)
                count += 1
            history.extend(
                [
                    {"role": "user", "content": turn["query"]},
                    {"role": "assistant", "content": turn.get("reply", "")},
                ]
            )
    return {
        "model_calls": 0,
        "turns": count,
        **sizes,
        "estimated_tokens": None,
        "estimated_money": None,
        "character_reduction": 1 - sizes["compact"]["characters"] / sizes["full"]["characters"]
        if count
        else None,
        "rows": rows,
        "source_sha256": _hash(checkpoint),
        "scope": "Input text sizes only; compact semantic equivalence has not been validated.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "data/judge-profiles")
    args = parser.parse_args(argv)
    report = profile_checkpoint(
        json.loads(args.checkpoint.read_text(encoding="utf-8")),
        json.loads(args.catalog.read_text(encoding="utf-8")),
    )
    report["implementation_sha256"] = hashlib.sha256(
        (ROOT / "scripts/shopping_grading.py").read_bytes()
    ).hexdigest()
    output = args.output / uuid.uuid4().hex
    output.mkdir(parents=True, exist_ok=False)
    (output / "profile.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "profile.md").write_text(
        "# 裁判输入体积对照\n\n"
        f"历史回答：{report['turns']} 轮；新增模型调用：0。\n\n"
        f"完整输入：{report['full']['characters']} 字符；压缩输入：{report['compact']['characters']} 字符。\n\n"
        f"字符体积变化：{report['character_reduction']:.1%}。\n\n"
        "这是文本体积对照，不是 Token 或费用节省测量。压缩版语义等价性尚待校准。\n",
        encoding="utf-8",
    )
    print(f"Judge input profile: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
