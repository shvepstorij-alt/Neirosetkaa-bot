"""Shared shortcut coverage gate, independent of a particular store brand.

This is a conservative routing floor, not a claim that intent is fully solved.
Uncovered goals go to the complete reviewed consultation pipeline.
"""
import re

PATTERNS={
    'purchase':r'оформ|как[^.!?\n]{0,40}(?:оплат|купить)|что[^.!?\n]{0,40}(?:нужно|потребуется)|какие[^.!?\n]{0,25}данные',
    'activation':r'активир|активац|авториз|страниц[а-яё]*\s+сесси',
    'price':r'цен[аыуе]|стоимост|сколько[^.!?\n]{0,30}стоит|какие[^.!?\n]{0,30}тариф',
    'features':r'подробнее|возможност|что[^.!?\n]{0,25}входит|отлич|сравн|функци|лимит',
    'problem':r'ошиб|не\s+(?:появил|появля|активир|работа|получа)|заблок|блокиров|исправить',
    'account_change':r'друг[а-яё]*\s+аккаунт|смен[а-яё]*\s+аккаунт|перен[а-яё]*\s+подписк',
    'refund':r'возврат|верн[а-яё]*\s+деньг|вернут[а-яё]*\s+деньг',
}


def goals(text, facts=None):
    result={name for name,pattern in PATTERNS.items() if re.search(pattern,str(text),re.I)}
    from .selection_help import is_selection_request
    from .consultation_intent import context
    if is_selection_request(text,facts) or context(text,facts).get('active'):
        result.add('recommendation')
    return result


def covers(text, supported, facts=None):
    return not (goals(text,facts)-set(supported))
