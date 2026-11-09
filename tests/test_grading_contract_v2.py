"""Offline price provenance and narrow structural evidence controls."""
import pytest

from scripts.shopping_grading import evaluate_turn


def catalog():
    return {"P1": {"title": "TrailOx 24 寸铝框行李箱", "brand": "TrailOx", "price": "149.00", "currency": "USD"},
            "P2": {"title": "TrailOx 28 寸铝框行李箱", "brand": "TrailOx", "price": "159.00", "currency": "USD"}}


def grade(reply, quote, amount="1057.90", currency="CNY", basis="converted", products=None, pid="P1", text=None):
    extraction = {"recommendations": [], "claims": [{"product_id": pid, "evidence": quote,
        "amount": amount, "currency": currency, "amount_text": text or amount + "元", "price_basis": basis}],
        **{key: {"value": False, "evidence": ""} for key in ("no_match", "clarification", "acknowledgement")}}
    return evaluate_turn({"mode": "recommend", "eligible_ids": [pid], "min_recommendations": 0}, extraction, reply, products or catalog())


def converted_catalog():
    products = catalog()
    products["P1"]["observed_prices"] = [{"basis": "converted_reference", "amount": "1057.90", "currency": "CNY",
        "original": {"amount": "149.00", "currency": "USD"}, "fx": {"rate": "7.1", "source": "static_demo", "as_of": None}}]
    return products


@pytest.mark.parametrize("label", ["换算参考", "折合", "约合", "converted reference"])
def test_converted_requires_tool_observation_and_local_label(label):
    quote = "P1 " + label + " 1057.90元"
    assert grade(quote, quote, products=converted_catalog())["passed"]
    assert not grade(quote, quote)["passed"]


@pytest.mark.parametrize("mutation", ["original_amount", "original_currency", "rate", "source", "snapshot", "amount", "missing_fx", "missing_snapshot"])
def test_malformed_conversion_provenance_is_rejected(mutation):
    products = converted_catalog()
    row = products["P1"]["observed_prices"][0]
    if mutation == "original_amount":
        row["original"]["amount"] = "148"
    elif mutation == "original_currency":
        row["original"]["currency"] = "CNY"
    elif mutation == "rate":
        row["fx"]["rate"] = "NaN"
    elif mutation == "source":
        row["fx"]["source"] = "invented"
    elif mutation == "snapshot":
        row["fx"]["as_of"] = "2026-11-04"
    elif mutation == "amount":
        row["fx"]["rate"] = "7.2"
    elif mutation == "missing_fx":
        del row["fx"]
    elif mutation == "missing_snapshot":
        del row["fx"]["as_of"]
    quote = "P1 换算参考1057.90元"
    assert not grade(quote, quote, products=products)["passed"]


def test_conversion_cannot_masquerade_as_original_or_use_distant_label():
    products = converted_catalog()
    for quote, basis in [("P1 标价1057.90元", "merchandise"), ("P1 标价1057.90元，换算另计", "converted")]:
        assert not grade(quote, quote, basis=basis, products=products)["passed"]


def test_conversion_validates_sku_original_and_minor_rounding():
    products = converted_catalog()
    row = products["P1"]["observed_prices"][0]
    row["original"]["amount"] = "149.001"
    products["P1"]["sku_prices"] = [{"amount": "149.001", "currency": "USD"}]
    quote = "P1 换算参考1057.90元"
    assert grade(quote, quote, products=products)["passed"]


@pytest.mark.parametrize("rate", ["0", "-7.1", "Infinity", "7.1001"])
def test_invalid_or_unrounded_conversion_rates_fail(rate):
    products = converted_catalog()
    products["P1"]["observed_prices"][0]["fx"]["rate"] = rate
    quote = "P1 折合1057.90元"
    assert not grade(quote, quote, products=products)["passed"]


def test_conversion_cannot_satisfy_required_original_price():
    products = converted_catalog()
    quote = "P1 折合1057.90元"
    extraction = {"recommendations": [{"product_id":"P1", "evidence":"P1"}],
        "claims": [{"product_id":"P1", "evidence":quote, "amount":"1057.90",
                    "currency":"CNY", "amount_text":"1057.90元", "price_basis":"converted"}],
        **{key:{"value":False,"evidence":""} for key in ("no_match","clarification","acknowledgement")}}
    result = evaluate_turn({"mode":"recommend", "eligible_ids":["P1"], "required_price_ids":["P1"]}, extraction, quote, products)
    assert result["failures"] == ["missing_required_merchandise_price:P1"]


@pytest.mark.parametrize("orientation", ["row", "column"])
def test_amount_only_table_quote_is_bound_to_own_product_and_header(orientation):
    products = catalog()
    products["P1"].update(price="149", currency="CNY", observed_prices=[{"basis":"landed_total", "amount":"214", "currency":"CNY"}])
    if orientation == "row":
        reply = "| 商品 | 标价 | 到手价 |\n|---|---|---|\n| P1 | 149元 | 214元 |\n| P2 | 159元 | 224元 |"
    else:
        reply = "| 商品 | P1 | P2 |\n|---|---|---|\n| 标价 | 149元 | 159元 |\n| 到手价 | 214元 | 224元 |"
    result = grade(reply, "214元", "214", basis="landed", products=products)
    assert result["passed"], result
    check = result["evidence_checks"][0]
    assert check["context_anchor"]["product_id"] == "P1"
    assert "P2" not in check["expanded_evidence"]
    assert not grade(reply, "224元", "224", basis="landed", products=products)["passed"]


def test_table_landed_label_does_not_leak_into_merchandise_cell():
    products = catalog()
    products["P1"].update(price="149", currency="CNY", observed_prices=[{"basis":"landed_total", "amount":"149", "currency":"CNY"}])
    reply = "| 商品 | 标价 | 到手价 |\n|---|---|---|\n| P1 | 149元 | 214元 |"
    assert not grade(reply, "149元", "149", basis="landed", products=products)["passed"]


@pytest.mark.parametrize("header", ["P1 | P1", "P1 | P999", "P2 | P1"])
def test_table_does_not_guess_swapped_unknown_or_ambiguous_columns(header):
    reply = "| 商品 | " + header + " |\n|---|---|---|\n| 标价 | 149美元 | 159美元 |"
    assert not grade(reply, "149美元", "149", "USD", "merchandise", text="149美元")["passed"]


def test_local_approximate_landed_equation_result():
    products = catalog()
    products["P1"]["observed_prices"] = [{"basis":"landed_total", "amount":"214", "currency":"CNY"}]
    quote = "P1 到手价：商品189元 + 运费25元 =约 214.00元"
    assert grade(quote, quote, "214.00", basis="landed", products=products)["passed"]


@pytest.mark.parametrize("sentence", [
    "关于到手价（供参考）：若按发往中国的含运税口径估算，商品 189.00 元 + 运费 25.00 元 + 关税 0.00 元（已适用小额免税，关税率 9%）= **约 214.00 元**",
    "若按到手价口径，该商品发往 CN 为 189.00 + 运费 25.00 + 关税 0.00 = 214.00 元",
])
def test_landed_equation_with_delivery_clause(sentence):
    products = catalog()
    products["P1"]["observed_prices"] = [{"basis": "landed_total", "amount": "214", "currency": "CNY"}]
    reply = f"P1：{sentence}"
    assert grade(reply, reply, "214.00", basis="landed", products=products, text="214.00 元")["passed"]
    assert not grade(reply, reply, "189.00", basis="landed", products=products, text="189.00")["passed"]
    for altered in (
        reply.replace("214.00", "215.00"),
        reply.replace("到手价", "标价").replace("含运税口径", "商品标价口径"),
        reply.replace("，商品", "。商品").replace("，该商品", "。该商品"),
        reply.replace("运费", "标价"),
        reply.replace("若按", "不按"),
    ):
        amount = "215.00" if "215.00" in altered else "214.00"
        assert not grade(altered, altered, amount, basis="landed", products=products, text=amount + " 元")["passed"]


def test_shortened_distinctive_name_is_unique_in_full_catalog():
    products = catalog()
    quote = "TrailOx 24 寸行李箱 149美元"
    assert grade(quote, quote, "149", "USD", "merchandise", products=products, text="149美元")["passed"]
    products["P3"] = {**products["P1"], "title": "TrailOx 24 寸软壳行李箱"}
    assert not grade(quote, quote, "149", "USD", "merchandise", products=products, text="149美元")["passed"]


def test_shortened_name_preserves_model_digits_and_rejects_shared_brand():
    for quote in ["TrailOx 124 寸行李箱 149美元", "TrailOx 行李箱149美元"]:
        assert not grade(quote, quote, "149", "USD", "merchandise", text="149美元")["passed"]


def test_lumengo_title_with_omitted_rechargeable_qualifier():
    products = {"P1": {"title":"LumenGo 可充电便携露营灯", "brand":"LumenGo", "price":"89", "currency":"CNY"},
                "P2": {"title":"LumenGo 手电筒", "brand":"LumenGo", "price":"99", "currency":"CNY"}}
    quote = "LumenGo 便携露营灯 89元"
    assert grade(quote, quote, "89", basis="merchandise", products=products)["passed"]


def test_table_inside_larger_prose_quote_cannot_inherit_prose_product():
    products = catalog()
    reply = "P1\n| 商品 | 标价 |\n|---|---|\n| P2 | 149美元 |"
    assert not grade(reply, reply, "149", "USD", "merchandise", products=products, text="149美元")["passed"]


@pytest.mark.parametrize("header,basis", [("到手价", "merchandise"), ("换算参考价格", "merchandise"),
                                          ("标价", "landed"), ("换算参考价格", "landed"),
                                          ("到手价", "converted"), ("标价", "converted")])
def test_table_header_price_basis_cannot_be_overridden_by_extraction(header, basis):
    products = converted_catalog()
    products["P1"]["sku_prices"] = [{"amount":"1057.90", "currency":"CNY"}]
    products["P1"]["observed_prices"].append({"basis":"landed_total", "amount":"1057.90", "currency":"CNY"})
    cell_label = {"merchandise":"", "landed":"到手价", "converted":"折合"}[basis]
    reply = "| 商品 | " + header + " |\n|---|---|\n| P1 | " + cell_label + "1057.90元 |"
    assert not grade(reply, "1057.90元", basis=basis, products=products)["passed"]


# Frozen feature-span examples from the 2026-11-04 direct/LLM lamp replies.
LAMP_CATALOG = {'P1008': {'aliases': ['LumenGo 便携露营灯 可充电'],
           'brand': 'LumenGo',
           'currency': 'CNY',
           'description': '露营灯 三档调光 Type-C充电 磁吸挂钩 防水 IPX5 户外 应急 停电 抗造耐摔',
           'highlights': [{'detail': 'IPX5 防水，抗摔', 'label': '防护'},
                          {'detail': '最长 72 小时', 'label': '续航'}],
           'price': '89.00',
           'product_id': 'P1008',
           'ships_to': ['CN', 'US', 'EU'],
           'sku_prices': [{'amount': '89.00',
                           'currency': 'CNY',
                           'sku_id': 'P1008-S1',
                           'spec': '军绿'},
                          {'amount': '89.00',
                           'currency': 'CNY',
                           'sku_id': 'P1008-S2',
                           'spec': '橙色'}],
           'title': 'LumenGo 便携露营灯 可充电'},
 'P1039': {'aliases': ['SolarLamp 太阳能营地灯'],
           'brand': 'SolarLamp',
           'currency': 'CNY',
           'description': '太阳能 营地灯 露营灯 可充电 防水IPX5 可折叠 应急照明',
           'highlights': [{'detail': '太阳能 + USB 双模', 'label': '供电'}],
           'price': '129.00',
           'product_id': 'P1039',
           'ships_to': ['CN', 'US', 'EU'],
           'sku_prices': [{'amount': '129.00',
                           'currency': 'CNY',
                           'sku_id': 'P1039-S1',
                           'spec': '黑色'}],
           'title': 'SolarLamp 太阳能营地灯'}}
LAMP_CASES = [{'expectation': {'eligible_ids': ['P1008', 'P1039'],
                  'min_recommendations': 1,
                  'mode': 'recommend'},
  'extraction': {'acknowledgement': {'evidence': '', 'value': False},
                 'claims': [{'amount': '89.00',
                             'amount_text': '89.00',
                             'currency': 'CNY',
                             'evidence': '**LumenGo 便携露营灯 可充电** — 89.00 CNY',
                             'price_basis': 'merchandise',
                             'product_id': 'P1008'},
                            {'amount': '129.00',
                             'amount_text': '129.00',
                             'currency': 'CNY',
                             'evidence': '**SolarLamp 太阳能营地灯** — 129.00 CNY',
                             'price_basis': 'merchandise',
                             'product_id': 'P1039'}],
                 'clarification': {'evidence': '', 'value': False},
                 'comparison_reasoning': {'evidence': ['- 三档调光、Type-C 充电、磁吸挂钩、IPX5 防水、抗摔\n'
                                                       '   - 续航最长 72 小时；可选军绿、橙色',
                                                       '- 太阳能 + USB 双模供电、IPX5 防水、可折叠、应急照明'],
                                          'value': True},
                 'no_match': {'evidence': '', 'value': False},
                 'recommendations': [{'evidence': '**LumenGo 便携露营灯 可充电** — 89.00 CNY',
                                      'product_id': 'P1008'},
                                     {'evidence': '**SolarLamp 太阳能营地灯** — 129.00 CNY',
                                      'product_id': 'P1039'}]},
  'reply': '按商品标价 130 元以内、寄往中国、不含运税的条件，有两款露营灯符合要求：\n'
           '\n'
           '1. **LumenGo 便携露营灯 可充电** — 89.00 CNY\n'
           '   - 三档调光、Type-C 充电、磁吸挂钩、IPX5 防水、抗摔\n'
           '   - 续航最长 72 小时；可选军绿、橙色\n'
           '   - 预算命中，可发往中国\n'
           '\n'
           '2. **SolarLamp 太阳能营地灯** — 129.00 CNY\n'
           '   - 太阳能 + USB 双模供电、IPX5 防水、可折叠、应急照明\n'
           '   - 黑色；可发往中国\n'
           '\n'
           '两款都满足你的标价预算和收货地要求，也都不属于钥匙扣手电。如果更看重续航和便携，LumenGo 更合适；如果希望有太阳能补电，可以选 SolarLamp。\n'
           '\n'
           '以上均为商品标价，不含运费和税费；如需了解含运税的到手价，可以再告诉我。'},
 {'expectation': {'eligible_ids': ['P1008', 'P1039'],
                  'min_recommendations': 1,
                  'mode': 'recommend'},
  'extraction': {'acknowledgement': {'evidence': '', 'value': False},
                 'claims': [{'amount': '89.00',
                             'amount_text': '89.00',
                             'currency': 'CNY',
                             'evidence': '**1. LumenGo 便携露营灯 可充电 — 标价 89.00 CNY**',
                             'price_basis': 'merchandise',
                             'product_id': 'P1008'},
                            {'amount': '129.00',
                             'amount_text': '129.00',
                             'currency': 'CNY',
                             'evidence': '**2. SolarLamp 太阳能营地灯 — 标价 129.00 CNY**',
                             'price_basis': 'merchandise',
                             'product_id': 'P1039'}],
                 'clarification': {'evidence': '', 'value': False},
                 'comparison_reasoning': {'evidence': ['- 特点：三档调光、Type-C 充电、磁吸挂钩、IPX5 防水、最长 72 '
                                                       '小时续航',
                                                       '- 特点：太阳能 + USB 双模供电、IPX5 防水、可折叠、应急照明'],
                                          'value': True},
                 'no_match': {'evidence': '', 'value': False},
                 'recommendations': [{'evidence': '**1. LumenGo 便携露营灯 可充电 — 标价 89.00 CNY**',
                                      'product_id': 'P1008'},
                                     {'evidence': '**2. SolarLamp 太阳能营地灯 — 标价 129.00 CNY**',
                                      'product_id': 'P1039'}]},
  'reply': '按你更新后的条件（商品标价 130 元以内、寄中国、不含运税、排除钥匙扣手电），共找到 2 款合适的露营灯：\n'
           '\n'
           '**1. LumenGo 便携露营灯 可充电 — 标价 89.00 CNY**\n'
           '- 规格：军绿、橙色（同价）\n'
           '- 特点：三档调光、Type-C 充电、磁吸挂钩、IPX5 防水、最长 72 小时续航\n'
           '- 可发往中国\n'
           '\n'
           '**2. SolarLamp 太阳能营地灯 — 标价 129.00 CNY**\n'
           '- 规格：黑色\n'
           '- 特点：太阳能 + USB 双模供电、IPX5 防水、可折叠、应急照明\n'
           '- 可发往中国\n'
           '\n'
           '以上价格均为商品原币种标价，不含运费和税费。两款中，LumenGo 价格更低，续航和防水表现明确；SolarLamp 接近预算上限，优势是太阳能供电、可折叠。\n'
           '\n'
           '如果你需要，我可以再补充这两款寄到中国的到手价参考（含运费）。候选列表并非全库穷举。'}]


@pytest.mark.parametrize("saved", LAMP_CASES)
def test_saved_feature_only_comparison_spans_inherit_exclusive_headings(saved):
    result = evaluate_turn(saved["expectation"], saved["extraction"], saved["reply"], LAMP_CATALOG)
    assert result["passed"], result["failures"]
    checks = [check for check in result["evidence_checks"] if check["kind"] == "comparison_reasoning"]
    assert {check["product_id"] for check in checks} == {"P1008", "P1039"}
    assert all(check["exact_substring"] and check["context_anchor"] for check in checks)


@pytest.mark.parametrize("heading", ["P1008 / P1039", "P1008\n## P999", "P1008\n## P1039"])
def test_comparison_spans_do_not_inherit_joint_unknown_or_other_product_heading(heading):
    import copy
    saved = copy.deepcopy(LAMP_CASES[0])
    first, second = saved["extraction"]["comparison_reasoning"]["evidence"]
    reply = "LumenGo 便携露营灯 可充电\nSolarLamp 太阳能营地灯\n## " + heading + "\n" + first + "\n## P1039\n" + second
    # Recommendations remain verbatim and valid; the first fact span has no
    # exclusive P1008 heading, so neither generic attribute can rescue it.
    saved["extraction"]["recommendations"] = [{"product_id": key, "evidence": row["title"]} for key,row in LAMP_CATALOG.items()]
    saved["extraction"]["claims"] = []
    assert not evaluate_turn({"mode":"recommend", "eligible_ids":["P1008","P1039"]}, saved["extraction"], reply, LAMP_CATALOG)["passed"]


def test_trailing_pack_quantity_can_be_omitted_only_when_catalog_unique():
    products = {"P1":{"title":"CascadePro 钛合金折叠登山杖一对", "brand":"CascadePro", "price":"149", "currency":"USD"},
                "P2":{"title":"CascadePro 铝合金登山杖", "brand":"CascadePro", "price":"99", "currency":"USD"}}
    quote = "CascadePro 钛合金折叠登山杖是登山杖，而且标价149美元"
    assert grade(quote, quote, "149", "USD", "merchandise", products=products, text="149美元")["passed"]
    products["P3"] = {**products["P1"], "title":"CascadePro 钛合金折叠登山杖单支"}
    assert not grade(quote, quote, "149", "USD", "merchandise", products=products, text="149美元")["passed"]


def test_title_abbreviation_cannot_remove_a_semantic_model_noun():
    products = {"P1":{"title":"LumenGo 三档便携营地灯", "brand":"LumenGo", "price":"89", "currency":"CNY"},
                "P2":{"title":"LumenGo 手电筒", "brand":"LumenGo", "price":"99", "currency":"CNY"}}
    quote = "LumenGo 三档便携 89元"
    assert not grade(quote, quote, "89", basis="merchandise", products=products)["passed"]
