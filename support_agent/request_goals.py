"""Shared shortcut coverage gate, independent of a particular store brand.

This is a conservative routing floor, not a claim that intent is fully solved.
Uncovered goals go to the complete reviewed consultation pipeline.
"""
import re

PATTERNS={
    'purchase':r'оформ|продл[а-яё]*|\brenew(?:al)?\b|как[^.!?\n]{0,40}(?:оплат|купить|пополн)|(?:помо(?:гаете|гаешь|жете|жешь|чь|гите|ги)|помогли\s+бы)[^.!?\n]{0,35}оплат|(?:как|где|откуда)[^.!?\n]{0,35}(?:получить|найти|скопировать)[^.!?\n]{0,30}ссылк|(?:можно|могу)[^.!?\n]{0,35}(?:оплат|купить|пополн)|что[^.!?\n]{0,40}(?:нужно|потребуется)|какие[^.!?\n]{0,25}данные',
    'activation':r'активир|активац|авториз|страниц[а-яё]*\s+сесси',
    'installation':r'(?:скача|установ)[а-яё]*[^.!?\n]{0,45}(?:прилож|chat\s*gpt|чат\s*гпт|android|андроид|iphone|айфон|apk|апк)|как[^.!?\n]{0,30}(?:сменить|изменить)[^.!?\n]{0,20}регион',
    'setup':r'(?:нужно|надо|требуется)\s+ли[^.!?\n]{0,35}настр[а-яё]*|(?:нужн[а-яё]*|надо|требуется)[^.!?\n]{0,35}настр[а-яё]*[^.!?\n]{0,20}\?|настр[а-яё]*[^.!?\n]{0,35}(?:нужн[а-яё]*|надо|требуется)[^.!?\n]{0,20}\?',
    'price':r'цен[аыуе]|стоимост|сколько[^.!?\n]{0,30}сто(?:ит|ят)|какие[^.!?\n]{0,30}тариф',
    'features':r'подробнее|возможност|что[^.!?\n]{0,25}входит|отлич|сравн|разниц|функци|лимит',
    'problem':r'ошиб|не\s+(?:появил|появля|активир|работа|получа)|заблок|блокиров|исправить',
    'account_change':r'друг[а-яё]*\s+аккаунт|смен[а-яё]*\s+аккаунт|перен[а-яё]*\s+подписк',
    'refund':r'возврат|верн[а-яё]*\s+деньг|вернут[а-яё]*\s+деньг',
}


def goals(text, facts=None):
    result={name for name,pattern in PATTERNS.items() if re.search(pattern,str(text),re.I)}
    from .privacy import public_token_quantity_question
    if any(public_token_quantity_question(line) for line in re.split(r'(?<=[?!])\s*|\n',str(text))):
        result.add('features')
    turn=facts.get('conversation_turn',{}) if isinstance(facts,dict) else {}
    if turn.get('version')==1 and str(text)==turn.get('current_text'):
        result.update(g for g in turn.get('goals',[]) if g in PATTERNS or g=='recommendation')
    from .selection_help import is_selection_request
    from .consultation_intent import context
    if is_selection_request(text,facts) or context(text,facts).get('active'):
        result.add('recommendation')
    return result


def covers(text, supported, facts=None):
    return not (goals(text,facts)-set(supported))
