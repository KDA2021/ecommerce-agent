"""Replay the real SDVault failure without model calls or a database."""
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.ports.retrieval_ports import VectorHit
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.persistence.seed_products import build_seed_products


class Repository(InMemoryProductRepository):
    scans = 0

    async def list_all(self):
        self.scans += 1
        return await super().list_all()


class Embedder:
    async def embed(self, text):
        return [0.0]


class Index:
    calls = 0

    def __init__(self, ids):
        self.ids = ids

    async def search(self, embedding, top_n):
        self.calls += 1
        return [VectorHit(product_id=pid, score=0.9) for pid in self.ids][:top_n]


def usecase(ids):
    repo = Repository(build_seed_products())
    index = Index(ids)
    return CatalogSearchUseCase(repo, Embedder(), index), repo, index


async def test_nonempty_irrelevant_vectors_cannot_suppress_named_reader():
    search, repo, index = usecase(['P1060', 'P1033', 'P1053'])
    result = await search.execute(ProductSearchSpec(
        normalized_query='SDVault 双口读卡器', category='读卡器', ship_to='EU',
        price_max_major=154, top_k=8))  # 79 merchandise + 75 freight to EU
    assert result['hits'][0]['product_id'] == 'P1030'
    assert result['retrieval_diagnostics']['fallback_trigger'] == 'named_identity_missing'
    assert 'P1030' in result['retrieval_diagnostics']['added_candidate_ids']
    assert repo.scans == 1
    assert index.calls == 1


async def test_exact_product_id_can_recover_a_vector_miss():
    search, repo, index = usecase(['P1060'])
    result = await search.execute(ProductSearchSpec(normalized_query='P1030', ship_to='EU', price_max_major=154))
    assert result['hits'][0]['product_id'] == 'P1030'
    assert repo.scans == index.calls == 1


async def test_brand_with_ordinary_english_word_can_recover():
    search, _, _ = usecase(['P1060'])
    result = await search.execute(ProductSearchSpec(normalized_query='SDVault reader'))
    assert result['hits'][0]['product_id']=='P1030'


async def test_product_id_with_sentence_punctuation_can_recover():
    search, _, _ = usecase(['P1060'])
    result = await search.execute(ProductSearchSpec(normalized_query='P1030。'))
    assert result['hits'][0]['product_id']=='P1030'


async def test_budget_number_in_query_is_not_a_model_identity():
    search, _, _ = usecase(['P1060'])
    result = await search.execute(ProductSearchSpec(normalized_query='SDVault reader budget 100 CNY',price_max_major=100))
    assert result['hits'][0]['product_id']=='P1030'


async def test_unknown_alphanumeric_model_remains_a_constraint():
    search, _, _ = usecase(['P1060'])
    result = await search.execute(ProductSearchSpec(normalized_query='SDVault Imaginary9900 reader'))
    assert result['retrieval_diagnostics']['added_candidate_ids']==[]


async def test_recovery_keeps_budget_and_shipping_constraints():
    for cap, country, reason in [(78, 'EU', 'over_price_cap'), (80, 'EU', 'over_price_cap'), (80, 'JP', 'ship_to_unavailable')]:
        search, _, _ = usecase(['P1060'])
        result = await search.execute(ProductSearchSpec(
            normalized_query='SDVault 双口读卡器', category='读卡器', ship_to=country, price_max_major=cap))
        assert all(hit['product_id'] != 'P1030' for hit in result['hits'])
        assert any(item['product_id']=='P1030' and item['reason']==reason
                   for item in result.get('filtered_out', []))


async def test_matching_vector_results_do_not_trigger_catalog_scan():
    search, repo, _ = usecase(['P1030'])
    result = await search.execute(ProductSearchSpec(normalized_query='SDVault 双口读卡器', category='读卡器'))
    assert result['hits'][0]['product_id'] == 'P1030'
    assert repo.scans == 0


async def test_recovered_reader_card_exposes_catalog_facts_without_inventing_ports():
    search, _, _ = usecase(['P1060'])
    result=await search.execute(ProductSearchSpec(normalized_query='SDVault 双口读卡器'))
    assert 'SD TF' in result['hits'][0]['description']
    assert '双口' not in result['hits'][0]['description']


async def test_product_description_projection_is_bounded_and_marks_truncation():
    catalog=build_seed_products()
    product=next(p for p in catalog if p.product_id=='P1030')
    product.description='已确认商品事实。'*1000
    search=CatalogSearchUseCase(Repository(catalog),Embedder(),Index(['P1030']))
    result=await search.execute(ProductSearchSpec(normalized_query='SDVault 读卡器'))
    assert len(result['hits'][0]['description'])<=400
    assert result['hits'][0]['description_truncated'] is True


async def test_chinese_category_mismatch_triggers_one_lexical_recovery():
    search, repo, _ = usecase(['P1060', 'P1033'])
    result = await search.execute(ProductSearchSpec(normalized_query='双口读卡器', category='读卡器'))
    assert result['hits'][0]['product_id'] == 'P1030'
    assert result['retrieval_diagnostics']['fallback_trigger'] == 'lexical_mismatch'
    assert repo.scans == 1


async def test_unknown_model_does_not_report_a_fabricated_identity_match():
    search, repo, _ = usecase(['P1060'])
    result = await search.execute(ProductSearchSpec(normalized_query='ImaginaryReader9900 读卡器'))
    assert result['retrieval_diagnostics']['added_candidate_ids'] == []
    assert repo.scans == 1


async def test_failed_supplement_preserves_existing_vector_results():
    search, repo, _ = usecase(['P1060'])
    async def unavailable():
        raise RuntimeError('catalog read unavailable')
    repo.list_all = unavailable
    result = await search.execute(ProductSearchSpec(normalized_query='SDVault 双口读卡器'))
    assert result['hits'][0]['product_id']=='P1060'
    assert result['retrieval_diagnostics']['error_type']=='RuntimeError'
