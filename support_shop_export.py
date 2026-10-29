"""Authenticated, read-only customer-facing export from the running shop.

Dependencies are injected by the shop; this module never imports common/runtime,
opens orders, buys codes, returns procurement fields, or handles customer passwords.
"""
import asyncio
import copy
import hmac
import html
import json
import math
import os
import re
import time

from aiohttp import web


def _text(value, limit):
    return html.unescape(re.sub(r'<[^>]*>', '', str(value or '')))[:limit]


def _positive(value):
    if isinstance(value,bool):raise ValueError('Invalid price')
    number=float(value)
    if not math.isfinite(number) or number<=0 or number>100_000_000:
        raise ValueError('Invalid price')
    return int(number)


def make_support_catalog_handler(*, catalog, hidden, price_for, prepare_stock,
                                 stock, apple_categories, stock_age,
                                 folder_for_category, markup_for_category,
                                 usd_rate, calc_price, workflow_snapshot=None, bot_identity=None):
    """All network/DB dependencies are read-only functions already used by the shop.

    catalog() is sync; hidden/price_for/prepare_stock/stock/markup_for_category/
    usd_rate are async. Remaining functions are sync. Auth is disabled unless an
    owner configured a separate token of at least 32 characters in both services.
    """
    gate=asyncio.Semaphore(2)
    pricing_gate=asyncio.Semaphore(8)
    identity_gate=asyncio.Lock()
    identity_cache={'until':0,'value':None}

    async def navigation():
        if bot_identity is None:return {'status':'not_connected'}
        async with identity_gate:
            if identity_cache['until']>time.monotonic():
                return dict(identity_cache['value'])
            try:
                value=await asyncio.wait_for(bot_identity(),timeout=3)
                if (not isinstance(value,dict) or not isinstance(value.get('username'),str)
                        or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',value['username'])
                        or type(value.get('catalog_webapp')) is not bool):
                    return {'status':'invalid_response'}
                result={'status':'available','username':value['username'],
                        'catalog_webapp':value['catalog_webapp']}
                identity_cache.update(until=time.monotonic()+300,value=result)
                return dict(result)
            except asyncio.TimeoutError:return {'status':'timeout'}
            except Exception:return {'status':'read_error'}

    async def regular(uid, hidden_keys):
        rows=[];truncated=False
        source=catalog()
        if not isinstance(source,dict):raise ValueError('Invalid catalogue')
        source=copy.deepcopy(source)
        workflows={}
        workflow_status='not_connected'
        if workflow_snapshot is not None:
            try:
                workflows=await asyncio.wait_for(workflow_snapshot(source),timeout=5)
                workflow_status='available'
                if not isinstance(workflows,dict):
                    workflows={};workflow_status='invalid_response'
            except asyncio.TimeoutError:
                workflow_status='timeout'
            except Exception:
                workflows={};workflow_status='read_error'
        async def priced(key,index,service,plan):
            base=_positive(plan.get('price'))
            row={'service_key':_text(key,100),'plan_index':index,
                 'service':_text(service.get('name',key),160),
                 'plan':_text(plan.get('name'),160),
                 'service_description':_text(service.get('desc'),1500),
                 'description':_text(plan.get('desc'),1500),
                 'catalog_price_rub':base,
                 'price_scope':'public_catalog_before_personal_adjustments',
                 'stock':'not_checked','fulfillment':'not_verified'}
            from support_fulfillment import METHODS, REQUIRED_FIELDS
            detail=workflows.get((key,index))
            method=detail.get('method') if isinstance(detail,dict) else detail
            if method in METHODS:
                row['fulfillment']=method
                row['fulfillment_source']='live_shop_settings_and_code'
            if method=='account_details':
                row.update(required_fields_status='not_verified',required_fields=[])
                if isinstance(detail,dict):
                    fields=detail.get('required_fields')
                    if (detail.get('required_fields_status')=='available'
                            and detail.get('required_fields_source')=='live_shop_creds_instructions'
                            and isinstance(fields,list) and fields
                            and all(isinstance(f,str) and f in REQUIRED_FIELDS for f in fields)
                            and len(fields)==len(set(fields))):
                        row.update(required_fields_status='available',required_fields=list(fields),
                                   required_fields_source='live_shop_creds_instructions')
            try:
                async with pricing_gate:
                    quote=await price_for(uid,key,base)
                row['customer_price_rub']=_positive(quote)
                row['price_scope']='customer_catalog_before_checkout_coupon_and_coins'
            except Exception:
                # Public price remains usable; never invent a personal quote.
                pass
            return row
        for key,service in source.items():
            if key in hidden_keys or not isinstance(service,dict):continue
            for index,plan in enumerate(service.get('plans') or []):
                if not isinstance(plan,dict):continue
                try:_positive(plan.get('price'))
                except (ValueError,TypeError,OverflowError):continue
                if len(rows)>=500:
                    truncated=True;break
                rows.append((key,index,service,dict(plan)))
            if truncated:break
        result=await asyncio.gather(*(priced(*row) for row in rows))
        if workflow_status=='available' and any(r['fulfillment']=='not_verified' for r in result):
            workflow_status='partial'
        return {'catalog_status':'truncated' if truncated else 'available',
                'workflow_status':workflow_status,
                'catalog_complete':not truncated,'catalog':result}

    async def apple(hidden_keys):
        instructions=[
            'Пополнение выполняется подарочным кодом Apple. Регион кода должен совпадать с регионом Apple ID.',
            'Для подбора номинала уточни регион Apple ID, валюту и цену нужной покупки, а также текущий баланс. Не проси пароль или данные карты.',
            'Номинал карты и стоимость покупки в рублях — разные суммы. Выбирай номинал в валюте аккаунта с учётом недостающего баланса.',
            'Не обещай оплату любой подписки балансом Apple: сначала проверь, продаётся ли она через App Store и допускает ли такой способ оплаты.',
            'Погашение кода: App Store → аватар → «Погасить подарочную карту или код» → «Ввести код вручную».',
            'Цена ниже — прайс магазина до введённого при оплате промокода и списания монет. Наличие и цену окончательно проверяет оформление заказа.',
        ]
        result={'status':'unavailable','stock_age_seconds':-1,'regions':[],
                'instructions':instructions}
        if 'appstore' in hidden_keys:
            result['status']='hidden';return result
        await prepare_stock()
        data=await stock()
        if not isinstance(data,dict) or not data:return result
        age=stock_age()
        if type(age) is not int or age<0:return result
        result['stock_age_seconds']=age
        if age>=1800:
            result['status']='stale';return result
        rate=await usd_rate()
        if not math.isfinite(float(rate)) or float(rate)<=0:raise ValueError('Invalid rate')
        count=0;truncated=False
        for category in apple_categories(data):
            cat_id=category['category_id']
            # Match api_appstore_regions_handler: the dedicated Apple section
            # does not depend on generic gift-card folder membership/visibility.
            # Its own shop_hidden['appstore'] gate above remains authoritative.
            if len(result['regions'])>=50:
                truncated=True;break
            markup=await markup_for_category(data,cat_id,category)
            region={'region_id':int(cat_id),'name':_text(category.get('category_name'),160),'denoms':[]}
            for service in category.get('services',[]):
                if not isinstance(service,dict) or float(service.get('in_stock') or 0)<=0:continue
                if count>=500 or len(region['denoms'])>=100:
                    truncated=True;break
                price=_positive(calc_price(service['price'],rate,markup))
                nominal=_text(service.get('service_name'),160).split('|')[-1].strip()
                region['denoms'].append({'nominal':nominal,'price_rub':price});count+=1
            if region['denoms']:result['regions'].append(region)
            if count>=500:break
        result['status']='truncated' if truncated else 'stale' if age>=1800 else 'available'
        return result

    async def handler(request):
        token=os.getenv('SUPPORT_SHOP_READ_TOKEN','')
        if len(token)<32:return web.json_response({'ok':False,'error':'disabled'},status=503)
        supplied=request.headers.get('Authorization','')
        if len(supplied)>512 or not hmac.compare_digest(supplied.encode(),('Bearer '+token).encode()):
            return web.json_response({'ok':False,'error':'auth'},status=403)
        try:
            raw=bytearray()
            async for chunk in request.content.iter_chunked(1024):
                raw.extend(chunk)
                if len(raw)>4096:return web.json_response({'ok':False,'error':'size'},status=413)
            body=json.loads(raw)
            uid=body.get('uid') if isinstance(body,dict) else None
            if type(uid) is not int or not 0<uid<2**63:
                return web.json_response({'ok':False,'error':'uid'},status=400)
        except (ValueError,UnicodeDecodeError):
            return web.json_response({'ok':False,'error':'body'},status=400)
        # Bound parallel exports and acquisition; no unbounded queue or work.
        try:await asyncio.wait_for(gate.acquire(),timeout=1)
        except asyncio.TimeoutError:return web.json_response({'ok':False,'error':'busy'},status=429)
        try:
            result={'ok':True,'observed_at_unix':time.time(),
                    'catalog_status':'read_error','catalog_complete':False,'catalog':[],
                    'appstore':{'status':'read_error','stock_age_seconds':-1,'regions':[]}}
            try:
                hidden_keys=await asyncio.wait_for(hidden(),timeout=5)
                if not isinstance(hidden_keys,(list,set,tuple)) or not all(isinstance(k,str) for k in hidden_keys):
                    raise ValueError('Invalid hidden catalogue')
            except Exception:return web.json_response(result)
            values=await asyncio.gather(
                asyncio.wait_for(regular(uid,set(hidden_keys)),timeout=15),
                asyncio.wait_for(apple(set(hidden_keys)),timeout=15),
                navigation(),return_exceptions=True)
            if isinstance(values[0],dict):result.update(values[0])
            if isinstance(values[1],dict):result['appstore']=values[1]
            result['bot_navigation']=values[2] if isinstance(values[2],dict) else {'status':'read_error'}
            encoded=json.dumps(result,ensure_ascii=False,allow_nan=False)
            if len(encoded.encode())>1_500_000:
                return web.json_response({'ok':False,'error':'response_size'},status=503)
            return web.Response(text=encoded,content_type='application/json',headers={'Cache-Control':'no-store'})
        finally:gate.release()
    return handler
