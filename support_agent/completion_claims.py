"""Bounded recognition of asserted completed financial/account operations.

This is a content gate, not evidence that an operation really happened. Only
explicit local negation, non-entailment, questions and conditional antecedents
are exempted. A hedge such as 'probably' is not permission to assert completion.
"""
import re


_BRIDGE = r'[^.!?;,:\n]{0,65}?'
_CASES = (
    (r'\b(?:оплат[ауы]|плат[её]ж[а-яё]*)\b' + _BRIDGE,
     r'прош[её]л|прошла|подтвержд[её]н[ао]?|принят[ао]?|получен[ао]?'),
    (r'\bвозврат[а-яё]*\b' + _BRIDGE,
     r'одобрен[ао]?|оформлен[ао]?|подтвержд[её]н[ао]?|выполнен[ао]?|заверш[её]н[ао]?'),
    (r'\b(?:подписк[а-яё]*|аккаунт[а-яё]*)\b' + _BRIDGE,
     r'активирован[аоы]?|создан[аоы]?|зарегистрирован[аоы]?'),
    (r'\bактиваци[яию]\b' + _BRIDGE,
     r'выполнен[ао]?|заверш[её]н[ао]?|прошла'),
    (r'\bденьги\b' + _BRIDGE,
     r'возвращены|вернулись'),
)
_PATTERNS = [re.compile(prefix + r'(?P<predicate>\b(?:' + status + r')\b)', re.I)
             for prefix, status in _CASES]
# Active voice and inverted word order still assert performed operations.
_PATTERNS += [re.compile(r'(?P<predicate>\b(?:' + verbs + r')\b)' + _BRIDGE + r'\b(?:' + objects + r')\b', re.I)
              for verbs, objects in (
                  (r'одобрил[аи]?|оформил[аи]?|выполнил[аи]?|подтвердил[аи]?', r'возврат[а-яё]*'),
                  (r'активировал[аи]?', r'подписк[а-яё]*|аккаунт[а-яё]*'),
                  (r'создал[аи]?', r'аккаунт[а-яё]*'),
                  (r'подтвердил[аи]?|подтвержд[её]н[ао]?', r'оплат[ауы]|плат[её]ж[а-яё]*'),
                  (r'вернул[аи]?', r'деньги'),
                  (r'одобрен[ао]?|оформлен[ао]?|выполнен[ао]?|подтвержд[её]н[ао]?', r'возврат[а-яё]*'),
                  (r'активирован[аоы]?', r'подписк[а-яё]*'),
                  (r'создан[аоы]?|зарегистрирован[аоы]?', r'аккаунт[а-яё]*'),
                  (r'получен[ао]?|принят[ао]?', r'оплат[ауы]|плат[её]ж[а-яё]*'),
              )]

_BOUNDARY = re.compile(r'[.!?;\n]+|,?\s+\b(?:но|однако|зато|а)\b\s+', re.I)
_DIRECT_NEGATION = re.compile(r'\bне\s+(?:(?:был[ао]?|были|будет|будут)\s+)?$', re.I)
_NOT_ENTAILED = re.compile(
    r'(?:\bне\s+(?:означает|подтверждает|доказывает|гарантирует)|'
    r'\bне\s+(?:могу|можем|удалось|получилось)\s+(?:пока\s+)?(?:подтвердить|утверждать|сказать)|'
    r'\b(?:нельзя|невозможно)\s+(?:подтвердить|утверждать|сказать)|'
    r'\bнет\s+подтверждения|\bне\s+подтверждено)\s*,?\s*что\s*'
    r'(?:(?:ваш[а-яё]*|у\s+вас|уже|эта|этот|данная|данный|я|мы)\s+)*$', re.I)
_ANTECEDENT = re.compile(
    r'\b(?:если|когда|после\s+того\s+как)\s+'
    r'(?:(?:ваш[а-яё]*|у\s+вас|уже|эта|этот|данная|данный)\s+)*$', re.I)
_FUTURE = re.compile(r'\b(?:будет|будут|может\s+быть|могут\s+быть|должн[аоы]?\s+быть)\s*$', re.I)


def has_completion_claim(text):
    """Whether text asserts a completed payment/refund/activation/account.

    Polarity never crosses sentence or contrast boundaries. The exceptions
    apply only to their own bounded candidate, not later affirmative clauses.
    Unknown hedges remain blocked rather than pretending to resolve semantics.
    """
    text = str(text or '')
    boundaries = list(_BOUNDARY.finditer(text))
    starts = [0] + [boundary.end() for boundary in boundaries]
    ends = [boundary.start() for boundary in boundaries] + [len(text)]
    for segment_start, segment_end in zip(starts, ends):
        segment = text[segment_start:segment_end]
        if not segment.strip():
            continue
        for pattern in _PATTERNS:
            for match in pattern.finditer(segment):
                pred_start = match.start('predicate')
                before_predicate = segment[max(0, pred_start - 85):pred_start]
                before_subject = segment[max(0, match.start() - 140):match.start()]
                after = segment[match.end():]
                # Questions are not performed-action assertions. A comma and
                # a subsequent question do not turn the preceding assertion
                # into a question ("Оплата прошла, проверили?").
                absolute_end = segment_start + match.end()
                following = text[absolute_end:]
                question = bool(re.match(r'\s*\?', following))
                if question:
                    continue
                if _DIRECT_NEGATION.search(before_predicate) or _FUTURE.search(before_predicate):
                    continue
                if re.match(r'\s+(?:не\s+был[ао]?|не\s+были)\b', after, re.I):
                    continue
                if _NOT_ENTAILED.search(before_subject) or _ANTECEDENT.search(before_subject):
                    continue
                return True
    return False
