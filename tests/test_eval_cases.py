# -*- coding: utf-8 -*-
"""P5 评测回归的静态校验单测：用例集格式 + 裁判输出解析。

全部为离线测试，不调用真实 LLM、不连数据库。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "eval" / "cases.yaml"
SCRIPT_PATH = ROOT / "scripts" / "eval_regression.py"

REQUIRED_FIELDS = ("id", "kind", "query", "buyer_id", "must_contain", "must_not_contain")
EXPECTED_KINDS = {"search", "rag", "preference", "order", "general"}


def _load_eval_module():
    """用文件路径加载 scripts/eval_regression.py（scripts 不是包）。"""
    spec = importlib.util.spec_from_file_location("eval_regression", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def eval_module():
    return _load_eval_module()


@pytest.fixture(scope="module")
def raw_cases() -> list[dict]:
    data = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "cases.yaml 顶层必须是映射"
    return data["cases"]


# --------------------------------------------------------------------------- #
# 用例集校验
# --------------------------------------------------------------------------- #
def test_cases_file_exists():
    assert CASES_PATH.is_file(), "缺少 eval/cases.yaml"


def test_at_least_ten_cases(raw_cases):
    assert len(raw_cases) >= 10, f"评测用例不足 10 条：{len(raw_cases)}"


def test_all_required_fields_present(raw_cases):
    for case in raw_cases:
        missing = [field for field in REQUIRED_FIELDS if field not in case]
        assert not missing, f"用例 {case.get('id')!r} 缺少字段：{missing}"


def test_field_types_and_values(raw_cases):
    for case in raw_cases:
        assert isinstance(case["id"], str) and case["id"].strip(), f"id 非法：{case!r}"
        assert isinstance(case["query"], str) and case["query"].strip()
        assert isinstance(case["buyer_id"], str) and case["buyer_id"].strip()
        assert isinstance(case["kind"], str)
        for field in ("must_contain", "must_not_contain"):
            assert isinstance(case[field], list), f"{case['id']} 的 {field} 必须是列表"
            assert all(isinstance(item, str) for item in case[field])


def test_case_ids_are_unique(raw_cases):
    ids = [case["id"] for case in raw_cases]
    assert len(ids) == len(set(ids)), f"存在重复用例 id：{ids}"


def test_kinds_cover_required_scenarios(raw_cases):
    kinds = {case["kind"] for case in raw_cases}
    assert EXPECTED_KINDS <= kinds, f"缺少场景：{EXPECTED_KINDS - kinds}"


def test_loader_returns_same_ids(eval_module, raw_cases):
    loaded = eval_module.load_cases(CASES_PATH)
    assert [case["id"] for case in loaded] == [case["id"] for case in raw_cases]
    assert eval_module.KNOWN_KINDS and set(eval_module.KNOWN_KINDS) == EXPECTED_KINDS


def test_preference_cases_share_buyer_for_apply(raw_cases):
    """偏好写入与偏好应用必须共用 buyer_id，否则跨 thread 记忆无法验证。"""
    by_id = {case["id"]: case for case in raw_cases}
    assert (
        by_id["preference-like-lightweight"]["buyer_id"]
        == by_id["preference-apply-stored"]["buyer_id"]
    )


# --------------------------------------------------------------------------- #
# load_cases 负例
# --------------------------------------------------------------------------- #
def test_loader_rejects_malformed_yaml(tmp_path):
    bad = tmp_path / "cases.yaml"
    bad.write_text("cases: not-a-list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        _load_eval_module().load_cases(bad)


def test_loader_rejects_missing_field(tmp_path):
    bad = tmp_path / "cases.yaml"
    bad.write_text(
        "cases:\n  - id: x\n    kind: general\n    query: hi\n    buyer_id: b\n"
        "    must_contain: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must_not_contain"):
        _load_eval_module().load_cases(bad)


def test_loader_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        _load_eval_module().load_cases(tmp_path / "nope.yaml")


# --------------------------------------------------------------------------- #
# 裁判输出解析
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SCORE: 2\n理由：覆盖全部要点且无编造", 2),
        ("SCORE: 1 理由：只覆盖了一部分", 1),
        ("SCORE: 0 理由：答非所问", 0),
        ("score: 2", 2),
        ("得分：1", 1),
        ("评分: 0", 0),
        ("分数：2", 2),
        ("打分:1", 1),
        ('{"score": 1}', 1),
        ("2", 2),
        ("0", 0),
        ("1 分", 1),
        ("  2  ", 2),
        # 无法解析 / 越界
        ("", None),
        (None, None),
        ("   ", None),
        ("SCORE: 3 理由：自造档位", None),
        ("SCORE: -1 理由：负数", None),
        ("我无法判断这条回答", None),
        ("SCORE: abc", None),
    ],
)
def test_parse_judge_response(eval_module, text, expected):
    assert eval_module.parse_judge_response(text) == expected


def test_parse_judge_response_handles_message_like_text(eval_module):
    assert eval_module.parse_judge_response("SCORE: 2 理由：多行\n第二行忽略") == 2


def test_judge_prompt_states_scoring_rubric(eval_module):
    prompt = eval_module.JUDGE_PROMPT
    for token in ("SCORE", "2 分", "1 分", "0 分"):
        assert token in prompt, f"裁判提示词缺少：{token}"


# --------------------------------------------------------------------------- #
# 可信商品库渲染（供裁判区分真实引用与编造）
# --------------------------------------------------------------------------- #
def test_render_catalog_facts(eval_module):
    products = [
        SimpleNamespace(
            title="Nomadica 旅行三件套",
            product_id="P1001",
            category="旅行装备",
            origin_country="VN",
            ships_to=["CN", "US"],
            description="帆布加尼龙材质 轻便",
            highlights=[SimpleNamespace(label="重量", detail="全套 420g 轻便")],
            skus=[
                SimpleNamespace(
                    sku_id="P1001-S1",
                    spec="军绿色",
                    price=SimpleNamespace(to_major_units=lambda: 189.0, currency="CNY"),
                    stock=15,
                )
            ],
        )
    ]
    facts = eval_module.render_catalog_facts(products)
    for token in (
        "Nomadica 旅行三件套",
        "P1001",
        "旅行装备",
        "VN",
        "CN/US",
        "帆布加尼龙材质",
        "420g",
        "P1001-S1",
        "189",
        "CNY",
        "15",
    ):
        assert token in facts, f"商品库事实缺少：{token}"


def test_render_catalog_facts_handles_empty(eval_module):
    assert eval_module.render_catalog_facts([]) == "（商品库为空）"


# --------------------------------------------------------------------------- #
# 订单号核验事实渲染
# --------------------------------------------------------------------------- #
def test_order_id_pattern_extracts_order_numbers(eval_module):
    found = eval_module.ORDER_ID_PATTERN.findall("订单 GBX-000043 已创建；再次确认 GBX-000043")
    assert found == ["GBX-000043", "GBX-000043"]
    assert eval_module.ORDER_ID_PATTERN.findall("没有订单号") == []


def test_render_order_facts(eval_module):
    facts = eval_module.render_order_facts(["GBX-000043"], [])
    assert "GBX-000043" in facts and "无" in facts
    fabricated = eval_module.render_order_facts([], ["GBX-999999"])
    assert "GBX-999999" in fabricated
    assert "核验异常" in eval_module.render_order_facts([], [], "OperationalError: x")


# --------------------------------------------------------------------------- #
# 裁判 prompt 组装（用假 Chat 模型，不调真实 LLM）
# --------------------------------------------------------------------------- #
class _FakeChat:
    def __init__(self):
        self.calls: list = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content="SCORE: 2 理由：ok")


def _case(**overrides) -> dict:
    case = {
        "id": "c1",
        "kind": "search",
        "query": "帮我找旅行三件套",
        "buyer_id": "b1",
        "must_contain": ["旅行三件套"],
        "must_not_contain": ["编造"],
    }
    case.update(overrides)
    return case


async def test_judge_case_payload_carries_case_and_catalog(eval_module):
    fake = _FakeChat()
    await eval_module.judge_case(fake, _case(), "回答正文", [], [], "CATALOG-FACT", "")
    payload = fake.calls[0][1].content
    assert "帮我找旅行三件套" in payload
    assert "旅行三件套" in payload and "编造" in payload
    assert "CATALOG-FACT" in payload
    assert "回答正文" in payload
    # 非订单场景（无订单号）不注入订单核验块
    assert "订单号核验" not in payload


async def test_judge_case_injects_order_block_for_order_kind(eval_module):
    fake = _FakeChat()
    case = _case(kind="order", must_contain=["GBX"], must_not_contain=[])
    await eval_module.judge_case(
        fake, case, "订单 GBX-000001 已创建", [], [], "", "真实存在的订单号：['GBX-000001']"
    )
    payload = fake.calls[0][1].content
    assert "订单号核验" in payload and "GBX-000001" in payload


async def test_judge_case_result_is_parseable(eval_module):
    raw = await eval_module.judge_case(_FakeChat(), _case(), "答案", [], [], "", "")
    assert eval_module.parse_judge_response(raw) == 2


# --------------------------------------------------------------------------- #
# 报告渲染（不调 LLM）
# --------------------------------------------------------------------------- #
def _fake_result(**overrides) -> dict:
    result = {
        "id": "case-1",
        "kind": "search",
        "buyer_id": "b1",
        "query": "旅行三件套",
        "must_contain": ["旅行三件套"],
        "must_not_contain": [],
        "reply": "推荐 Nomadica 旅行三件套",
        "error": "",
        "missing": [],
        "forbidden": [],
        "score": 2,
        "parse_error": False,
        "judge_raw": "SCORE: 2 理由：完整覆盖",
        "reason": "完整覆盖",
        "preferences": [],
        "order_facts": "真实存在的订单号：无；回答中出现但库中不存在的订单号：无",
    }
    result.update(overrides)
    return result


def test_render_report_includes_total(eval_module):
    settings = SimpleNamespace(llm_model="fake-model", llm_base_url="http://localhost")
    report = eval_module.render_report(settings, [_fake_result(), _fake_result(score=0, id="case-2")])
    assert "# P5 评测回归报告" in report
    assert "总分：2 / 4" in report
    assert "case-1" in report and "case-2" in report
    assert "fake-model" in report


def test_render_report_flags_parse_error(eval_module):
    settings = SimpleNamespace(llm_model="m", llm_base_url="u")
    report = eval_module.render_report(
        settings,
        [_fake_result(score=0, parse_error=True, judge_raw="看不懂", reason="")],
    )
    assert "解析失败" in report
