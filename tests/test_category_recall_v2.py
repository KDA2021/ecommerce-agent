"""Offline regressions for explicit-category recall with imperfect vectors."""

from dataclasses import replace

import pytest

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from tests.test_catalog_recovery import usecase

VECTOR_IDS = ['P1054', 'P1007', 'P1032', 'P1039', 'P1031']
LAMP_IDS = {'P1008', 'P1039'}


def lamp_spec(cap=154, **overrides):  # Most expensive lamp: 129 merchandise + 25 freight
    return ProductSearchSpec(**{
        'normalized_query': '露营灯 户外营地照明 可充电 太阳能',
        'category': '露营灯', 'ship_to': 'CN', 'price_max_major': cap,
        'top_k': 8, **overrides,
    })


async def test_one_matching_vector_does_not_hide_other_affordable_category_items():
    search, repo, index = usecase(VECTOR_IDS)
    result = await search.execute(lamp_spec())
    assert {hit['product_id'] for hit in result['hits']} == LAMP_IDS
    assert repo.scans == index.calls == 1
    diagnostics = result['retrieval_diagnostics']
    assert diagnostics['fallback_trigger'] == 'category_supplement'
    assert diagnostics['vector_candidate_ids'] == VECTOR_IDS
    assert diagnostics['added_candidate_ids'] == ['P1008']
    assert diagnostics['keyword_scans'] == 1
    assert result['total_candidates'] == 2


async def test_tight_budget_explains_matching_lamps_without_unrelated_towels():
    search, repo, _ = usecase(VECTOR_IDS)
    result = await search.execute(lamp_spec(80))
    assert result['hits'] == []
    assert {item['product_id'] for item in result['filtered_out']} == LAMP_IDS
    assert all(item['reason'] == 'over_price_cap' for item in result['filtered_out'])
    assert repo.scans == 1


async def test_category_supplement_keeps_shipping_filter():
    search, _, _ = usecase(VECTOR_IDS)
    result = await search.execute(lamp_spec(ship_to='JP'))
    assert result['hits'] == []
    assert {item['product_id'] for item in result['filtered_out']} == LAMP_IDS
    assert all(item['reason'] == 'ship_to_unavailable' for item in result['filtered_out'])


@pytest.mark.parametrize('ordinary', ['CNY', 'USD', 'RMB', 'USB', 'Type-C'])
async def test_currency_and_connector_words_do_not_disable_category_supplement(ordinary):
    search, repo, _ = usecase(VECTOR_IDS)
    result = await search.execute(lamp_spec(normalized_query=f'露营灯 {ordinary}'))
    assert {hit['product_id'] for hit in result['hits']} == LAMP_IDS
    assert repo.scans == 1


@pytest.mark.parametrize('modifier', ['rechargeable', 'solar', 'portable bright'])
async def test_ordinary_english_modifiers_resolve_against_catalog_identity(modifier):
    search, repo, index = usecase(VECTOR_IDS)
    result = await search.execute(lamp_spec(normalized_query=f'露营灯 {modifier}'))
    assert {hit['product_id'] for hit in result['hits']} == LAMP_IDS
    assert repo.scans == index.calls == 1
    assert result['retrieval_diagnostics']['fallback_trigger'] == 'category_supplement'


async def test_keyword_only_category_with_english_modifier_uses_same_matching_rule():
    _, repo, _ = usecase(VECTOR_IDS)
    result = await CatalogSearchUseCase(repo).execute(
        lamp_spec(normalized_query='露营灯 户外 rechargeable'))
    assert {hit['product_id'] for hit in result['hits']} == LAMP_IDS
    assert repo.scans == 1


async def test_known_brand_with_english_modifier_recovers_only_that_brand():
    search, repo, _ = usecase(['P1060'])
    result = await search.execute(lamp_spec(normalized_query='LumenGo 露营灯 rechargeable'))
    diagnostics = result['retrieval_diagnostics']
    assert set(diagnostics['added_candidate_ids']) == {'P1008', 'P1038'}
    assert set(diagnostics['keyword_candidate_ids']) == {'P1008', 'P1038'}
    assert 'P1039' not in diagnostics['keyword_candidate_ids']
    assert repo.scans == 1


async def test_unknown_alphanumeric_model_with_modifier_is_not_generalized_to_category():
    search, repo, _ = usecase(VECTOR_IDS)
    result = await search.execute(lamp_spec(normalized_query='Imaginary9900 露营灯 rechargeable'))
    assert result['retrieval_diagnostics']['added_candidate_ids'] == []
    assert result['retrieval_diagnostics']['keyword_candidate_ids'] == []
    assert repo.scans == 1


async def test_keyword_only_category_search_excludes_irrelevant_partial_word_overlap():
    search, repo, _ = usecase(VECTOR_IDS)
    search = CatalogSearchUseCase(repo)
    result = await search.execute(lamp_spec())
    assert {hit['product_id'] for hit in result['hits']} == LAMP_IDS
    assert repo.scans == 1


async def test_category_match_uses_title_description_or_catalog_category():
    search, repo, _ = usecase(VECTOR_IDS)
    products = await repo.find_by_ids(['P1008'])
    lamp = products[0]
    for product in [
        replace(lamp, category='露营灯', title='便携照明', description='充电'),
        replace(lamp, category='户外', title='便携露营灯', description='充电'),
        replace(lamp, category='户外', title='便携照明', description='营地灯 可充电'),
    ]:
        assert search._category_matches(product, '露营灯')
    assert not search._category_matches(
        replace(lamp, category='户外', title='速干毛巾', description='适合露营'), '露营灯')


async def test_category_supplement_failure_preserves_vectors_and_diagnostics():
    search, repo, _ = usecase(VECTOR_IDS)
    async def unavailable():
        raise RuntimeError('catalog unavailable')
    repo.list_all = unavailable
    result = await search.execute(lamp_spec(cap=1000))
    assert [hit['product_id'] for hit in result['hits']] == VECTOR_IDS
    assert result['retrieval_diagnostics']['error_type'] == 'RuntimeError'
    assert result['retrieval_diagnostics']['added_candidate_ids'] == []


async def test_absent_category_scans_once_and_does_not_restore_irrelevant_vectors():
    search, repo, index = usecase(VECTOR_IDS)
    result = await search.execute(lamp_spec(category='不存在的品类'))
    assert result['hits'] == []
    assert result.get('filtered_out', []) == []
    assert repo.scans == index.calls == 1
    assert result['retrieval_diagnostics']['keyword_candidate_ids'] == []


async def test_category_supplement_bounds_pool_before_reranking():
    search, repo, _ = usecase(VECTOR_IDS)
    template = (await repo.find_by_ids(['P1008']))[0]
    for index in range(20):
        await repo.save(replace(template, product_id=f'extra-lamp-{index}'))
    class Reranker:
        async def rerank(self, query, documents):
            assert len(documents) == 8
            return [1.0] * len(documents)
    search._reranker = Reranker()
    result = await search.execute(lamp_spec(top_k=20))
    assert result['rerank_applied'] is True
    assert len(result['hits']) == 8


@pytest.mark.parametrize('category', ['露营炊具', '户外锅具', '炊具套装', '炊具', '锅具', '套锅'])
async def test_compound_cookware_category_keeps_real_cookware_not_titanium_poles(category):
    search, repo, index = usecase(['P1048', 'P1010', 'P1039', 'P1007'])
    result = await search.execute(ProductSearchSpec(
        normalized_query='钛合金露营炊具套装', category=category,
        ship_to='CN', price_max_major=454, top_k=8))  # 429 merchandise + 25 freight
    assert [hit['product_id'] for hit in result['hits']] == ['P1048']
    assert result.get('filtered_out', []) == []
    assert repo.scans == index.calls == 1


@pytest.mark.parametrize('noun', ['炊具', '锅具', '套锅'])
async def test_cookware_aliases_are_explicit_category_nouns_in_catalog_facts(noun):
    search, repo, _ = usecase(['P1048'])
    product = (await repo.find_by_ids(['P1048']))[0]
    for field in ['category', 'title', 'description']:
        facts = {'category': '户外', 'title': '便携用品', 'description': '钛合金 240g'}
        facts[field] = f'户外{noun}套装'
        assert search._category_matches(replace(product, **facts), '露营炊具')


@pytest.mark.parametrize('category', ['露营配件', '炊具配件', '不存在的锅具'])
async def test_unknown_category_is_not_guessed_from_cookware_suffix_or_modifiers(category):
    search, repo, _ = usecase(['P1048', 'P1010'])
    result = await search.execute(ProductSearchSpec(
        normalized_query='钛合金露营炊具套装', category=category,
        ship_to='CN', price_max_major=450, top_k=8))
    assert result['hits'] == []
    assert repo.scans == 1


async def test_cookware_category_keeps_budget_hard_filter():
    search, repo, _ = usecase(['P1048', 'P1010'])
    result = await search.execute(ProductSearchSpec(
        normalized_query='钛合金露营炊具套装', category='露营炊具',
        ship_to='CN', price_max_major=428, top_k=8))
    assert result['hits'] == []
    assert [item['product_id'] for item in result['filtered_out']] == ['P1048']
    assert result['filtered_out'][0]['reason'] == 'over_price_cap'
    assert repo.scans == 1
