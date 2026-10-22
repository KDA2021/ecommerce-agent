# -*- coding: utf-8 -*-
"""把 `docs/diagrams/*.mmd` 渲染成同名 PNG/SVG（调用 Kroki 在线服务）。

用途：核心代码讲解里的 7 张流程图直接嵌入 PNG；源码保留为 .mmd。
修改 Mermaid 后运行：

    uv run --no-sync python scripts/render_mermaid.py

需要可访问 https://kroki.io/；渲染失败会重试 6 次。
"""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DIAGRAMS = ROOT / "docs" / "diagrams"
KROKI_URL = "https://kroki.io/"
RETRIES = 6


def post_kroki(source: str, fmt: str, scale: int | None = None) -> bytes:
    """调用 Kroki 把 Mermaid 渲染为指定格式，带简单重试。

    Args:
        source: Mermaid 源码。
        fmt: `png` 或 `svg`。
        scale: 可选缩放倍数（PNG 用 2 提升清晰度）。

    Returns:
        图片二进制内容。

    Raises:
        httpx.HTTPError: 重试耗尽后仍失败。
    """
    url = KROKI_URL + (f"?scale={scale}" if scale else "")
    last: Exception | None = None
    for _ in range(RETRIES):
        try:
            resp = httpx.post(
                url,
                json={
                    "diagram_source": source,
                    "diagram_type": "mermaid",
                    "output_format": fmt,
                },
                timeout=120.0,
            )
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as exc:
            last = exc
            time.sleep(3)
    raise last  # type: ignore[misc]


def png_size(data: bytes) -> tuple[int, int]:
    """读取 PNG 宽高（IHDR 前 8 字节）。"""
    return struct.unpack(">II", data[16:24])


def main() -> None:
    """渲染目录下所有 .mmd 文件。"""
    files = sorted(DIAGRAMS.glob("*.mmd"))
    if not files:
        print(f"no .mmd files under {DIAGRAMS}")
        return
    for mmd in files:
        source = mmd.read_text(encoding="utf-8")
        png = mmd.with_suffix(".png")
        svg = mmd.with_suffix(".svg")
        svg.write_bytes(post_kroki(source, "svg"))
        png.write_bytes(post_kroki(source, "png", scale=2))
        width, height = png_size(png.read_bytes())
        print(f"{mmd.name}: {width}x{height} png={png.stat().st_size}B")
    print(f"rendered {len(files)} diagrams")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        print(f"render failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
