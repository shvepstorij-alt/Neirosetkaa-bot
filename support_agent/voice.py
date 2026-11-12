"""The assistant writes in the masculine (owner 12.11.2026): «рад», «проверил», «готов»."""
import re

# «я проверила» → «я проверил»; only after «я», so «подписка оформлена/готова» stays.
_I_PAST = re.compile(r'(?i)\b(я(?:\s+(?:уже|сейчас|тоже|только|ещё|еще|бы|вам|вас|это|всё|все|сам|сама))*\s+)([а-яё]{2,}?)ла(сь)?\b')
_I_SELF = re.compile(r'(?i)\b(я\s+)сама\b')
_I_ADJ = re.compile(r'(?i)\b(я(?:\s+(?:очень|всегда|тоже|буду|была|был))*\s+)(рада|готова|благодарна|уверена|согласна|должна|обязана|рад[а-я]*а)\b')
_OPEN = re.compile(r'(?m)(^|[.!?]\s+)(Рада|Готова|Благодарна)(?=[\s,!])')
_STEMS = {'рада': 'рад', 'готова': 'готов', 'благодарна': 'благодарен', 'уверена': 'уверен',
          'согласна': 'согласен', 'должна': 'должен', 'обязана': 'обязан'}


def _case(word, sample):
    return word[:1].upper() + word[1:] if sample[:1].isupper() else word


def masculine(text):
    value = str(text or '')
    value = _I_SELF.sub(lambda m: m.group(1) + 'сам', value)
    value = _I_PAST.sub(lambda m: m.group(1) + m.group(2) + 'л' + ('ся' if m.group(3) else ''), value)
    value = _I_ADJ.sub(lambda m: m.group(1) + _case(_STEMS.get(m.group(2).lower(), m.group(2)[:-1]), m.group(2)), value)
    value = _OPEN.sub(lambda m: m.group(1) + _case(_STEMS[m.group(2).lower()], m.group(2)), value)
    return value


_FILLER = re.compile(r'(?:\s|\n)*(?:(?:Всегда\s+|Буду\s+|Очень\s+)?[Рр]ад\s+помочь[^.!?\n]{0,40}[.!)]+|'
                     r'Готов\s+помочь[^.!?\n]{0,60}[.!)]+|Обращайтесь[^.!?\n]{0,60}[.!)]+)\s*$')


def trim_filler(text):
    """«…цена 1490 ₽. Рад помочь!» — the tail adds nothing. A reply that is only thanks stays."""
    value = str(text or '')
    cut = value
    for _ in range(2):
        cut = _FILLER.sub('', cut)
    return cut.rstrip() if len(cut.strip()) >= 25 else value


_HELLO = re.compile(r'(?i)^\W{0,3}(?:[а-яё]+,\s*)?(здравствуй(?:те)?|добр(?:ый|ое|ого)\s+(?:день|вечер|утро|времени\s+суток)|доброго|привет(?:ствую)?|хай|салам|hello|hi)\b')
_HELLO_REPLY = re.compile(r'(?i)^\W{0,3}(?:здравствуй|добр(?:ый|ое|ого)|привет|hello|hi\b)')


def greet_back(reply, burst, history):
    """Owner 12.11.2026: a client who says hello gets a hello back, once, at the start of the talk."""
    text = str(reply or '')
    hello = _HELLO.search(str(burst or ''))
    if not text or not hello or _HELLO_REPLY.search(text):
        return text
    if any(isinstance(row, dict) and row.get('role') in ('assistant', 'owner') for row in history or []):
        return text
    word = hello.group(1).casefold()
    if word.startswith('добр') and ' ' in word and 'времени' not in word:
        greeting = word[:1].upper() + word[1:] + '!'
        greeting = greeting.replace('Доброго', 'Добрый').replace('Доброе утро', 'Доброе утро')
    else:
        greeting = 'Здравствуйте!'
    if re.match(r'(?i)[a-z]', word):
        return text
    return greeting + ' ' + text if len(text) < 200 and '\n' not in text else greeting + '\n' + text
