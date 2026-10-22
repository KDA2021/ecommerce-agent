# -*- coding: utf-8 -*-
"""P5 评测回归：真实 Agent + LLM 裁判（0/1/2 分）。

设计要点：
1. 直接 ``build_container(load_settings())`` 组装真实容器（MySQL + Chroma + 真实 Agent）；
2. **不走** ``app.presentation.server`` 路径，而是对每条用例用*全新 thread* 直接调
   ``invoke_agent``，因此语义缓存天然不参与（缓存只在 server 的 submit_intent 里查询/回写）；
3. 仍然显式把 ``container.semantic_cache`` 置为 ``None``，避免任何隐式缓存读取；
4. 每条用例调一次 LLM 裁判，裁判同样由 ``build_chat_model`` 构造的 ChatOpenAI 担任，
   不依赖任何外部评测服务；
5. 汇总写 ``data/eval-report.md``，并在标准输出打印总分。

用法::

    uv run python scripts/eval_regression.py            # 全量
    uv run python scripts/eval_regression.py --limit 2  # 只跑前 2 条（冒烟）
    uv run python scripts/eval_regression.py --with-badcases  # 合并 data/badcases.jsonl

注意：本模块顶层的纯函数（``parse_judge_response`` / ``load_cases`` / ``render_report``）
不导入任何重依赖，方便在单测里直接加载，不会触碰真实 LLM。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 让 ``from app...`` 在「直接执行脚本」与「pytest 按路径导入」两种方式下都可用。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:  # pragma: no cover - 仅在缺依赖时给出清晰报错
    import yaml
except ModuleNotFoundError as err:  # pragma: no cover
    raise SystemExit("缺少 PyYAML，请先执行 uv sync") from err

CASES_PATH = ROOT / "eval" / "cases.yaml"
BADCASES_PATH = ROOT / "data" / "badcases.jsonl"
REPORT_NAME = "eval-report.md"
REQUIRED_FIELDS = ("id", "kind", "query", "buyer_id", "must_contain", "must_not_contain")
KNOWN_KINDS = ("search", "rag", "preference", "order", "general")

JUDGE_PROMPT = """你是电商导购 Agent 的严格评测裁判。
你会看到【用户问题】【必须覆盖的要点】【禁止出现的内容】【可信商品库】和【待评回答】。

请只依据待评回答本身打分，不要脑补回答里没有的信息：
- 2 分：回答了用户问题，覆盖全部必须要点，没有出现任何禁止内容，也没有编造（不虚构商品、价格、订单号、政策）。
- 1 分：部分覆盖必须要点，或方向基本正确但缺少关键信息，且没有出现禁止内容。
- 0 分：答非所问、完全没有覆盖必须要点、出现任何禁止内容，或存在明显编造。
判定编造时以【可信商品库】为准：引用其中真实存在的商品、SKU、价格、库存不算编造；
声称库里不存在的商品有货、编造订单号或政策，才算编造。
若【必须覆盖的要点】与【禁止出现的内容】均为空，按“用户问题是否被正确、如实地回答”判断。

只输出一行，格式严格为：
SCORE: <0|1|2> 理由：<不超过 30 字的一句话>"""

# 裁判输出解析：优先显式标注，其次裸分数，最后兼容 JSON。
_SCORE_PATTERNS = (
    re.compile(r"(?:SCORE|score|得分|评分|分数|打分)\s*[:：]?\s*(-?\d+)"),
    re.compile(r"[\"']?score[\"']?\s*[:：]\s*(-?\d+)", re.IGNORECASE),
)
_BARE_SCORE = re.compile(r"^\s*([0-2])\s*分?\s*$")
_REASON_PATTERN = re.compile(r"理由\s*[:：]\s*(.+)$", re.DOTALL)
# 订单号形态，用于把回答里的订单号回数据库核验（区分真实下单与编造）。
ORDER_ID_PATTERN = re.compile(r"GBX-\d{6}")


def parse_judge_response(text: Any) -> int | None:
    """把裁判输出解析成 0/1/2；无法解析或分数越界时返回 None。

    Args:
        text: 裁判原始输出。

    Returns:
        0 / 1 / 2；解析失败返回 None。
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    for pattern in _SCORE_PATTERNS:
        match = pattern.search(raw)
        if match:
            value = int(match.group(1))
            return value if value in (0, 1, 2) else None
    match = _BARE_SCORE.match(raw)
    if match:
        return int(match.group(1))
    return None


def load_cases(path: Path | str = CASES_PATH) -> list[dict]:
    """读取并校验用例集；格式不合法直接抛 ValueError。

    Args:
        path: 用例 YAML 路径。

    Returns:
        校验后的用例列表。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"用例文件不存在：{path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        raise ValueError(f"用例文件格式错误（顶层需为含 cases 列表的映射）：{path}")
    cases: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw["cases"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 条用例不是映射：{item!r}")
        for field in REQUIRED_FIELDS:
            if field not in item:
                raise ValueError(f"第 {index} 条用例缺少字段 {field}")
        for field in ("must_contain", "must_not_contain"):
            value = item[field]
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ValueError(f"第 {index} 条用例字段 {field} 必须是字符串列表")
        if not str(item["id"]).strip():
            raise ValueError(f"第 {index} 条用例 id 为空")
        if item["id"] in seen:
            raise ValueError(f"用例 id 重复：{item['id']}")
        seen.add(item["id"])
        cases.append(item)
    if not cases:
        raise ValueError(f"用例文件没有任何用例：{path}")
    return cases


def load_badcases(path: Path | str = BADCASES_PATH) -> list[dict]:
    """读取 ``data/badcases.jsonl`` 并转换成评测用例。

    每一行是前端点踩时写入的 JSON；这里只取 query / buyer_id 等能安全构造
    评测用例的字段。空行、坏行、缺 query 的行直接跳过，不让一条脏数据打断
    整轮回归。文件不存在时返回空列表（首次跑评测还没点踩样本是正常情况）。

    Args:
        path: badcases JSONL 路径。

    Returns:
        评测用例 dict 列表（附加 ``source="badcase"`` 标记）。
    """
    path = Path(path)
    if not path.exists():
        return []
    cases: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        query = str(item.get("query", "") or "").strip()
        if not query:
            continue
        raw_id = str(item.get("id") or f"line-{index}").strip() or f"line-{index}"
        case_id = raw_id if raw_id.startswith("badcase-") else f"badcase-{raw_id}"
        base, suffix = case_id, 2
        while case_id in seen:
            case_id = f"{base}-{suffix}"
            suffix += 1
        seen.add(case_id)
        cases.append(
            {
                "id": case_id,
                "kind": str(item.get("kind") or "general"),
                "query": query,
                "buyer_id": str(item.get("buyer_id") or "badcase-buyer"),
                "must_contain": [],
                "must_not_contain": [],
                "source": "badcase",
                "reason": str(item.get("reason") or ""),
                "reply_preview": str(item.get("reply_preview") or ""),
            }
        )
    return cases


def merge_cases(cases: list[dict], badcases: list[dict]) -> list[dict]:
    """把 badcase 用例合并进 YAML 用例；id 撞车时加后缀而非丢弃。

    Args:
        cases: ``load_cases`` 读出的 YAML 用例。
        badcases: ``load_badcases`` 读出的 badcase 用例。

    Returns:
        合并去重后的用例列表（YAML 在前，badcase 追加在后）。
    """
    merged = list(cases)
    seen = {str(case.get("id")) for case in cases}
    for case in badcases:
        candidate = dict(case)
        case_id = str(candidate.get("id"))
        if case_id in seen:
            base, suffix = case_id, 2
            while f"{base}-{suffix}" in seen:
                suffix += 1
            case_id = f"{base}-{suffix}"
            candidate["id"] = case_id
        seen.add(case_id)
        merged.append(candidate)
    return merged


def _bullets(items: list[str]) -> str:
    """把字符串列表渲染成 Markdown 无序列表。

    Args:
        items: 字符串列表。

    Returns:
        Markdown 文本。
    """
    return "\n".join(f"- {item}" for item in items) if items else "（无）"


def render_catalog_facts(products: list) -> str:
    """把商品库渲染成裁判可用的「可信事实」，用于区分真实引用与编造。

    覆盖标题/品类/产地/可发国家/描述/亮点/SKU 价格库存，让裁判能核对
    「420g 重量」「产地越南」这类真实属性，而不是把它们误判为编造。

    Args:
        products: 商品列表。

    Returns:
        注入裁判 prompt 的「可信商品库」文本。
    """
    lines: list[str] = []
    for product in products:
        ships = "/".join(getattr(product, "ships_to", None) or [])
        lines.append(
            f"- {getattr(product, 'title', '')}"
            f"（{getattr(product, 'product_id', '')}｜{getattr(product, 'category', '')}"
            f"｜产地{getattr(product, 'origin_country', '')}｜可发 {ships or '未知'}）"
        )
        description = str(getattr(product, "description", "") or "").strip()
        if description:
            lines.append(f"  描述：{description}")
        highlights = getattr(product, "highlights", None) or []
        rendered = [
            f"{getattr(h, 'label', '')}={getattr(h, 'detail', '')}".strip("=") for h in highlights
        ]
        if rendered:
            lines.append("  亮点：" + "；".join(rendered))
        skus = []
        for sku in getattr(product, "skus", None) or []:
            money = getattr(sku, "price", None)
            price = "价格未知"
            if money is not None:
                try:
                    price = (
                        f"{float(money.to_major_units()):g} {getattr(money, 'currency', '')}"
                    ).strip()
                except (AttributeError, TypeError, ValueError):
                    price = "价格未知"
            skus.append(
                f"{getattr(sku, 'sku_id', '')} {getattr(sku, 'spec', '')} {price}"
                f"（库存 {getattr(sku, 'stock', '?')}）"
            )
        if skus:
            lines.append("  SKU：" + "；".join(skus))
    return "\n".join(lines) if lines else "（商品库为空）"


def render_order_facts(verified: list[str], fabricated: list[str], error: str = "") -> str:
    """订单号核验结果：真实存在 / 库中不存在（供裁判判定是否编造订单）。

    Args:
        verified: 真实存在的订单号列表。
        fabricated: 库中不存在的订单号列表。
        error: 订单核验异常信息。

    Returns:
        订单号核验事实文本。
    """
    facts = (
        f"真实存在的订单号：{verified or '无'}；"
        f"回答中出现但库中不存在的订单号：{fabricated or '无'}"
    )
    if error:
        facts += f"（核验异常：{error}）"
    return facts


def _cell(text: Any) -> str:
    """清洗表格单元格文本（转义竖线、压平换行）。

    Args:
        text: 原始文本。

    Returns:
        单元格文本。
    """
    flat = str(text or "").replace("|", "／").replace("\n", " ").strip()
    return flat or "—"


def _message_text(message: Any) -> str:
    """把 LLM 消息 content（字符串或 block 列表）归一化成纯文本。

    Args:
        message: LLM 消息或 content。

    Returns:
        纯文本。
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(getattr(block, "text", "")))
        return "".join(parts)
    return str(content)


def _extract_reason(text: str) -> str:
    """从裁判输出里截取「理由」部分。

    Args:
        text: 裁判原始输出。

    Returns:
        理由文本。
    """
    if not text:
        return ""
    match = _REASON_PATTERN.search(text)
    if match:
        return match.group(1).strip()[:160]
    return text.strip().splitlines()[-1][:160]


def render_report(settings: Any, results: list[dict]) -> str:
    """把逐条结果渲染成 Markdown 报告（纯函数，便于单测）。

    Args:
        settings: 运行时配置。
        results: 逐条评测结果。

    Returns:
        Markdown 报告文本。
    """
    total = sum(int(item["score"]) for item in results)
    max_total = 2 * len(results)
    badcase_count = sum(1 for item in results if item.get("source") == "badcase")
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    lines = [
        "# P5 评测回归报告",
        "",
        f"- 生成时间：{stamp}",
        f"- 被测模型：{getattr(settings, 'llm_model', '')}",
        f"- 用例数：{len(results)}",
        f"- 其中 badcase 回流：{badcase_count}",
        f"- 总分：{total} / {max_total}",
        "- 缓存：已禁用（不走 server 路径，逐条新 thread 直连 invoke_agent）",
        "",
        "## 汇总",
        "",
        "说明：`字面校验` 只是确定性参考（同义表述会漏判），最终以 LLM 裁判的语义评分为准。",
        "",
        "| id | kind | 得分 | 字面校验 | 裁判理由 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in results:
        score = str(item["score"]) + ("（解析失败按 0 计）" if item["parse_error"] else "")
        literal = "通过"
        if item["missing"] or item["forbidden"]:
            literal = f"缺={item['missing']} 违禁={item['forbidden']}"
        lines.append(
            f"| {item['id']} | {item['kind']} | {score} | {_cell(literal)} | "
            f"{_cell(item['reason'])} |"
        )
    lines += ["", "## 逐条明细", ""]
    for item in results:
        lines += [
            f"### {item['id']}（{item['kind']}，{item['score']}/2）",
            "",
            f"- buyer_id：`{item['buyer_id']}`",
            f"- query：{item['query']}",
            f"- must_contain：{item['must_contain']}",
            f"- must_not_contain：{item['must_not_contain']}",
            f"- 字面未命中要点：{item['missing'] or '无'}",
            f"- 字面命中禁止内容：{item['forbidden'] or '无'}",
            f"- 裁判解析失败：{'是' if item['parse_error'] else '否'}",
            f"- 裁判原始输出：{_cell(item['judge_raw'])}",
            f"- 该买家当前偏好：{item.get('preferences') or '无'}",
            f"- 订单号核验：{item.get('order_facts') or '无'}",
        ]
        if item.get("error"):
            lines.append(f"- 执行异常：{_cell(item['error'])}")
        lines += [
            "",
            "回答：",
            "",
            "```text",
            (item["reply"] or "（空回答）").strip(),
            "```",
            "",
        ]
    return "\n".join(lines) + "\n"


async def judge_case(
    judge: Any,
    case: dict,
    reply: str,
    missing: list,
    forbidden: list,
    catalog_facts: str = "",
    order_facts: str = "",
) -> str:
    """调用 LLM 裁判并返回其原始文本输出。

    Args:
        judge: 裁判 LLM。
        case: 用例 dict。
        reply: 待评回答。
        missing: 字面未命中的要点。
        forbidden: 字面命中的禁止内容。
        catalog_facts: 可信商品库文本。
        order_facts: 订单号核验事实文本。

    Returns:
        裁判原始输出文本。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    # 只有订单类用例（或回答里确实出现订单号）才给裁判订单核验信息，避免干扰其它场景。
    order_block = ""
    if order_facts or case.get("kind") == "order":
        order_block = (
            f"【订单号核验（执行本用例后查询真实数据库得到）】\n"
            f"{order_facts or '（未提供）'}\n"
            f"若回答声称下单成功，订单号必须出现在「真实存在的订单号」中，否则视为编造。\n\n"
        )

    payload = (
        f"【用户问题】\n{case['query']}\n\n"
        f"【必须覆盖的要点】\n{_bullets(list(case['must_contain']))}\n\n"
        f"【禁止出现的内容】\n{_bullets(list(case['must_not_contain']))}\n\n"
        f"【可信商品库（真实存在，引用其中商品/SKU/价格/库存/属性不算编造）】\n"
        f"{catalog_facts or '（未提供）'}\n\n"
        f"{order_block}"
        f"【确定性字面校验（仅供参考，同义表述也算覆盖）】\n"
        f"未命中要点：{missing or '无'}；命中禁止内容：{forbidden or '无'}\n\n"
        f"【待评回答】\n{reply or '（空回答）'}\n\n"
        "请只输出一行：SCORE: <0|1|2> 理由：<一句话>"
    )
    response = await judge.ainvoke(
        [SystemMessage(content=JUDGE_PROMPT), HumanMessage(content=payload)]
    )
    return _message_text(response)


async def run_case(
    container: Any, judge: Any, case: dict, run_tag: str, catalog_facts: str = ""
) -> dict:
    """单条用例：全新 thread 调 invoke_agent → LLM 裁判打分。

    Args:
        container: 装配好的 Container。
        judge: 裁判 LLM。
        case: 用例 dict。
        run_tag: 本轮评测的唯一 tag，用于隔离 thread。
        catalog_facts: 可信商品库文本。

    Returns:
        单条评测结果 dict。
    """
    from app.application.agents.main_graph import invoke_agent
    from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot

    session_id = f"eval-{case['id']}-{run_tag}"
    reply = ""
    error = ""
    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id=session_id,
            buyer_id=case["buyer_id"],
            locale="zh-CN",
            currency="CNY",
        )
    )
    try:
        reply = await invoke_agent(container.agent, session_id, case["query"])
    except Exception as err:  # noqa: BLE001 - 单条用例失败不应中断整轮评测
        error = f"{type(err).__name__}: {err}"
        reply = f"[用例执行异常] {error}"
    finally:
        ShoppingContext.reset(token)

    missing = [item for item in case["must_contain"] if item not in (reply or "")]
    forbidden = [item for item in case["must_not_contain"] if item in (reply or "")]

    # 把回答里出现的订单号回数据库核验：真实存在的才算下单成功，否则视为编造。
    # 只在订单类用例或回答确实提到订单号时才生成，避免干扰其它场景的裁判。
    verified: list[str] = []
    fabricated: list[str] = []
    order_error = ""
    mentions = list(dict.fromkeys(ORDER_ID_PATTERN.findall(reply or "")))
    order_facts = ""
    if mentions or case.get("kind") == "order":
        for order_id in mentions:
            try:
                order = await container.order_repo.find_by_id(order_id)
            except Exception as err:  # noqa: BLE001
                order_error = f"{type(err).__name__}: {err}"
                fabricated.append(order_id)
                continue
            if order is None:
                fabricated.append(order_id)
            else:
                verified.append(order_id)
        order_facts = render_order_facts(verified, fabricated, order_error)

    try:
        judge_raw = await judge_case(
            judge, case, reply, missing, forbidden, catalog_facts, order_facts
        )
    except Exception as err:  # noqa: BLE001 - 裁判失败降级为 0 分并记录
        judge_raw = f"[裁判调用失败] {type(err).__name__}: {err}"

    score = parse_judge_response(judge_raw)
    parse_error = score is None
    if parse_error:
        # 解析失败按 0 分处理，报告里用 parse_error 单独标注。
        score = 0

    try:
        stored = await container.preference_store.list_by_buyer(case["buyer_id"])
        preferences = [f"[{p.kind}] {p.statement}" for p in stored]
    except Exception as err:  # noqa: BLE001
        preferences = [f"<读取失败 {type(err).__name__}>"]

    return {
        "id": case["id"],
        "kind": case["kind"],
        "source": case.get("source", "yaml"),
        "buyer_id": case["buyer_id"],
        "query": case["query"],
        "must_contain": case["must_contain"],
        "must_not_contain": case["must_not_contain"],
        "reply": reply,
        "error": error,
        "missing": missing,
        "forbidden": forbidden,
        "score": score,
        "parse_error": parse_error,
        "judge_raw": judge_raw,
        "reason": _extract_reason(judge_raw),
        "preferences": preferences,
        "order_facts": order_facts,
    }


async def run_eval(
    cases_path: Path | str = CASES_PATH,
    limit: int | None = None,
    report_path: Path | str | None = None,
    with_badcases: Path | str | None = None,
) -> int:
    """执行整轮评测，返回进程退出码。

    Args:
        cases_path: 用例 YAML 路径。
        limit: 只跑前 N 条；None 表示全量。
        report_path: 报告输出路径；None 使用默认 data/eval-report.md。
        with_badcases: 非 None 时把该路径（或默认 data/badcases.jsonl）的
            点踩样本合并进评测用例。

    Returns:
        进程退出码，正常为 0。
    """
    from app.bootstrap import build_container
    from app.infrastructure.llm import build_chat_model
    from app.infrastructure.settings import load_settings

    cases = load_cases(cases_path)
    if with_badcases is not None:
        badcase_path = Path(with_badcases) if str(with_badcases) else BADCASES_PATH
        badcase_cases = load_badcases(badcase_path)
        cases = merge_cases(cases, badcase_cases)
        if badcase_cases:
            print(f"[eval] 合并 {len(badcase_cases)} 条 badcase -> {badcase_path}", flush=True)
        else:
            print(f"[eval] 未读取到 badcase（{badcase_path}）", flush=True)
    if limit is not None:
        # 负数按 0 处理，避免切片语义意外取到倒数几条。
        cases = cases[: max(limit, 0)]

    settings = load_settings()
    judge = build_chat_model(settings)
    container = await build_container(settings)
    # 本脚本不走 server 路径，这里再显式禁用一次语义缓存，避免任何隐式读取。
    container.semantic_cache = None  # type: ignore[assignment]
    run_tag = uuid.uuid4().hex[:8]

    try:
        catalog_facts = render_catalog_facts(await container.product_repo.list_all())
    except Exception as err:  # noqa: BLE001 - 商品库快照失败不阻断评测
        catalog_facts = f"（商品库读取失败：{type(err).__name__}）"

    results: list[dict] = []
    try:
        for case in cases:
            print(f"[eval] {case['id']} ...", flush=True)
            result = await run_case(container, judge, case, run_tag, catalog_facts)
            results.append(result)
            print(
                f"[eval] {case['id']} -> {result['score']}/2"
                + ("（解析失败）" if result["parse_error"] else ""),
                flush=True,
            )
    finally:
        await container.close()

    report = render_report(settings, results)
    target = Path(report_path) if report_path else Path(settings.data_dir) / REPORT_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report, encoding="utf-8")

    total = sum(int(item["score"]) for item in results)
    max_total = 2 * len(results)
    print(f"总分：{total} / {max_total}（{len(results)} 条用例）")
    print(f"报告已写入：{target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口：解析参数并执行整轮评测。

    Args:
        argv: 可选参数列表。

    Returns:
        进程退出码。
    """
    parser = argparse.ArgumentParser(description="P5 LLM-judge 评测回归")
    parser.add_argument("--cases", default=str(CASES_PATH), help="用例 YAML 路径")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟用）")
    parser.add_argument("--report", default=None, help="报告输出路径，默认 data/eval-report.md")
    parser.add_argument(
        "--with-badcases",
        nargs="?",
        const=str(BADCASES_PATH),
        default=None,
        help="合并点踩 badcase 进评测；不带值用 data/badcases.jsonl，也可传自定义路径",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        run_eval(
            cases_path=Path(args.cases),
            limit=args.limit,
            report_path=Path(args.report) if args.report else None,
            with_badcases=args.with_badcases,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
