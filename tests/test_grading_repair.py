"""Positive/negative controls for multiple evidence spans and price association."""
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


def products():
    return {'P1':{'product_id':'P1','title':'Alpha 30W charger','brand':'Alpha','price':'89.10','currency':'CNY'},
            'P2':{'product_id':'P2','title':'Alpha 65W charger','brand':'Alpha','price':'159','currency':'CNY'}}


def extraction(claims=(), comparison=None):
    value={'recommendations':[{'product_id':'P1','evidence':'Alpha 30W charger'},
                              {'product_id':'P2','evidence':'Alpha 65W charger'}],
           'claims':list(claims), **{k:{'value':False,'evidence':''} for k in
                                    ('no_match','clarification','acknowledgement')}}
    if comparison is not None:
        value['comparison_reasoning']={'value':True,'evidence':comparison}
    return value


def claim(quote='Alpha 30W charger 89.10元', amount='89.10', text='89.10元', pid='P1'):
    return {'product_id':pid,'amount':amount,'currency':'CNY','amount_text':text,'evidence':quote}


def grade(value, reply, catalog=None, **extra):
    from scripts.shopping_grading import evaluate_turn
    return evaluate_turn({'mode':'recommend','eligible_ids':['P1','P2'], **extra},value,reply,catalog or products())


def test_exact_duplicates_are_normalized_without_mutating_the_input():
    from scripts.shopping_grading import parse_extraction
    value=extraction([claim(),claim()])
    original=copy.deepcopy(value)
    parsed=parse_extraction(json.dumps(value))
    assert len(parsed['claims'])==1
    assert value==original


def test_same_fact_in_distinct_passages_is_checked_in_both_places():
    from scripts.shopping_grading import parse_extraction
    a='Alpha 30W charger 89.10元'
    b='P1 标价89.10元'
    value=parse_extraction(json.dumps(extraction([claim(a),claim(b)])))
    assert len(value['claims'])==2
    assert grade(value,a+'\n'+b+'\nAlpha 65W charger')['passed']
    assert not grade(value,a+'\nAlpha 65W charger')['passed']


def test_unique_complete_unbranded_model_name_can_anchor_a_price():
    catalog=products()
    catalog['P1']['title']='Alpha 30W 迷你充电器'
    quote='30W 迷你充电器 89.10元'
    value=extraction([claim(quote)])
    value['recommendations'][0]['evidence']='Alpha 30W 迷你充电器'
    assert grade(value,'Alpha 30W 迷你充电器\nAlpha 65W charger\n'+quote,catalog)['passed']
    catalog['P3']={'title':'Gamma 30W 迷你充电器','brand':'Gamma','price':'89.10','currency':'CNY'}
    assert not grade(value,quote,catalog)['passed']


def test_unbranded_model_alias_cannot_slice_a_larger_wattage():
    from scripts.shopping_grading import evaluate_turn
    catalog=products()
    catalog['P1']['title']='Alpha 30W 迷你充电器'
    value=extraction()
    value['recommendations']=[{'product_id':'P1','evidence':'130W 迷你充电器'}]
    assert not evaluate_turn({'mode':'recommend','eligible_ids':['P1']},value,'130W 迷你充电器',catalog)['passed']


def test_landed_equation_ignores_separators_inside_tax_qualifiers():
    catalog=products()
    catalog['P1']['observed_prices']=[{'basis':'landed_total','amount':'154.10','currency':'CNY'}]
    quote='Alpha 30W charger 到手价参考（寄欧盟）：商品89.10元+运费65元+关税0元（税率12%，适用免税）=154.10元'
    value=extraction([{**claim(quote,'154.10','154.10元'),'price_basis':'landed'}])
    assert grade(value,quote+'\nAlpha 65W charger',catalog)['passed']


@pytest.mark.parametrize('total_prefix', ['合计约', '总计：'])
def test_landed_equation_can_end_with_a_comma_separated_total(total_prefix):
    quote='P1 到手价参考：商品 89.10元 + 运费 25元 + 关税 0元（适用低值免税），'+total_prefix+' **114.10元**'
    catalog=products()
    catalog['P1']['observed_prices']=[{'amount':'114.10','currency':'CNY','basis':'landed_total'}]
    value=extraction([{**claim(quote,'114.10','114.10元'),'price_basis':'landed'}])
    assert grade(value,'Alpha 30W charger\n'+quote+'\nAlpha 65W charger',catalog)['passed']


@pytest.mark.parametrize('quote', [
    'P1 到手价参考：商品 89.10元 + 运费 25元 + 关税 0元。商品标价合计约 114.10元',
    'P1 商品价格：商品 89.10元 + 运费 25元 + 关税 0元，合计约 114.10元',
    'P1 到手价参考：商品 89.10元 + 运费 25元 + 关税 0元，另一个套餐合计约 114.10元',
])
def test_comma_total_cannot_inherit_an_unrelated_or_merchandise_label(quote):
    catalog=products()
    catalog['P1']['observed_prices']=[{'amount':'114.10','currency':'CNY','basis':'landed_total'}]
    value=extraction([{**claim(quote,'114.10','114.10元'),'price_basis':'landed'}])
    assert not grade(value,'Alpha 30W charger\n'+quote+'\nAlpha 65W charger',catalog)['passed']


def test_conflicting_price_is_not_hidden_by_deduplication():
    value=extraction([claim(),claim('P1 98元','98','98元')])
    verdict=grade(value,'Alpha 30W charger 89.10元\nP1 98元\nAlpha 65W charger')
    assert not verdict['passed']
    assert any('wrong_price' in f for f in verdict['failures'])


def test_comparison_can_quote_two_verbatim_paragraphs():
    left='Alpha 30W charger 更适合手机。'
    right='Alpha 65W charger 更适合笔记本。'
    assert grade(extraction(comparison=[left,right]),left+'\n'+right,
                 require_comparison_reasoning=True)['passed']


def test_implicit_comparison_needs_distinct_supported_attributes():
    catalog=products()
    catalog['P1']['highlights']=[{'label':'用途','detail':'适配手机快充'}]
    catalog['P2']['highlights']=[{'label':'用途','detail':'适配笔记本快充'}]
    quotes=['Alpha 30W charger：适配手机快充。','Alpha 65W charger：适配笔记本快充。']
    assert grade(extraction(comparison=quotes),'\n'.join(quotes),catalog,
                 require_comparison_reasoning=True)['passed']
    catalog['P2']['highlights']=catalog['P1']['highlights']
    quotes=['Alpha 30W charger：适配手机快充。','Alpha 65W charger：适配手机快充。']
    assert not grade(extraction(comparison=quotes),'\n'.join(quotes),catalog,
                     require_comparison_reasoning=True)['passed']


def test_negated_catalog_attributes_do_not_support_an_implicit_comparison():
    catalog=products()
    catalog['P1']['highlights']=[{'label':'用途','detail':'适配手机快充'}]
    catalog['P2']['highlights']=[{'label':'用途','detail':'适配笔记本快充'}]
    quotes=['Alpha 30W charger：不是适配手机快充。','Alpha 65W charger：不是适配笔记本快充。']
    assert not grade(extraction(comparison=quotes),'\n'.join(quotes),catalog,
                     require_comparison_reasoning=True)['passed']


def test_model_names_repeated_in_catalog_do_not_become_comparison_facts():
    catalog=products()
    catalog['P1']['description']='30W'
    catalog['P2']['description']='65W'
    quotes=['Alpha 30W charger 很好。','Alpha 65W charger 很好。']
    assert not grade(extraction(comparison=quotes),'\n'.join(quotes),catalog,
                     require_comparison_reasoning=True)['passed']


def test_implicit_attributes_cannot_cross_another_product_in_the_same_span():
    catalog=products()
    catalog['P1']['highlights']=[{'label':'用途','detail':'适配手机快充'}]
    catalog['P2']['highlights']=[{'label':'用途','detail':'适配笔记本快充'}]
    quotes=['Alpha 30W charger 无此用途；Alpha 65W charger 适配手机快充。',
            'Alpha 65W charger 无此用途；Alpha 30W charger 适配笔记本快充。']
    assert not grade(extraction(comparison=quotes),'\n'.join(quotes),catalog,
                     require_comparison_reasoning=True)['passed']


def test_connective_erqie_does_not_make_generic_praise_a_comparison():
    quotes=['Alpha 30W charger 而且很不错。','Alpha 65W charger 很不错。']
    assert not grade(extraction(comparison=quotes),'\n'.join(quotes),
                     require_comparison_reasoning=True)['passed']


@pytest.mark.parametrize('quotes',[
    ['Alpha 30W charger 更适合手机。','不存在的对比段落'],
    ['Alpha 30W charger 很好。','Alpha 65W charger 很好。'],
    ['Alpha 30W charger 更适合手机。','Alpha 30W charger 更适合手机。'],
])
def test_multi_quote_comparison_still_needs_real_difference_and_both_products(quotes):
    reply='Alpha 30W charger 更适合手机。\nAlpha 65W charger 更适合笔记本。\nAlpha 30W charger 很好。\nAlpha 65W charger 很好。'
    assert not grade(extraction(comparison=quotes),reply,require_comparison_reasoning=True)['passed']


def test_comparison_shared_brand_is_resolved_only_within_anchored_choices():
    catalog={'P1':{'title':'Terra 手工杯','brand':'Terra','price':'89.10','currency':'CNY'},
             'P2':{'title':'Nordic 陶瓷杯','brand':'Nordic','price':'159','currency':'CNY'},
             'P3':{'title':'Terra 茶具','brand':'Terra','price':'268','currency':'CNY'}}
    value=extraction(comparison='Terra 更适合手作风格，而 Nordic 更便宜。')
    value['recommendations']=[{'product_id':'P1','evidence':'Terra 手工杯'},
                              {'product_id':'P2','evidence':'Nordic 陶瓷杯'}]
    reply='Terra 手工杯\nNordic 陶瓷杯\nTerra 更适合手作风格，而 Nordic 更便宜。'
    assert grade(value,reply,catalog,require_comparison_reasoning=True)['passed']


def test_table_price_inherits_its_own_id_column_only():
    reply='| 商品 | Alpha 30W charger | Alpha 65W charger |\n|---|---|---|\n| 编号 | P1 | P2 |\n| 标价 | 89.10元 | 159元 |'
    row='| 标价 | 89.10元 | 159元 |'
    value=extraction([claim(row),claim(row,'159','159元','P2')])
    verdict=grade(value,reply)
    assert verdict['passed']
    assert any(e.get('context_anchor',{}).get('kind')=='table_column'
               for e in verdict['evidence_checks'] if e.get('context_anchor'))
    catalog=products()
    catalog['P1']['sku_prices']=[{'amount':'159','currency':'CNY'}]
    assert not grade(extraction([claim(row,'159','159元')]),reply,catalog)['passed']


def test_ambiguous_table_columns_are_not_guessed():
    reply='Alpha 30W charger\nAlpha 65W charger\n| 编号 | P1 | P1 |\n| 标价 | 89.10元 | 159元 |'
    assert not grade(extraction([claim('| 标价 | 89.10元 | 159元 |')]),reply)['passed']


def test_row_oriented_product_table_keeps_its_price_association():
    reply='| 商品 | 品类 | 标价 |\n|---|---|---|\n| Alpha 30W charger | 充电器 | 89.10元 |\n| Alpha 65W charger | 充电器 | 159元 |'
    row='| Alpha 30W charger | 充电器 | 89.10元 |'
    assert grade(extraction([claim(row)]),reply)['passed']
    catalog=products()
    catalog['P1']['sku_prices']=[{'amount':'159','currency':'CNY'}]
    wrong='| Alpha 65W charger | 充电器 | 159元 |'
    assert not grade(extraction([claim(wrong,'159','159元')]),reply,catalog)['passed']


def test_replay_recovers_newly_valid_saved_raw_without_model_calls():
    from scripts.shopping_replay import replay_checkpoint
    reply='Alpha 30W charger 89.10元\nAlpha 65W charger'
    turn={'turn':1,'query':'recommend','reply':reply,'error':'','extraction':None,
          'judge_raw_attempts':[json.dumps(extraction([claim(),claim()]))],
          'expectation':{'mode':'recommend','eligible_ids':['P1','P2']},
          'grading':{'passed':False,'failures':['extraction_error: duplicate item']},'tool_events':[]}
    checkpoint={'metadata':{'run_id':'fixture'},'runs':[{'task_id':'one','route':'direct',
                 'repetition':1,'turns':[turn],'error':'','task_passed':False}]}
    original=copy.deepcopy(checkpoint)
    report=replay_checkpoint(checkpoint,products())
    assert report['turns'][0]['outcome']=='business_success'
    assert report['turns'][0]['extraction_source']=='raw_attempt:0'
    assert checkpoint==original


def test_metadata_without_git_records_unknown_state_and_keeps_source_hash(monkeypatch):
    from scripts import eval_regression
    def missing(*args, **kwargs):
        raise FileNotFoundError('git is not installed')
    monkeypatch.setattr(eval_regression.subprocess,'run',missing)
    settings=SimpleNamespace(llm_model='test')
    data=eval_regression.build_run_metadata(settings,[{'id':'one'}],'run',datetime.now(timezone.utc))
    assert data['git_commit']=='unavailable'
    assert data['git_dirty'] is None
    assert data['git_available'] is False
    assert data['source_fingerprint'].startswith('sha256:')
