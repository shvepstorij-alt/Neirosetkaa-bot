"""Owner-only smoke probe; no client messages, orders or history are used."""
import html
from .research import lookup, attach_evidence, failure_details


async def run_probe(service):
    revision=service.store.get('revision')
    def charge():
        if service.store.get('revision')!=revision:
            raise ValueError('Stale research job')
        service.charge_call()
    stage='search'
    try:
        charge()
        evidence=await lookup(service.ai,{'topic':'plans','products':['zoom','krea','lovable']},charge_extra=charge)
        lines=['Проверка интернета · Zoom / Krea / Lovable']
        for name in ('zoom','krea','lovable'):
            error=evidence.get('failures',{}).get(name)
            lines.append(name+': '+(error if error else 'источники найдены'))
        await service.owner_message('\n'.join(lines))
        facts={'catalog':[],'catalog_status':'not_connected','public_research':evidence,
               'note':'Это тест без магазина. Только официальные цены и возможности. Не обещать оформление через магазин.'}
        charge();stage='final_draft'
        answer=await service.ai.draft([{'role':'client','text':'Какие официальные тарифы и основные ограничения у Zoom, Krea и Lovable?'}],facts)
        stage='citations'
        checked=attach_evidence(dict(answer),evidence,allow_catalog_only=bool(evidence.get('failures')))
        charge();stage='answer_check'
        review=await service.ai.check_answer(answer,facts)
        if service.store.get('revision')!=revision:return
        text=('Проверка содержания: '+('пройдена' if review['supported'] else 'не пройдена')
              +'\n'+review['reason']+'\n\nПробный ответ (клиентам не отправлялся):\n'+checked['reply'])
        await service.owner_message(html.escape(text),parse_mode='HTML')
    except Exception as exc:
        if str(exc)=='Stale research job' or service.store.get('revision')!=revision:return
        await service.owner_message('Тест остановлен. '+failure_details(exc,stage))
