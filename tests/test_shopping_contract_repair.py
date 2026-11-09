"""Price evidence must survive catalog -> tool event -> grader projection."""
import copy

from app.application.tools.product_search import build_product_search_tool
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.persistence.seed_products import build_seed_products
from scripts.eval_shopping import observed_catalog


def search():
    return CatalogSearchUseCase(InMemoryProductRepository(build_seed_products()))


async def test_rejected_usd_item_preserves_original_price_and_labels_conversion():
    result = await search().execute(ProductSearchSpec(
        normalized_query='P1010', ship_to='CN', price_max_major=450, target_currency='CNY'))
    item = next(p for p in result['filtered_out'] if p['product_id']=='P1010')
    assert (item['price_major'], item['currency']) == (149, 'USD')
    assert item['pricing']['original']['amount']=='149.00'
    converted=item['pricing']['converted_reference']
    assert (converted['amount'],converted['currency'])==('1057.90','CNY')
    assert converted['original']=={'amount':'149.00','currency':'USD'}
    assert converted['fx']=={'rate':'7.1','source':'static_demo','as_of':None}


async def test_hit_and_rejection_use_the_same_original_and_conversion_contract():
    product=next(p for p in build_seed_products() if p.product_id=='P1010')
    usecase=search()
    spec=ProductSearchSpec(normalized_query='P1010',ship_to='EU')
    hit=usecase._to_card(1,product,spec).to_dict()
    rejected=usecase._to_rejected(product,spec,'over_price_cap')
    assert hit['pricing']['original']==rejected['pricing']['original']
    assert hit['pricing']['converted_reference']==rejected['pricing']['converted_reference']
    assert hit['pricing']['landed_total']['currency']==hit['landed_price']['currency']
    assert hit['pricing']['landed_total']['basis']=='landed_total'
    assert usecase._to_card(1,product,spec).to_comparison()['landed_currency']=='CNY'


async def test_same_currency_does_not_emit_a_fake_conversion():
    result=await search().execute(ProductSearchSpec(normalized_query='P1008'))
    hit=next(p for p in result['hits'] if p['product_id']=='P1008')
    assert hit['pricing']['original']['currency']=='CNY'
    assert 'converted_reference' not in hit['pricing']


async def test_filtered_prices_are_retained_for_audit_and_converted_observations():
    class Broker:
        events=[]
        def publish(self, session, kind, payload):
            self.events.append((kind,payload))
    broker=Broker()
    result=await build_product_search_tool(search(),broker).ainvoke({
        'normalized_query':'P1010','ship_to':'CN','price_max_major':450})
    retained=next(payload for kind,payload in broker.events if kind=='tool.result')
    assert retained['filtered_out']==result['filtered_out']
    catalog={'P1010':{'price':'149.00','currency':'USD'}}
    original=copy.deepcopy(catalog)
    projected=observed_catalog(catalog,broker.events)
    observation=next(p for p in projected['P1010']['observed_prices']
                     if p['basis']=='converted_reference')
    assert observation==result['filtered_out'][0]['pricing']['converted_reference']
    assert catalog==original


def test_legacy_filtered_number_without_price_provenance_is_not_authorized():
    catalog={'P1010':{'price':'149.00','currency':'USD'}}
    result=observed_catalog(catalog,[('tool.result',{'tool':'product_search','filtered_out':[
        {'product_id':'P1010','price_major':1057.90,'currency':'CNY'}]})])
    assert not result['P1010'].get('observed_prices')


async def test_tool_conversion_evidence_reaches_grader_without_becoming_a_sticker_price():
    from scripts.shopping_grading import evaluate_turn
    result=await search().execute(ProductSearchSpec(normalized_query='P1010',price_max_major=450))
    catalog={'P1010':{'product_id':'P1010','title':'CascadePro 钛合金折叠登山杖一对',
                     'brand':'CascadePro','price':'149.00','currency':'USD'}}
    events=[('tool.result',{'tool':'product_search',**result})]
    quote='没有符合预算的商品。P1010 换算参考1057.90元'
    claim={'product_id':'P1010','amount':'1057.90','currency':'CNY',
           'evidence':'P1010 换算参考1057.90元','amount_text':'1057.90元','price_basis':'converted'}
    extraction={'recommendations':[],'claims':[claim],
                'no_match':{'value':True,'evidence':'没有符合预算的商品'},
                'clarification':{'value':False,'evidence':''},
                'acknowledgement':{'value':False,'evidence':''}}
    projected=observed_catalog(catalog,events)
    assert evaluate_turn({'mode':'no_match','eligible_ids':[]},extraction,quote,projected)['passed']
    claim['price_basis']='merchandise'
    assert not evaluate_turn({'mode':'no_match','eligible_ids':[]},extraction,quote,projected)['passed']
