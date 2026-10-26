"""Read-only personalized catalog prices, before checkout coupons/coin spending.

No partner IDs, procurement discounts or margins are passed to the model.
The SQL helper is installed separately by the database owner. Missing access is
not proof that a client has no partner: remain without a personalized quote.
"""
import datetime as dt
import math


def catalog_customer_price(base, markup_pct, promo_pct):
    """Match common.shop_price_for / db.partner_prices, including banker's round."""
    if type(base) is not int or base <= 0:
        raise ValueError('Invalid catalog price')
    markup=float(markup_pct)
    promo=float(promo_pct)
    if not math.isfinite(markup) or not math.isfinite(promo):
        raise ValueError('Invalid price context')
    if markup <= 0:
        return base
    full=max(1,int(round(float(base)*(100.0+max(0.0,markup))/100.0)))
    return max(1,int(round(full*(100.0-max(0.0,min(95.0,promo)))/100.0)))


async def customer_quotes(pool, uid, catalog):
    """Return prices keyed by (service_key, plan_index); fail closed on errors."""
    result={'status':'not_configured','prices':{}}
    if pool is None or type(uid) is not int or uid <= 0:
        return result
    try:
        async with pool.acquire() as c, c.transaction(readonly=True):
            rows=await c.fetch('''SELECT service_key,markup_pct,promo_pct
                FROM public.support_price_context_readonly($1::bigint,$2::timestamp)''',
                uid,dt.datetime.now())
        rates={}
        for row in rows:
            key=row['service_key']
            if not isinstance(key,str) or not key or key in rates:
                raise ValueError('Invalid price context')
            rates[key]=(row['markup_pct'],row['promo_pct'])
        prices={}
        for item in catalog:
            key=item['service_key']
            if key not in rates:
                raise ValueError('Incomplete price context')
            index=item['plan_index']
            if type(index) is not int or index<0 or (key,index) in prices:
                raise ValueError('Invalid catalog item')
            prices[(key,index)]=catalog_customer_price(item['base_price_rub'],*rates[key])
        return {'status':'available','prices':prices}
    except Exception as exc:
        # Do not expose DSNs, raw rows, SQL messages, or partner metadata.
        result['status']='not_configured' if getattr(exc,'sqlstate',None) in ('42883','42501') else 'read_error'
        return result
