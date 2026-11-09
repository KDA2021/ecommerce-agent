"""Explicit catalog price types for both accepted and rejected candidates."""
from __future__ import annotations

from app.domain.catalog.exchange_rate import ExchangeRateTable
from app.domain.catalog.money import Money


def catalog_pricing(original: Money, sku_id: str, rates: ExchangeRateTable,
                    target_currency: str, landed: dict | None = None) -> dict:
    """Keep original merchandise prices separate from static demo conversions."""
    source = {'amount':f'{original.to_major_units():.2f}', 'currency':original.currency}
    result = {'original':{**source, 'basis':'merchandise', 'sku_id':sku_id,
                          'source':'catalog_primary_sku'}}
    if original.currency != target_currency:
        reference = rates.convert(original, target_currency)
        result['converted_reference'] = {
            'amount':f'{reference.to_major_units():.2f}', 'currency':reference.currency,
            'basis':'converted_reference', 'original':source,
            'fx':{'rate':str(rates.rate(original.currency,target_currency)),
                  'source':'static_demo', 'as_of':None},
        }
    if landed and 'landed_total_major' in landed:
        result['landed_total'] = {
            'amount':f"{landed['landed_total_major']:.2f}",
            'currency':landed.get('currency',target_currency), 'basis':'landed_total',
            'ship_to':landed.get('ship_to'), 'source':'tariff_schedule',
        }
    return result
