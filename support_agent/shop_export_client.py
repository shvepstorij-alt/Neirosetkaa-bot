"""Read only the owner's authenticated shop export; never accept client URLs."""
import json
import time
from urllib.parse import urlsplit
import aiohttp
from .privacy import scrub
from .catalog_context import apple_region_codes


def clean_export(data):
    if not isinstance(data,dict) or data.get('ok') is not True:raise ValueError('Invalid export')
    observed=data.get('observed_at_unix')
    if type(observed) not in (float,int) or not -5<=time.time()-observed<=60:
        raise ValueError('Expired export')
    result={'live_shop_status':'available','live_shop_observed_at':observed}
    status=data.get('catalog_status')
    if status in ('available','truncated'):
        rows=data.get('catalog')
        if not isinstance(rows,list) or len(rows)>500:raise ValueError('Invalid catalogue')
        cleaned=[]
        for row in rows:
            if not isinstance(row,dict):raise ValueError('Invalid row')
            price=row.get('catalog_price_rub')
            if type(price) is not int or price<=0:raise ValueError('Invalid price')
            item={key:scrub(str(row.get(key,'')))[:limit] for key,limit in
                  (('service_key',100),('service',160),('plan',160),('description',1500),('service_description',1500))}
            item.update(plan_index=row.get('plan_index'),catalog_price_rub=price,
                        price_scope='public_catalog_before_personal_adjustments',stock='not_checked',fulfillment='not_verified')
            method=row.get('fulfillment')
            if (method in ('manual','checkout_link','account_details','code_activation_conditional')
                    and row.get('fulfillment_source')=='live_shop_settings_and_code'):
                item.update(fulfillment=method,fulfillment_source='live_shop_settings_and_code')
            quote=row.get('customer_price_rub')
            if type(quote) is int and quote>0:
                item.update(customer_price_rub=quote,price_scope='customer_catalog_before_checkout_coupon_and_coins')
            cleaned.append(item)
        workflow_status=data.get('workflow_status','not_connected')
        if workflow_status not in ('available','partial','not_connected','invalid_response','timeout','read_error'):
            workflow_status='invalid_response'
        result.update(catalog=cleaned,catalog_status=status,catalog_complete=False,workflow_status=workflow_status,
                      customer_prices_status='available' if cleaned and all('customer_price_rub' in r for r in cleaned) else 'partial')
    try:result['appstore']=clean_apple(data.get('appstore',{}))
    except (ValueError,TypeError,KeyError,AttributeError):result['appstore']={'status':'read_error','regions':[]}
    return result


def clean_apple(apple):
    if not isinstance(apple,dict):raise ValueError('Invalid App Store')
    apple_status=apple.get('status','read_error')
    info={'status':apple_status,'regions':[],
          'instructions':[scrub(str(s))[:500] for s in apple.get('instructions',[])[:10]]}
    if apple_status in ('available','truncated'):
        age=apple.get('stock_age_seconds')
        if type(age) is not int or not 0<=age<1800:raise ValueError('Invalid stock age')
        info['stock_age_seconds']=age
        regions=apple.get('regions',[])
        if not isinstance(regions,list) or len(regions)>50:raise ValueError('Invalid regions')
        count=0
        for region in regions:
            denoms=[]
            for entry in region.get('denoms',[]):
                price=entry.get('price_rub')
                if type(price) is not int or price<=0:raise ValueError('Invalid denomination')
                denoms.append({'nominal':scrub(str(entry.get('nominal','')))[:160],'price_rub':price});count+=1
            if count>500:raise ValueError('Too many denominations')
            name=scrub(str(region.get('name','')))[:160]
            info['regions'].append({'name':name,'region_codes':sorted(apple_region_codes(name)),'denoms':denoms})
    return info


async def fetch_export(base_url,token,uid):
    if not base_url or not token:return {'live_shop_status':'not_connected'}
    try:
        url=urlsplit(base_url)
        if (url.scheme!='https' or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ('','/') or len(token)<32):
            return {'live_shop_status':'configuration_error'}
        timeout=aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(base_url.rstrip('/')+'/api/support/catalog',
                    headers={'Authorization':'Bearer '+token},json={'uid':uid},allow_redirects=False) as response:
                if response.status!=200:return {'live_shop_status':'http_'+str(response.status)}
                raw=bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    raw.extend(chunk)
                    if len(raw)>1_500_000:raise ValueError('Export too large')
                return clean_export(json.loads(raw))
    except Exception:
        return {'live_shop_status':'read_error'}
