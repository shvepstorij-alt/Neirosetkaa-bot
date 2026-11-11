"""What a reply claims. The client's wording does not decide the hold."""
import re

from .completion_claims import has_completion_claim


_ASK = re.compile(r'(?i)пришлите|напишите|укажите|сообщите')
_CASE_FACT = re.compile(
    r'(?i)номер\s+заказа|номер\s+платежа|free\s*kassa|freekassa|фри\s*касс|fk_intid|'
    r'текст\s+ошибки|скриншот')
_OUTCOME = re.compile(
    r'(?i)оплат\w*\s+(?:уже\s+)?(?:отмечен\w*|получен\w*|прошл\w*|подтвержден\w*)|'
    r'подписк\w*\s+(?:уже\s+)?активирован\w*|'
    r'активаци\w*\s+(?:уже\s+)?(?:выполнен\w*|прошл\w*|завершен\w*)|'
    r'возврат\s+(?:уже\s+)?(?:одобрен\w*|оформлен\w*|выполнен\w*|сделан\w*)')
def _asks_secret(text):
    for sentence in re.split(r'(?<=[.!?])\s+', str(text or '')):
        if not re.search(r'(?i)пришлите|напишите|укажите|сообщите', sentence):
            continue
        if re.search(r'(?i)без\s+(?:\w+[,\s]+){0,6}(?:парол|токен|сесси)', sentence):
            continue
        if re.search(r'(?i)(?:пришлите|напишите|укажите|сообщите)[^.!?\n]{0,80}'
                     r'(?:парол|токен|текст\s+сесси|данн\w*\s+доступа)', sentence):
            return True
    return False


def asserts_outcome(text):
    """The reply states that a payment, activation or refund already happened."""
    value = str(text or '')
    if has_completion_claim(value):
        return True
    for match in _OUTCOME.finditer(value):
        before = value[max(0, match.start() - 30):match.start()]
        # «Если оплата прошла…» is a condition, not a statement that it happened.
        if re.search(r'(?i)(?:если|когда|как\s+только|после\s+того,?\s+как)\s+(?:\w+\s+){0,2}$', before):
            continue
        return True
    return False


def collects_case_data(text):
    """The reply only asks for an identifier or evidence of the problem."""
    value = str(text or '')
    if asserts_outcome(value) or _asks_secret(value):
        return False
    return bool(_ASK.search(value) and _CASE_FACT.search(value))
