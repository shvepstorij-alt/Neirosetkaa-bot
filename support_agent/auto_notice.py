"""Code-owned customer notices. Never include provider errors or customer secrets."""
import re

TEXTS = {
    'technical': 'Сейчас не удалось получить проверенные данные. Помогу разобраться по карточке тарифа или скриншоту ошибки без личных и платёжных данных. Также могу подобрать другой сервис под вашу задачу).',
    'information': 'Часть условий пока не подтверждена. Помогу разобраться по официальному описанию: пришлите ссылку на нужный пункт или скриншот без личных данных. Если хотите, подберём другой сервис под вашу задачу).',
    'secret': 'Полученные данные доступа повторно присылать не нужно. С общими вопросами по сервису и дальнейшим шагам помогу).',
}


def text_for(kind, now=None):
    """The only fixed sentence a client can receive instead of a draft."""
    from .handoff import handoff_text
    return handoff_text(now)


def matches_template(kind, text, now=None):
    if kind=='human':
        from .handoff import HANDOFF
        return str(text or '').startswith(HANDOFF)
    return text==text_for(kind, now)


def kind_for(evidence, reason, current_text='', usable_reply=False):
    # Exact diagnostics stay owner-only. This classification makes no promises
    # about fulfilment, delivery times, refunds or the outcome of a payment.
    evidence = evidence if isinstance(evidence, dict) else {}
    from .privacy import sensitive
    current = str(current_text or '')
    diagnostic = '\n'.join(str(value) for value in (reason, evidence.get('handoff_reason', ''), evidence.get('diagnostic', '')))
    lower = diagnostic.casefold()
    if (evidence.get('manual_secret_intake') or sensitive(current)
            or re.search(r'\[(?:данные доступа|токен|сообщение скрыто)', current, re.I)):
        return 'secret'
    if (re.search(r'(?:позовите|позови|соедините|подключите)[^.!?\n]{0,45}(?:человек|оператор|александр)|'
                 r'(?:хочу|можно)[^.!?\n]{0,25}(?:поговорить|общаться)[^.!?\n]{0,25}(?:с\s+человеком|с\s+александром)|'
                 r'(?:хочу|нужен|нужна)[^.!?\n]{0,25}(?:оператор|жив[а-яё]*\s+человек)', current, re.I)
        or 'клиент явно просит человека' in lower or 'human request' in lower):
        return 'human'
    # Held work tells the client that Alexander will reply. It does not describe an operation result.
    if (evidence.get('status') in ('order_status', 'owner_policy') or evidence.get('new_account_stage')
            or evidence.get('owner_action_required') is True or evidence.get('review_required') is True
            or re.search(r'конфликт\s+(?:подтвержд[её]нных\s+)?(?:правил|политик|способов\s+оформления)', diagnostic, re.I)):
        return 'human'
    if any(x in lower for x in ('лимит запросов', 'telegram', '120 секунд', 'ошибка подготовки')):
        return 'technical'
    # Missing sources/coverage is informational even if an old route stored
    # handoff_reason. That field alone is not evidence of an account action.
    # A held draft is a real answer. The canned gap text is only for when there is nothing to show.
    if re.search(r'источник|неполнот|не\s+подтвержд|не\s+удалось\s+(?:получить|подтвердить)|coverage|source', diagnostic, re.I):
        return 'human' if usable_reply else 'information'
    if evidence.get('status') in ('unavailable', 'not_recorded'):
        return 'technical'
    return 'human' if usable_reply else 'information'


def helpful_reply(facts, evidence, reason, question=''):
    """Trusted scoped repair for informational failures, before notice storage.

    None means retain the real manual path and its static notice. The caller
    must use normal current-job/send checks for a returned replacement; this
    function cannot authorize Telegram delivery or change the selected mode.
    """
    if not isinstance(facts, dict):
        return None
    current = (facts.get('conversation_turn', {}).get('current_text')
               or facts.get('conversation', {}).get('latest_message') or question
               or facts.get('conversation', {}).get('latest_client_message', ''))
    kind = kind_for(evidence, reason, current)
    if kind in ('manual', 'human', 'secret'):
        return None
    evidence = evidence if isinstance(evidence, dict) else {}
    from .privacy import scrub
    diagnostic = '\n'.join(scrub(str(value))[:1400] for value in
                           (reason, evidence.get('handoff_reason', ''), evidence.get('diagnostic', '')) if value)
    from .autonomous_fallback import reply
    answer = reply(facts, diagnostic, question)
    if answer is not None:
        answer.setdefault('evidence', {})['notice_recovery_kind'] = kind
    return answer
