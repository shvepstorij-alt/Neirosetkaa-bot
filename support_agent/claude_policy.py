"""Owner-confirmed Claude risk disclosures, separate from product feature research."""
import re

SOURCE='https://support.claude.com/en/articles/8461763-where-can-i-access-claude'
PURCHASE_DISCLOSURE_MARKER='Перед оформлением Claude важное предупреждение'


def _claude_purchase_request(text, facts):
    """Whether this turn moves toward a Claude purchase, not mere app help.

    The warning is an owner condition for selling Claude through this shop.  It
    is deliberately separate from public facts about how Claude itself works.
    """
    value=str(text or '')
    named=bool(re.search(r'claude|клод',value,re.I))
    rows=facts.get('requested_catalog',[]) if isinstance(facts,dict) else []
    contextual=bool(rows) and any(str(row.get('service_key','')).casefold()=='claude' or
                                  re.search(r'claude|клод',str(row.get('service','')),re.I)
                                  for row in rows)
    if not (named or contextual):return False
    if re.search(r'(?:скача|установ)[а-яё]*|приложени|android|андроид|iphone|айфон|компьютер',value,re.I):
        return False
    return bool(re.search(r'оформ|оплат|куп|подписк|тариф|план|стоим|стоит|сколько|цен|продл|хочу',value,re.I))


def _accepts_disclosure(text):
    value=str(text or '').strip()
    return len(value)<=180 and bool(re.match(r'^(?:да|согласен|согласна|подтверждаю|понял(?:а)?|готов(?:а)?)(?:\b|[,.!\s])',value,re.I))


def claude_purchase_disclosure(facts,current,history=()):
    """Gate Claude purchase guidance until the client accepts owner terms."""
    rows=[row for row in list(history)[-40:] if isinstance(row,dict)]
    prior=next((row for row in reversed(rows)
                if row.get('role') in ('assistant','owner') and PURCHASE_DISCLOSURE_MARKER in str(row.get('text',''))),None)
    if prior and _accepts_disclosure(current):
        return None
    if not _claude_purchase_request(current,facts):
        return None
    if prior:
        body=('Чтобы продолжить оформление Claude, подтвердите, пожалуйста, что вы согласны с условиями из предыдущего сообщения.')
    else:
        body=(PURCHASE_DISCLOSURE_MARKER+':\n\n'
              'По условиям оформления через нас для Claude потребуется пройти верификацию личности. '
              'Для попытки прохождения можно использовать загранпаспорт РФ или водительское удостоверение РФ.\n\n'
              'Мы не можем гарантировать успешное прохождение верификации или то, что аккаунт не будет заблокирован системой Claude.\n\n'
              'Оформляя этот сервис, вы подтверждаете, что готовы при необходимости пройти верификацию личности Claude. '
              'Если верификацию не удастся пройти или аккаунт будет заблокирован, возврат по этому сервису не оформляется.\n\n'
              'Если условия подходят, напишите «согласен» — и помогу продолжить.')
    return {'reply':body,
            'summary':'Перед оформлением Claude показано обязательное условие владельца; инструкция оплаты и цена до согласия не выдавались.',
            'handoff':False,'source_ids':[],
            'evidence':{'status':'owner_policy','sources':[],
                        'diagnostic':'Этап: условия оформления Claude. Правило подтверждено владельцем 10.11.2026; это условие магазина, а не утверждение о публичном универсальном требовании Claude. До явного согласия клиента оформление не продолжается.'}}


def claude_policy_facts():
    return {
        'source':'owner_confirmed_2026_11_05',
        'identity_source':'https://support.claude.com/en/articles/14328960-identity-verification-on-claude',
        'identity_check_mandatory_for_every_purchase':False,
        'identity_documents_options':['valid_government_passport','physical_drivers_license','other_accepted_government_photo_id'],
        'document_acceptance_or_verification_success_guaranteed':False,
        'region_source':SOURCE,
        'russia_officially_supported':False,
        'vpn_owner_recommendation':'При работе из РФ использовать VPN, по возможности один сервис VPN и одну локацию; это практический совет владельца, не доказанный способ снизить риск и не гарантия доступа.',
        'identity_verification_can_apply_without_subscription':True,
        'store_can_complete_identity_verification':False,
        'no_guarantee':['absence_of_verification','absence_of_account_block'],
        'payment_does_not_override_service_controls':True,
        'specific_block_cause':'unknown_until_verified; do not assert unrelated to payment or caused by it',
        'identity_documents':'Do not request identity documents, passwords or codes for verification in this chat.',
    }


def claude_risk_reply(facts,current):
    """Only pure Claude risk questions. Mixed comparisons remain reviewed pipeline."""
    text=str(current or '')
    if re.search(r'chat\s*gpt|чат\s*гпт|krea|zoom|сравн|тариф|сколько|как\s+(?:оплат|оформ)|'
                 r'возврат|верн[а-я]*\s+деньги',text,re.I):return None
    named=bool(re.search(r'claude|клод',text,re.I))
    rows=facts.get('requested_catalog',[])
    keys={str(r.get('service_key','')).casefold() for r in rows}
    names={str(r.get('service','')).casefold() for r in rows}
    contextual=bool(rows) and all(k=='claude' for k in keys if k) and all('claude' in n or 'клод' in n for n in names if n)
    if not named and not contextual:return None
    risk=bool(re.search(r'риск|гаранти|блокиров|заблок|бан|верификац|провер[а-я]*\s+личност|'
                        r'подтверд[а-я]*\s+личност|vpn|впн|росси|\bрф\b|паспорт',text,re.I))
    if not risk:return None
    # A concrete incident needs screenshot evidence, not an assumed cause.
    from .troubleshooting import screenshot_request
    if screenshot_request(text) is not None:return None
    if re.search(r'меня\s+заблок|мой\s+аккаунт.{0,25}заблок|у\s+меня.{0,25}(?:блок|верификац)|'
                 r'просит.{0,25}(?:паспорт|документ|верификац)|потребовал.{0,25}верификац',text,re.I):
        return None
    body=('Россия не входит в официальный список поддерживаемых стран Claude. '
          'Оплата подписки не снимает региональные ограничения сервиса.\n\n'
          'При работе из РФ используйте VPN. По возможности придерживайтесь одного VPN и одной локации. '
          'Это практическая рекомендация, но она не гарантирует доступ и отсутствие блокировок.\n\n'
          'Claude может запросить проверку личности даже без активной подписки. '
          'Если проверка появилась, можно попробовать действующий загранпаспорт, водительское удостоверение '
          'или другой государственный документ из списка на экране Claude. Принятие документа и успешное прохождение не гарантируются. '
          'С её прохождением я помочь не могу; документы для проверки сюда присылать не нужно.\n\n'
          'Я не могу гарантировать, что аккаунт не заблокируют или не попросят верификацию: '
          'эти проверки контролирует сам Claude. Причину конкретной блокировки нужно разбирать отдельно, '
          'а не автоматически связывать с оплатой или исключать такую связь.')
    return {'reply':body,'summary':'Сообщены ограничения и отсутствие гарантий по согласованной политике. VPN не представлен доказанным снижением риска; причина конкретной блокировки не устанавливалась.',
            'handoff':False,'source_ids':[],
            'evidence':{'status':'catalog_direct','sources':[],
                        'diagnostic':'Этап: раскрытие рисков Claude. Условия владельца обновлены 05.11.2026; Россия отсутствует в официальном перечне доступа. Источник региона: '+SOURCE+'. Принятие документов, успешная верификация, гарантия доступности и доказанное снижение риска VPN не установлены.'}}

def public_region_issue(answer,facts=None):
    """Reject unsupported Claude availability/immunity claims before delivery.

    No statement about another provider is inferred from Claude's policy.
    """
    text=str(answer or '')
    facts=facts or {}
    rows=facts.get('requested_catalog',[])
    claude=bool(re.search(r'claude|клод',text,re.I)) or any(
        str(r.get('service_key','')).casefold()=='claude' or re.search(r'claude|клод',str(r.get('service','')),re.I)
        for r in rows)
    if not claude:return None
    for clause in re.split(r'[.!?;\n]+',text):
        low=clause.casefold()
        if not re.search(r'claude|клод',low):
            other_names=['chatgpt','чат гпт','openai','zoom','krea','notion']
            other_names += [str(r.get('service','')).casefold() for r in rows
                            if str(r.get('service_key','')).casefold()!='claude' and r.get('service')]
            if any(re.search(r'(?<![a-zа-я])'+re.escape(n)+r'(?![a-zа-я])',low) for n in other_names):continue
        universal_check=bool(re.search(r'(?:всегда|обязательно|у\s+каждого|для\s+всех)[^.!?\n]{0,65}верификац|'
                                       r'верификац[^.!?\n]{0,40}(?:обязательна|для\s+всех)|'
                                       r'при\s+оформлении[^.!?\n]{0,65}(?:затребует|потребует)[^.!?\n]{0,25}верификац',low))
        owner_sale_term=bool(re.search(r'услови[яе]\s+(?:оформления|магазина)[^.!?\n]{0,25}(?:через\s+нас|у\s+нас)|'
                                       r'(?:через|у)\s+нас[^.!?\n]{0,30}услови[яе]\s+оформления',low))
        if universal_check and not owner_sale_term and not re.search(r'не\s+обязатель|не\s+всегда|может\s+(?:потребова|запроси)|нельзя\s+утверждать',low):
            return 'Этап: проверка условий верификации Claude. Не подтверждено, что каждый покупатель обязательно проходит проверку личности. Наличие запроса в конкретном аккаунте и универсальная обязательность — разные факты.'
        # Separate warning that an outcome cannot be guaranteed is permitted.
        disclaimer=bool(re.search(r'(?:не\s+(?:могу|можем|да[её]м)|нельзя|невозможно)\s+гарантир|'
                                  r'без\s+гаранти|не\s+гарантиру|гаранти[а-я]*\s+нет',low))
        region=bool(re.search(r'росси|\bрф\b',low))
        supported=bool(re.search(r'входит\s+в\s+(?:официальный\s+)?(?:список|перечень)|'
                                r'официально\s+(?:поддержива|доступ)|поддерживаем[а-я]*\s+стран|'
                                r'доступ[а-я]*\s+(?:без\s+ограничений|официально)',low))
        neg=bool(re.search(r'не\s+(?:входит|поддержива|доступ)|отсутствует|исключена|не\s+относится',low))
        if region and supported and not neg:
            return ('Этап: проверка региональных условий Claude. Ответ утверждает официальную поддержку РФ; '
                    'Россия отсутствует в подтверждённом перечне доступа. Оплата и VPN не меняют этот перечень.')
        immunity=bool(re.search(r'блокиров[а-я]*\s+не\s+(?:будет|случится)|не\s+заблокиру[а-я]*|'
                               r'гарантиру[а-я]*[^.!?]{0,50}(?:доступ|блокиров|верификац)|'
                               r'без\s+риска\s+блокиров',low))
        verification=bool(re.search(r'верификац[а-я]*\s+не\s+(?:потребуется|нужна|будет)|'
                                   r'проверка\s+личности\s+не\s+(?:нужна|потребуется)|'
                                   r'верификац[а-я]*\s+(?:только|лишь)\s+(?:на\s+)?платн',low))
        if (immunity or verification) and not disclaimer:
            return ('Этап: проверка гарантий Claude. Ответ обещает отсутствие блокировки или проверки личности. '
                    'Такие гарантии не предоставляются; проверка возможна и без активной подписки.')
    return None
