"""Paid owner-triggered smoke conversations through Service.generate, never Telegram clients."""
import asyncio
import html
import time
from .storage import Store
from .conversation_style import decorate_reply, session_info


class NoTelegram:
    async def call(self, *args, **kwargs):
        raise RuntimeError('Test isolation: Telegram client operation forbidden')


class CatalogOnly:
    def __init__(self, shop, owner):
        self.shop, self.owner = shop, owner
        self.last_facts = {}

    async def facts(self, uid):
        # No client ID is queried. Do not put owner order history into the model.
        facts = await self.shop.facts(self.owner)
        allowed = ('catalog', 'catalog_status', 'catalog_complete', 'appstore', 'bot_navigation',
                   'live_shop_status', 'workflow_status', 'catalog_observed_at_unix', 'note', 'source')
        self.last_facts = {k: v for k, v in facts.items() if k in allowed}
        return self.last_facts


def report_details(answer, facts, question):
    from .catalog_context import annotate
    from .reply_audit import catalogue_audit
    from .privacy import scrub
    from .research import safe_url
    from .service_discovery import public_host
    evidence = answer.get('evidence', {})
    status = evidence.get('status', 'not_recorded')
    lines = ['Диагностика владельцу:', 'Статус доказательств: '+str(status),
             'Источники этого шага: каталог='+str(facts.get('source','unknown'))
             +'; живой экспорт='+str(facts.get('live_shop_status','not_connected'))
             +'; App Store='+str(facts.get('appstore',{}).get('status','not_connected'))
             +'; навигация='+str(facts.get('bot_navigation',{}).get('status','not_connected'))]
    checked_at=evidence.get('checked_at')
    if type(checked_at) in (int,float) and 0<checked_at<4102444800:
        import datetime as dt
        lines.append('Время поиска UTC: '+dt.datetime.fromtimestamp(checked_at,dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
    if evidence.get('diagnostic'):
        lines.append('Причина: '+scrub(str(evidence['diagnostic'])))
    if evidence.get('missing_products'):
        lines.append('Не покрыты сервисы: '+', '.join(map(str,evidence['missing_products'])))
    if evidence.get('failures'):
        lines.append('Ошибки источников: '+scrub(str(evidence['failures'])))
    if evidence.get('citation_mode')=='bibliography':
        lines.append('Ссылки перечислены списком; привязка ко всем фразам не подтверждена.')
    review=evidence.get('content_review')
    if isinstance(review,dict):
        lines.append('Проверка содержания: '+('пройдена моделью (не гарантия)' if review.get('supported') else 'отклонена'))
        lines.append(scrub(str(review.get('reason','Причина отсутствует'))))
    else:
        lines.append('Результат проверки содержания моделью не сохранён или модельная проверка не выполнялась.')
    if evidence.get('handoff_reason'):
        lines.append('Участие владельца: '+scrub(str(evidence['handoff_reason'])))
    if answer.get('handoff') and not any(evidence.get(k) for k in ('diagnostic','missing_products','failures','citation_mode','handoff_reason')):
        lines.append('Флаг участия владельца установлен; конкретная причина отдельно не записана. '
                     'Это не доказательство ошибки: ручное оформление также может требовать владельца.')
    selected=facts if 'requested_catalog' in facts else annotate(facts,question)
    if evidence.get('rejected_reply'):
        lines.append('Отклонённый вариант (клиенту не отправлялся):\n'+scrub(evidence['rejected_reply']))
        lines.append('Далее проверяется сохранённый безопасный ответ, а не отклонённый вариант.')
    audit=catalogue_audit(answer.get('reply',''),selected)
    lines.append(f"Сопоставлено рублёвых цен с именованными тарифами: {audit['checked_prices']}.")
    lines.extend(audit['issues'])
    if not audit['checked_prices']:
        from .reply_audit import MONEY
        lines.append('Обычных рублёвых цен для сопоставления нет.' if not MONEY.search(answer.get('reply','')) else
                     'Цены обычных тарифов этим правилом не проверены; региональный номинал App Store проверяется отдельно ниже, если определён.')
    elif not audit['issues']:
        lines.append('Расхождений в проверенных ценах/сроках не обнаружено; непроверенные фразы этим не подтверждаются.')
    from .apple_matching import exact_quote
    quote=exact_quote(selected,question)
    if quote:
        from .reply_audit import MONEY
        from decimal import Decimal
        import re
        amounts=[Decimal(re.sub(r'[ \u00a0\u202f]','',m[1]).replace(',','.')) for m in MONEY.finditer(answer.get('reply',''))]
        expected=quote.get('price')
        lines.append(f"App Store: регион={quote['region']}; номинал={quote['amount']} {quote['currency']}; подтверждённая цена={expected}; суммы ответа={amounts}; "
                     +('совпадение' if expected is not None and amounts and all(v==Decimal(str(expected)) for v in amounts) else 'требуется проверка; совпадение не установлено'))
    for source in evidence.get('sources',[]):
        url=source.get('url','') if isinstance(source,dict) else ''
        host=public_host(url)
        if host and safe_url(url,(host,)):
            lines.append('Источник ['+str(source.get('id','?'))+']: '+url)
    if evidence.get('sources'):
        lines.append('URL позволяют перепроверить страницы сейчас. Полные тексты страниц на момент поиска в этом отчёте не сохранены.')
    return '\n\n'.join(lines)


SCENARIOS = (
    ('Оформление и смена темы', (
        ('Привет!', 'Как оформить Krea? Какие тарифы есть и что нужно кроме оплаты?'),
        ('А какие тарифы в зуме?',),
        ('Что потребуется для оформления обоих сервисов?',))),
    ('App Store и уточнение', (
        ('Как пополнить App Store? У меня регион Германия.',),
        ('Сколько стоит 10 евро?',))),
    ('Настоящий поиск и сравнение', (
        ('Какие возможности у платных тарифов Zoom и Krea? Сравни по официальным сведениям, а цены назови из магазина.',),)),
)


async def run_probe(service, max_calls=20):
    from .service import Service
    revision = service.store.get('revision')
    calls = 0
    def charge():
        nonlocal calls
        if service.store.get('revision') != revision:
            raise RuntimeError('Test stopped: owner changed mode')
        if calls >= max_calls:
            raise RuntimeError('Test API call limit reached')
        service.charge_call()
        calls += 1

    store = Store(':memory:', batch_seconds=300)
    shop=CatalogOnly(service.shop, service.owner)
    isolated = Service(store, NoTelegram(), service.ai, shop, 0, {-1})
    isolated._probe_capture=True
    isolated.charge_call = charge
    store.switch('draft')
    store.connection('test-only', True, True)
    stage = 'инициализация'
    try:
        for index, (name, turns) in enumerate(SCENARIOS):
            cid = None
            mid = 0
            for turn, fragments in enumerate(turns, 1):
                isolated._probe_facts={}
                isolated._probe_question=''
                stage = f'сценарий {index+1}, шаг {turn}, подготовка ответа'
                if service.store.get('revision') != revision:
                    raise RuntimeError('Test stopped: owner changed mode')
                for fragment in fragments:
                    mid += 1
                    cid = store.ingest('test-only', -index-1, mid, 'client', fragment, {}, time.time())
                store.prepare_now(cid, time.time())
                job = store.claim(time.time())
                if not job:
                    raise RuntimeError('Test queue: synthetic batch not claimed')
                answer = await asyncio.wait_for(isolated.generate(job), timeout=180)
                if not answer:
                    raise RuntimeError('Test generation: no current answer returned')
                first = store.chat(cid)['batch_first']
                reply = decorate_reply(answer['reply'], session_info(store.db, cid, first, mid))
                store.finish(job, reply, answer.get('summary', ''), answer.get('handoff', True))
                # Simulate a delivered reply only in isolated memory, no send API.
                mid += 1
                store.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                                 (cid, mid, 'assistant', reply, '{}', time.time(), 'stored'))
                label = 'НУЖНА ПРОВЕРКА' if answer.get('handoff') else 'ПОДГОТОВЛЕН (не гарантия точности)'
                report = (f'Тест {index+1}, шаг {turn}: {name}\n{label}\n\n'
                          +'Вопрос:\n'+'\n'.join(fragments)+'\n\nОтвет:\n'+reply
                          +'\n\nРазбор:\n'+str(answer.get('summary', '')))
                selected=isolated._probe_facts or shop.last_facts
                question=isolated._probe_question or '\n'.join(fragments)
                report+='\n\n'+report_details(answer,selected,question)
                await service.owner_message(html.escape(report), parse_mode='HTML')
        await service.owner_message(f'Прогон завершён. Учтённых этапов API: {calls}/{max_calls}. '
                                    'Часть ответов могла быть подготовлена без модели. '
                                    'Клиентам ничего не отправлено. Семантическую точность нужно оценить по отчёту.')
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Do not expose arbitrary exception messages, credentials or raw payloads.
        known = ('Test stopped: owner changed mode', 'Test API call limit reached',
                 'Test queue: synthetic batch not claimed', 'Test generation: no current answer returned',
                 'Daily API request limit reached')
        reason = str(exc) if str(exc) in known else ('превышено время этапа 180 секунд' if isinstance(exc, TimeoutError)
                 else 'первопричина не установлена; произвольный текст исключения скрыт')
        await service.owner_message(f'Прогон остановлен. Этап: {stage}; причина: {reason}; '
                                    f'тип: {type(exc).__name__}; учтённых этапов API: {calls}/{max_calls}. '
                                    'Полный прогон не завершён. Первопричину смотрите в диагностике приложения.')
    finally:
        store.db.close()
