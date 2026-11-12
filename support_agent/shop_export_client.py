"""Read only the owner's authenticated shop export; never accept client URLs."""
import json
import asyncio
import re
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
    nav=data.get('bot_navigation',{})
    nav_status=nav.get('status','not_connected') if isinstance(nav,dict) else 'invalid_response'
    if nav_status not in ('available','not_connected','timeout','read_error','invalid_response'):
        nav_status='invalid_response'
    result['bot_navigation']={'status':nav_status}
    if nav_status=='available':
        if (not isinstance(nav.get('username'),str)
                or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',nav['username'])
                or type(nav.get('catalog_webapp')) is not bool):
            result['bot_navigation']={'status':'invalid_response'}
        else:
            result['bot_navigation'].update(username=nav['username'],catalog_webapp=nav['catalog_webapp'])
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
                reasons={'manual_not_projected','code_steps_not_projected','invalid_instruction_type',
                         'default_fields_changed','custom_fields_not_projected','fields_confirmed',
                         'default_steps_unavailable','default_steps_changed','custom_steps_not_projected','steps_confirmed'}
                if row.get('workflow_reason') in reasons:item['workflow_reason']=row['workflow_reason']
                if method=='checkout_link':
                    allowed_steps=['open_service','login_own_account','open_plans','select_plan',
                                   'open_checkout','copy_checkout_url','send_checkout_url']
                    item.update(workflow_steps_status='not_verified',workflow_steps=[])
                    if (row.get('workflow_steps_status')=='available'
                            and row.get('workflow_steps_source')=='live_shop_linkpay_instructions'
                            and row.get('workflow_steps')==allowed_steps):
                        item.update(workflow_steps_status='available',workflow_steps=allowed_steps,
                                    workflow_steps_source='live_shop_linkpay_instructions')
                if method=='account_details':
                    item.update(required_fields_status='not_verified',required_fields=[])
                    fields=row.get('required_fields')
                    if (row.get('required_fields_status')=='available'
                            and row.get('required_fields_source')=='live_shop_creds_instructions'
                            and isinstance(fields,list) and fields
                            and all(isinstance(f,str) and f in ('account_email','account_password') for f in fields)
                            and len(fields)==len(set(fields))):
                        item.update(required_fields_status='available',required_fields=list(fields),
                                    required_fields_source='live_shop_creds_instructions')
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
    result['generation']=clean_generation(data.get('generation'))
    return result


def clean_generation(raw):
    """Credit packs and enabled models of the bot's own generation sections."""
    if not isinstance(raw,dict) or raw.get('status')!='available':
        return {'status':(raw or {}).get('status','not_connected') if isinstance(raw,dict) else 'not_connected'}
    try:
        packs=[]
        for row in (raw.get('packs') or [])[:30]:
            price=row.get('price_rub');credits=row.get('credits')
            if type(price) is not int or price<=0 or type(credits) is not int or credits<=0:continue
            packs.append({'name':scrub(str(row.get('name','')))[:80],'credits':credits,'price_rub':price,
                          'note':scrub(str(row.get('note','')))[:160]})
        sections=[]
        for sec in (raw.get('sections') or [])[:6]:
            models=[]
            for m in (sec.get('models') or [])[:60]:
                if type(m.get('credits')) is not int or m['credits']<=0:continue
                models.append({'name':scrub(str(m.get('name','')))[:80],'credits':m['credits'],
                               'note':scrub(str(m.get('note','')))[:200],'quality':scrub(str(m.get('quality','')))[:80]})
            sections.append({'menu':scrub(str(sec.get('menu','')))[:60],'title':scrub(str(sec.get('title','')))[:60],
                             'models':models})
        return {'status':'available','buy_menu':scrub(str(raw.get('buy_menu','')))[:60],'packs':packs,'sections':sections}
    except (TypeError,AttributeError):
        return {'status':'read_error'}


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


_EXPORT_REASONS={
    'not_configured':'Подключение живого экспорта не настроено.',
    'configuration_incomplete':'Не задан URL магазина или отдельный токен доступа.',
    'invalid_url':'URL магазина некорректен; требуется HTTPS и адрес без пути, параметров и встроенных учётных данных.',
    'invalid_token':'Отдельный токен доступа имеет неверный формат или длину.',
    'invalid_uid':'Идентификатор клиента должен быть положительным целым числом.',
    'permission_denied':'Магазин отклонил авторизацию или доступ. Повтор запроса не выполняется.',
    'remote_disabled':'Магазин сообщил, что живой экспорт отключён или его токен не настроен.',
    'remote_response_size':'Магазин отказал из-за превышения размера экспорта.',
    'http_failure':'Получен неуспешный HTTP-статус. Первопричина этим ответом не установлена.',
    'gateway_failure':'Получен HTTP-сбой шлюза или временная недоступность. Первопричина не установлена.',
    'timeout':'Истекло ограниченное время чтения живого экспорта.',
    'connection_error':'Не удалось установить или сохранить сетевое соединение с магазином.',
    'tls_error':'Не удалось проверить защищённое соединение с магазином.',
    'invalid_json':'Успешный HTTP-ответ не содержит корректный JSON экспорта.',
    'response_too_large':'Ответ превысил ограничение 1500000 байт.',
    'expired_export':'Время снимка экспорта не прошло проверку актуальности.',
    'invalid_export':'Структура или обязательные поля экспорта не прошли проверку.',
    'read_error':'Чтение экспорта прервано; конкретная первопричина не установлена.',
}


def _export_failure(status,reason,attempts,http_status=None):
    result={'live_shop_status':status,'live_shop_reason':reason,'live_shop_attempts':attempts}
    result['live_shop_diagnostic']=('Этап: чтение живого экспорта магазина. '
        +_EXPORT_REASONS[reason]+(' HTTP='+str(http_status)+'.' if http_status is not None else '')
        +' Попыток='+str(attempts)+'.')
    return result


async def _app_503_reason(response):
    """Read a tiny bounded error envelope; return only known public enum values."""
    try:
        raw=bytearray()
        async for chunk in response.content.iter_chunked(512):
            raw.extend(chunk)
            if len(raw)>1024:return None
        value=json.loads(raw)
        if isinstance(value,dict) and value.get('ok') is False:
            return {'disabled':'remote_disabled','response_size':'remote_response_size'}.get(value.get('error'))
    except (ValueError,TypeError,AttributeError):
        pass
    return None


async def fetch_export(base_url,token,uid):
    """Two bounded read-only attempts for gateway failures, never for auth/data errors."""
    if not base_url and not token:return _export_failure('not_connected','not_configured',0)
    if not base_url or not token:return _export_failure('configuration_error','configuration_incomplete',0)
    # The store caps the entire Authorization header at 512 characters.
    if (not isinstance(token,str) or not 32<=len(token)<=505 or not token.isascii()
            or any(ord(char)<=32 or ord(char)>=127 for char in token)):
        return _export_failure('configuration_error','invalid_token',0)
    try:
        if not isinstance(base_url,str) or base_url!=base_url.strip() or any(ord(c)<=32 for c in base_url):
            raise ValueError()
        url=urlsplit(base_url)
        if (url.scheme!='https' or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ('','/')):
            raise ValueError()
        url.port  # Validate malformed/out-of-range ports before any network I/O.
    except (ValueError,TypeError,AttributeError):
        return _export_failure('configuration_error','invalid_url',0)
    if type(uid) is not int or not 0<uid<2**63:return _export_failure('invalid_request','invalid_uid',0)
    attempts=0;last_status=None;previous_status=None
    deadline=time.monotonic()+25
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            for attempt in range(1,3):
                remaining=deadline-time.monotonic()
                if remaining<=0:return _export_failure('timeout','timeout',attempts,last_status)
                attempts=attempt
                # The store may legitimately spend 5s reading visibility plus
                # 15s on independent sections. Do not shorten that path to 12s.
                timeout=aiohttp.ClientTimeout(total=min(24,remaining))
                async with session.post(base_url.rstrip('/')+'/api/support/catalog',
                        headers={'Authorization':'Bearer '+token},json={'uid':uid},allow_redirects=False,
                        timeout=timeout) as response:
                    status=response.status;last_status=status
                    if status!=200:
                        reason=await _app_503_reason(response) if status==503 else None
                        if reason:return _export_failure('http_503',reason,attempts,status)
                        transient=status in (502,503,504)
                        if not transient or attempt==2:
                            reason='permission_denied' if status in (401,403) else 'gateway_failure' if transient else 'http_failure'
                            return _export_failure('http_'+str(status),reason,attempts,status)
                        previous_status=status
                    else:
                        raw=bytearray()
                        async for chunk in response.content.iter_chunked(16384):
                            raw.extend(chunk)
                            if len(raw)>1_500_000:return _export_failure('invalid_response','response_too_large',attempts,status)
                        try:data=json.loads(raw)
                        except (ValueError,UnicodeDecodeError):return _export_failure('invalid_response','invalid_json',attempts,status)
                        try:result=clean_export(data)
                        except (ValueError,TypeError,KeyError,AttributeError) as error:
                            reason='expired_export' if isinstance(error,ValueError) and str(error)=='Expired export' else 'invalid_export'
                            return _export_failure('invalid_response',reason,attempts,status)
                        result.update(live_shop_attempts=attempts)
                        if attempts>1:
                            result.update(live_shop_reason='recovered_after_gateway_failure',
                                live_shop_previous_http_status=previous_status,
                                live_shop_diagnostic='Этап: чтение живого экспорта магазина. Повторное чтение после HTTP='+str(previous_status)+' выполнено успешно. Первопричина первоначального HTTP-сбоя не установлена. Попыток='+str(attempts)+'.')
                        return result
                # Never hold a failed response open during the bounded backoff.
                if deadline-time.monotonic()<=0.3:return _export_failure('timeout','timeout',attempts,last_status)
                await asyncio.sleep(0.3)
    except asyncio.TimeoutError:return _export_failure('timeout','timeout',attempts,last_status)
    except (aiohttp.ClientConnectorCertificateError,aiohttp.ClientSSLError):
        return _export_failure('connection_error','tls_error',attempts,last_status)
    except aiohttp.ClientConnectionError:return _export_failure('connection_error','connection_error',attempts,last_status)
    except Exception:return _export_failure('read_error','read_error',attempts,last_status)
