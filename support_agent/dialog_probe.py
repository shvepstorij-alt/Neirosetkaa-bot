"""Paid owner-triggered smoke conversations through Service.generate, never Telegram clients."""
import asyncio
import html
import time
from .storage import Store
from .conversation_style import decorate_reply, session_info


class NoTelegram:
    def __init__(self,fixtures=None):
        self.fixtures=fixtures or {}

    async def download(self,file_id):
        if file_id not in self.fixtures:
            raise RuntimeError('Test isolation: non-fixture download forbidden')
        return self.fixtures[file_id]

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
                   'live_shop_status', 'live_shop_diagnostic','live_shop_attempts','live_shop_reason',
                   'workflow_status', 'catalog_observed_at_unix', 'note', 'source', 'generation')
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
    turn=facts.get('conversation_turn',{})
    if turn.get('version')==1:
        lines.append('Этап: текущий вопрос. Целей='+','.join(turn.get('goals',[]))
                     +'; тип='+str(turn.get('current_act','unspecified'))
                     +'; связь='+str(turn.get('relation','current_request'))
                     +'; источник намерения mid='+str(turn.get('source_mid'))
                     +'; вопросов с привязкой к товару='+str(len(turn.get('requests',[])))+'.')
    from .response_decision import verified_atomic_decision
    decision=verified_atomic_decision(answer,evidence)
    if decision:
        lines.append('Этап: решение об ответе. Отправляемый текст подтверждён; полное покрытие='
                     +str(decision['coverage_complete'])+'; действие владельца='
                     +str(decision['owner_action_required'])+'. Неполнота отделена от достоверности текста.')
    from .auto_policy import block_reason
    auto_reason=block_reason(answer,evidence,current_text=turn.get('current_text',question))
    lines.append('Допуск содержания AUTO: '+(auto_reason or 'пройден; права и фактическая доставка этим прогоном не проверяются.'))
    if facts.get('live_shop_diagnostic'):
        lines.append('Диагностика подключения магазина: '+scrub(str(facts['live_shop_diagnostic']))[:700])
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
        label=('собран частичный ответ из принятых утверждений; исходный ответ целиком не подтверждён'
               if review.get('partial') else 'пройдена моделью (не гарантия)' if review.get('supported') else 'отклонена')
        lines.append('Проверка содержания: '+label)
        lines.append(scrub(str(review.get('reason','Причина отсутствует'))))
        for diagnostic in evidence.get('atomic_diagnostics',[]):
            lines.append('Проверка утверждения: '+scrub(str(diagnostic)))
    else:
        lines.append('Результат проверки содержания моделью не сохранён или модельная проверка не выполнялась.')
    if evidence.get('handoff_reason'):
        lines.append('Участие владельца: '+scrub(str(evidence['handoff_reason'])))
    if answer.get('handoff') and not any(evidence.get(k) for k in ('diagnostic','missing_products','failures','citation_mode','handoff_reason')):
        lines.append('Флаг участия владельца установлен; конкретная причина отдельно не записана. '
                     'Это не доказательство ошибки: ручное оформление также может требовать владельца.')
    selected=facts if 'requested_catalog' in facts else annotate(facts,question)
    from .new_account_guide import selection_diagnostic
    sku_trace=selection_diagnostic(selected,question)
    if sku_trace:lines.append(sku_trace)
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
    from .reply_audit import apple_audit
    apple=apple_audit(answer.get('reply',''),selected,question)
    quote=apple['quote']
    if quote:
        amounts=apple['prices']
        expected=quote.get('price')
        lines.append(f"App Store: регион={quote['region']}; номинал={quote['amount']} {quote['currency']}; подтверждённая цена={expected}; суммы ответа={amounts}; "
                     +('совпадение' if expected is not None and amounts and not apple['issues'] else 'требуется проверка; совпадение не установлено'))
    sources=evidence.get('sources',[])
    selected_ids=set(evidence.get('selected_source_ids',answer.get('source_ids',[])))
    ordered=sorted(sources,key=lambda s:s.get('id') not in selected_ids) if all(isinstance(s,dict) for s in sources) else sources
    for source in ordered[:12]:
        url=source.get('url','') if isinstance(source,dict) else ''
        host=public_host(url)
        if host and safe_url(url,(host,)):
            lines.append('Источник ['+str(source.get('id','?'))+']: '+url)
    if evidence.get('sources'):
        lines.append('URL позволяют перепроверить страницы сейчас. Полные тексты страниц на момент поиска в этом отчёте не сохранены.')
    if len(sources)>12:lines.append('Показаны первые 12 источников (сначала использованные в ответе) из '+str(len(sources))+'.')
    if evidence.get('public_digest'):
        lines.append('Сводка публичного поиска — данные модели поиска, а не сохранённые тексты страниц и не гарантия точности:')
        digest=evidence['public_digest']
        lines.extend(digest[i:i+1000] for i in range(0,len(digest),1000))
        if evidence.get('public_digest_truncated'):lines.append('Сводка сокращена до 6000 символов.')
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


async def run_probe(service, max_calls=20, emit=None, expected_revision=None, suite='baseline'):
    from .service import Service
    revision = service.store.get('revision') if expected_revision is None else expected_revision
    calls = 0
    quota_exhausted = False
    async def send(text,parse_mode=None):
        if emit is not None:
            await emit(html.unescape(text) if parse_mode=='HTML' else text)
        else:
            await service.owner_message(text,parse_mode=parse_mode)
    def charge():
        nonlocal calls,quota_exhausted
        if service.store.get('revision') != revision:
            raise RuntimeError('Test stopped: owner changed mode')
        if calls >= max_calls:
            raise RuntimeError('Test API call limit reached')
        try:
            service.charge_call()
        except ValueError as exc:
            if str(exc)=='Daily API request limit reached':quota_exhausted=True
            raise
        calls += 1

    from .coverage_cases import SUITES, EXPECTATION
    if suite not in ('baseline','acceptance',*SUITES):raise ValueError('Unknown fixed test suite')
    scenarios=SCENARIOS;files={};expectations={}
    if suite=='acceptance':
        from .acceptance_cases import cases,fixtures,EXPECTATIONS
        scenarios=cases();files=fixtures();expectations=EXPECTATIONS
    elif suite in SUITES:
        scenarios=SUITES[suite]
        expectations={name:EXPECTATION for name,_ in scenarios}
    store = Store(':memory:', batch_seconds=300)
    shop=CatalogOnly(service.shop, service.owner)
    isolated = Service(store, NoTelegram(files), service.ai, shop, 0, {-1})
    isolated._probe_capture=True
    isolated.charge_call = charge
    store.switch('draft')
    store.connection('test-only', True, True)
    stage = 'инициализация'
    try:
        for index, (name, turns) in enumerate(scenarios):
            cid = None
            mid = 0
            for turn, fragments in enumerate(turns, 1):
                isolated._probe_facts={}
                isolated._probe_question=''
                stage = f'сценарий {index+1}, шаг {turn}, подготовка ответа'
                if service.store.get('revision') != revision:
                    raise RuntimeError('Test stopped: owner changed mode')
                fragment_texts=[]
                for fragment in fragments:
                    mid += 1
                    media={};text=fragment
                    if isinstance(fragment,dict):
                        key=fragment['photo'];text=fragment['text']
                        media={'kind':'photo','file_id':key,'file_size':len(files[key])}
                    fragment_texts.append(text)
                    # Mirror production intake for fixed public-site fixtures.
                    # No arbitrary remote prompts, URL fetch or customer files.
                    from .external_quote import public_sites
                    from .privacy import sensitive,scrub
                    if sensitive(text):
                        text='[Сообщение скрыто: возможны данные доступа; нужна ручная проверка]'
                        media={'kind':'sensitive'}
                    else:
                        sites=public_sites(text)
                        if sites:media['public_service_sites']=sites
                        text=scrub(text)
                    cid = store.ingest('test-only', -index-1, mid, 'client', text, media, time.time())
                store.prepare_now(cid, time.time())
                job = store.claim(time.time())
                if not job:
                    raise RuntimeError('Test queue: synthetic batch not claimed')
                answer = await asyncio.wait_for(isolated.generate(job), timeout=180)
                # generate() may turn a resource failure into a safe client
                # fallback. That is not a completed semantic test of this turn.
                if quota_exhausted:raise RuntimeError('Daily API request limit reached')
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
                          +'Вопрос:\n'+'\n'.join(fragment_texts)+'\n\nОтвет:\n'+reply
                          +'\n\nРазбор:\n'+str(answer.get('summary', '')))
                if name in expectations:
                    report+='\n\nКритерий ручной оценки (не автоматическое заключение):\n'+expectations[name]
                selected=isolated._probe_facts or shop.last_facts
                question=isolated._probe_question or '\n'.join(fragment_texts)
                report+='\n\n'+report_details(answer,selected,question)
                await send(html.escape(report), parse_mode='HTML')
        await send(f'Прогон завершён. Учтённых этапов API: {calls}/{max_calls}. '
                                    'Часть ответов могла быть подготовлена без модели. '
                                    'Клиентам ничего не отправлено. Семантическую точность нужно оценить по отчёту.')
        return {'status':'completed','calls':calls}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Do not expose arbitrary exception messages, credentials or raw payloads.
        known = ('Test stopped: owner changed mode', 'Test API call limit reached',
                 'Test queue: synthetic batch not claimed', 'Test generation: no current answer returned',
                 'Daily API request limit reached')
        reason = str(exc) if str(exc) in known else ('превышено время этапа 180 секунд' if isinstance(exc, TimeoutError)
                 else 'первопричина не установлена; произвольный текст исключения скрыт')
        from .clients import RemoteError
        if isinstance(exc,RemoteError):
            from .research import failure_details
            reason=failure_details(exc,'draft')
        await send(f'Прогон остановлен. Этап: {stage}; причина: {reason}; '
                                    f'тип: {type(exc).__name__}; учтённых этапов API: {calls}/{max_calls}. '
                                    'Полный прогон не завершён. Первопричину смотрите в диагностике приложения.')
        return {'status':'failed','calls':calls,'stage':stage,'reason':reason}
    finally:
        store.db.close()
