# -*- coding: utf-8 -*-
"""Latency report：解析 ``data/traces/*.jsonl`` 里的 span 行并输出延迟分位数。

用法::

    uv run --no-sync python scripts/latency_report.py
    uv run --no-sync python scripts/latency_report.py --trace-dir data/traces
    uv run --no-sync python scripts/latency_report.py --json
    uv run --no-sync python scripts/latency_report.py --out data/latency-report.md

只读本地 JSONL；事件 trace 行（``type != "span"``）与坏行会被跳过。
默认同时把 Markdown 报告写到 ``data/latency-report.md``（``--out`` 可覆盖路径）。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Iterable, Optional, Sequence

DEFAULT_TRACE_DIR = Path(__file__).resolve().parents[1] / "data" / "traces"


def percentile(values: Sequence[float], p: float) -> float:
    """计算线性插值分位数；空样本返回 0.0。

    Args:
        values: 数值序列。
        p: 百分位，0–100。

    Returns:
        分位数值。
    """
    data = sorted(float(value) for value in values)
    if not data:
        return 0.0
    if not 0 <= p <= 100:
        raise ValueError("p 必须在 0 到 100 之间")
    if len(data) == 1:
        return data[0]
    rank = (len(data) - 1) * (p / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return data[low]
    return data[low] + (data[high] - data[low]) * (rank - low)


def _paths_from(source) -> list[Path]:
    """把目录 / 文件 / glob / 路径列表归一成 JSONL 文件列表。"""
    if source is None:
        return []
    if isinstance(source, (str, Path)):
        raw = str(source)
        path = Path(raw)
        if path.is_dir():
            return sorted(path.glob("*.jsonl"))
        if any(ch in raw for ch in "*?["):
            parent = path.parent if str(path.parent) else Path(".")
            return sorted(parent.glob(path.name))
        if path.suffix == ".jsonl":
            return [path]
        return []
    return [Path(item) for item in source]


def load_spans(source=DEFAULT_TRACE_DIR) -> list[dict]:
    """读取 trace JSONL 中的 span 行；目录、glob 或文件列表都支持。

    Args:
        source: trace 目录 / JSONL 路径 / glob / 路径列表。

    Returns:
        span dict 列表；非法 JSON 或非 span 行自动跳过。
    """
    spans: list[dict] = []
    for path in _paths_from(source):
        if not path.is_file():
            continue
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw_line in raw_lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") != "span":
                continue
            spans.append(record)
    return spans


def _duration_ms(span: dict) -> Optional[float]:
    """取数值型 duration_ms；非法/缺失返回 None。"""
    value = span.get("duration_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _stats(durations: Sequence[float], errors: int) -> dict:
    """把一组 duration 汇总成 count/avg/p50/p95/p99/errors。"""
    count = len(durations)
    if count == 0:
        return {
            "count": 0,
            "avg": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "errors": errors,
        }
    return {
        "count": count,
        "avg": round(sum(durations) / count, 3),
        "p50": round(percentile(durations, 50), 3),
        "p95": round(percentile(durations, 95), 3),
        "p99": round(percentile(durations, 99), 3),
        "errors": errors,
    }


def aggregate_spans(spans: Iterable[dict]) -> dict:
    """聚合 span：request/ttft 分位数 + 按 name 的 count/avg/p95/errors。

    Args:
        spans: span dict 序列。

    Returns:
        汇总 dict；``by_name`` 为各 span 名称统计。
    """
    ordered = [span for span in spans if isinstance(span, dict)]
    by_name_durations: dict[str, list[float]] = {}
    by_name_errors: dict[str, int] = {}

    for span in ordered:
        duration = _duration_ms(span)
        if duration is None:
            continue
        name = str(span.get("name") or "")
        by_name_durations.setdefault(name, []).append(duration)
        if span.get("status") == "error":
            by_name_errors[name] = by_name_errors.get(name, 0) + 1

    by_name = {
        name: _stats(durations, by_name_errors.get(name, 0))
        for name, durations in by_name_durations.items()
    }

    def _for(name: str) -> dict:
        """按 span 名称取统计；缺失时返回全 0，保证报告字段齐全。"""
        durations = by_name_durations.get(name, [])
        errors = by_name_errors.get(name, 0)
        return _stats(durations, errors)

    total = sum(stats["count"] for stats in by_name.values())
    return {
        "count": total,
        "request": _for("request"),
        "ttft": _for("ttft"),
        "by_name": by_name,
        # 兼容不同调用方对 per-span 字段的称呼。
        "spans": by_name,
        "per_span": by_name,
    }


def format_report(data) -> str:
    """把 spans 或 aggregate_spans 结果渲染成人类可读文本。

    Args:
        data: span 列表或 ``aggregate_spans`` 结果。

    Returns:
        纯文本报告。
    """
    summary = data
    if not (isinstance(data, dict) and "by_name" in data):
        summary = aggregate_spans(data or [])

    request = summary.get("request", {})
    ttft = summary.get("ttft", {})
    lines = [
        "Latency Report",
        "==============",
        f"spans: {summary.get('count', 0)}",
        "Percentiles: P50 / P95 / P99 (p50 / p95 / p99)",
        "",
        "Request total (ms)",
    ]

    def _render(label: str, stats: dict) -> str:
        """把一组统计渲染成单行文本。"""
        return (
            f"{label}: count={stats.get('count', 0)} avg={stats.get('avg', 0.0)} "
            f"p50={stats.get('p50', 0.0)} p95={stats.get('p95', 0.0)} "
            f"p99={stats.get('p99', 0.0)} errors={stats.get('errors', 0)}"
        )

    lines.append(_render("  request", request))
    lines.append("TTFT (ms)")
    lines.append(_render("  ttft", ttft))
    lines.append("")
    lines.append("Per-span breakdown (ms)")
    lines.append(f"{'name':<24} {'count':>6} {'avg':>10} {'p95':>10} {'errors':>7}")
    for name, stats in sorted(summary.get("by_name", {}).items()):
        lines.append(
            f"{name:<24} {stats.get('count', 0):>6} {stats.get('avg', 0.0):>10} "
            f"{stats.get('p95', 0.0):>10} {stats.get('errors', 0):>7}"
        )
    if not summary.get("by_name"):
        lines.append("(no spans found)")
    return "\n".join(lines)


# 常用别名，便于脚本/测试按不同命名调用。
summarize = aggregate_spans
aggregate = aggregate_spans

DEFAULT_REPORT_PATH = Path(__file__).resolve().parents[1] / "data" / "latency-report.md"


def format_markdown_report(data) -> str:
    """把 spans 或 aggregate_spans 结果渲染成 Markdown 报告。

    Args:
        data: span 列表或 ``aggregate_spans`` 结果。

    Returns:
        Markdown 文本，可写入 ``data/latency-report.md``。
    """
    summary = data
    if not (isinstance(data, dict) and "by_name" in data):
        summary = aggregate_spans(data or [])

    rows = [
        "| name | count | avg(ms) | p50(ms) | p95(ms) | p99(ms) | errors |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]

    def _row(label: str, stats: dict) -> str:
        """把一组统计渲染成 Markdown 表格行。"""
        return (
            f"| {label} | {stats.get('count', 0)} | {stats.get('avg', 0.0)} | "
            f"{stats.get('p50', 0.0)} | {stats.get('p95', 0.0)} | "
            f"{stats.get('p99', 0.0)} | {stats.get('errors', 0)} |"
        )

    lines = [
        "# Latency Report",
        "",
        f"- spans: {summary.get('count', 0)}",
        "- source: `data/traces/*.jsonl`（仅 `type=span` 行）",
        "",
        "## 总耗时 / TTFT",
        "",
        *rows,
        _row("request", summary.get("request", {})),
        _row("ttft", summary.get("ttft", {})),
        "",
        "## 按 span 名称",
        "",
        *rows,
    ]
    by_name = summary.get("by_name", {})
    for name, stats in sorted(by_name.items()):
        lines.append(_row(name, stats))
    if not by_name:
        lines.append("| (no spans found) | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0 |")
    lines.append("")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        description="Read local span JSONL files and print latency percentiles.",
    )
    parser.add_argument(
        "trace_dir_pos",
        nargs="?",
        default=None,
        help="trace 目录 / JSONL 文件 / glob；缺省 data/traces。",
    )
    parser.add_argument(
        "--trace-dir",
        dest="trace_dir",
        default=None,
        help="trace 目录 / JSONL 文件 / glob；优先于位置参数。",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="stdout 输出机器可读的 JSON 汇总（默认输出文本报告）。",
    )
    parser.add_argument(
        "--out",
        dest="out",
        default=None,
        help=(
            "Markdown 报告输出路径；缺省 data/latency-report.md。"
            "传 -（或 --no-report）表示不写文件。"
        ),
    )
    parser.add_argument(
        "--no-report",
        dest="no_report",
        action="store_true",
        help="只打印报告，不写 Markdown 文件。",
    )
    return parser


def main(argv=None) -> int:
    """命令行入口：读取 trace 并输出报告，默认落盘 Markdown 报告。

    Args:
        argv: 可选命令行参数；缺省取 ``sys.argv``。

    Returns:
        进程退出码，正常为 0。
    """
    args = _build_parser().parse_args(argv)
    source = args.trace_dir or args.trace_dir_pos or DEFAULT_TRACE_DIR
    spans = load_spans(source)
    summary = aggregate_spans(spans)
    if args.as_json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(format_report(summary))

    if not args.no_report and args.out != "-":
        out_path = Path(args.out) if args.out else DEFAULT_REPORT_PATH
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            format_markdown_report(summary) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
