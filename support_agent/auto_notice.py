"""Code-owned customer notices. Never include provider errors or customer secrets."""

TEXTS = {
    'technical': 'Сейчас возникла техническая ошибка, и я не смог подготовить проверенный ответ. Александр посмотрит ваше обращение и ответит, когда будет на связи.',
    'information': 'Пока не получилось надёжно подтвердить условия по вашему вопросу. Не хочу дать вам неверную информацию. Потребуется проверка Александра; он ответит, когда будет на связи.',
    'manual': 'Этот вопрос требует личной проверки Александра. Он посмотрит ваше обращение и ответит, когда будет на связи.',
}


def kind_for(evidence, reason):
    # Exact diagnostics stay owner-only. This classification makes no promises
    # about fulfilment, delivery times, refunds or the outcome of a payment.
    if evidence.get('manual_secret_intake') or evidence.get('new_account_stage'):
        return 'manual'
    if evidence.get('status') in ('unavailable', 'not_recorded'):
        return 'technical'
    if any(x in reason.casefold() for x in ('лимит запросов', 'telegram', '120 секунд', 'ошибка подготовки')):
        return 'technical'
    if evidence.get('status') in ('order_status', 'owner_policy') or evidence.get('handoff_reason'):
        return 'manual'
    return 'information'
