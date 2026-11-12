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
