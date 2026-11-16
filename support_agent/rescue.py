"""Answer instead of stopping (owner 16.11.2026: stops are solved systemically, not word by word).

Runs on every held draft. It leaves the stop only for real reasons — refund or money
complaint, the client asking for a person, credentials, a real conflict, the owner
already handling the chat, an action only the owner can do. Otherwise it gives the
client the most useful safe answer: a known topic, the plans and how to buy, or the
model's own draft when it passes the content check. The owner still sees the card.
"""
import re

HARD_CONFLICT = re.compile(r'(?i)возврат|верн[иу]|чарджбек|chargeback|оспор|жалоб|\bсуд|прокурат|роспотреб|полици|'
                           r'угрож|мошенн|обман|развод|кидал|оскорб|\bмат\b|груб|refund|scam')
BUY_INTENT = re.compile(r'(?i)купить|покупк|оформ|приобр|оплат|продл|платн|подписк|тариф|сколько\s+стои|цен[аыу]|хочу\s+\w+|нужн\w*\s+(?:плат|подпис|тариф)')
PAID_CLAIM = re.compile(r'(?i)оплатил|оплатила|оплачен|деньги\s+(?:точно\s+)?(?:дошли|ушли|пришли)|оплата\s+прошла|перев[её]л|включите\s+подписк|активируйте')
OWNER_ACTION_KEYS = ('refund_hold', 'account_setup_hold', 'manual_secret_intake', 'owner_action_required',
                     'paid_notice', 'outlook_ready', 'account_ready', 'payment_case')
HOLD_KEYS = ('conflict_hold', 'owner_alignment_hold', 'stale_price_hold', 'owner_fact_hold', 'repeat_hold',
             'handoff_reason', 'review_required', 'auto_block_reason')


def _must_stop(result, understanding, context):
    from .owner_scenarios import _human
    from .privacy import sensitive
    from .turn_understanding import _MONEY_COMPLAINT, _REFUND_WORD
    evidence = result.get('evidence') if isinstance(result.get('evidence'), dict) else {}
    burst = str((context or {}).get('burst') or '')
    if not burst.strip() or len(burst) > 1500:
        return 'нет текста клиента'
    if any(evidence.get(key) for key in OWNER_ACTION_KEYS):
        return 'нужно действие владельца'
    if _REFUND_WORD.search(burst) or _MONEY_COMPLAINT.search(burst):
        return 'возврат или жалоба на списание'
    if PAID_CLAIM.search(burst):
        return 'клиент сообщает об оплате'
    if _human(burst):
        return 'клиент просит человека'
    if sensitive(burst):
        return 'данные доступа'
    if str((context or {}).get('answers_to_who') or '') in ('Александр', 'owner'):
        return 'чат ведёт владелец'
    u = understanding if isinstance(understanding, dict) else {}
    if u.get('owner_alignment') == 'contradicts':
        return 'расходится с владельцем'
    from .turn_understanding import _conflict
    if _conflict(u) == 'high' or evidence.get('conflict_hold'):
        return 'конфликт'
    if evidence.get('repeat_hold') or evidence.get('stale_price_hold'):
        return 'повтор'
    return ''


def _buy_answer(context):
    from .turn_understanding import BUY_STEPS, RENEW_LINE, _RENEW, _named_services, _service_rows, tariff_question
    burst = str((context or {}).get('burst') or '')
    if not BUY_INTENT.search(burst):
        return ''
    rows = []
    for service in sorted(_named_services(context or {})):
        rows = _service_rows(context, service)
        if rows:
            break
    parts = ['Здравствуйте!']
    if rows:
        parts.append(tariff_question(rows))
    parts.append(BUY_STEPS)
    if _RENEW.search(burst):
        parts.append(RENEW_LINE)
    return '\n\n'.join(parts)


def _faq(topic_id):
    try:
        from .faq import load_faq
        return next((t['answer'] for t in load_faq()['topics'] if t['id'] == topic_id), '')
    except Exception:
        return ''


def _clean(result, reply, how):
    evidence = dict(result.get('evidence') or {}) if isinstance(result.get('evidence'), dict) else {}
    reason = str(evidence.get('handoff_reason') or evidence.get('diagnostic') or '')
    for key in HOLD_KEYS:
        evidence.pop(key, None)
    evidence.update(rescued=how, rescued_from=reason[:300], owner_followup=True)
    summary = (str(result.get('summary') or '') + ' Вместо остановки: ' + how + '.').strip()
    return dict(result, reply=reply, handoff=False, evidence=evidence, summary=summary)


def answer_instead_of_stop(result, understanding, context):
    from .handoff import HANDOFF
    if not isinstance(result, dict):
        return result
    # The model may write «Александр будет на связи» itself without asking for a stop.
    self_stop = not result.get('handoff') and HANDOFF in str(result.get('reply') or '')
    if not result.get('handoff') and not self_stop:
        return result
    if _must_stop(result, understanding, context):
        return dict(result, handoff=True) if self_stop else result
    from .turn_understanding import _CODEX_HOW, classify_draft
    burst = str((context or {}).get('burst') or '')
    if _CODEX_HOW.search(burst) and _faq('codex_install'):
        return _clean(result, 'Здравствуйте!\n\n' + _faq('codex_install'), 'инструкция по Codex')
    buy = _buy_answer(context)
    if buy:
        return _clean(result, buy, 'тарифы и как купить')
    draft = str(result.get('reply') or '').strip()
    prices = {row['price'] for row in (context or {}).get('tariffs') or [] if type(row.get('price')) is int}
    if evidence_of(result).get('owner_fact_hold'):
        return result  # the draft states facts about a topic the owner has no recorded answer for
    if draft and HANDOFF not in draft and len(draft) > 40 and classify_draft(draft, prices).get('send'):
        return _clean(result, draft, 'черновик помощника прошёл проверку')
    return dict(result, handoff=True) if self_stop else result


def evidence_of(result):
    return result.get('evidence') if isinstance(result.get('evidence'), dict) else {}
