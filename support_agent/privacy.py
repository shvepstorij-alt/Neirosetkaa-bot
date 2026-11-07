"""Conservative filtering. Not a guarantee that all secrets can be detected."""
import re


_PUBLIC_MODEL_PATTERN = (r'(?:gpt-\d+(?:\.\d+)?(?:-(?:mini|nano|pro|turbo|preview))?'
                         r'|claude-(?:sonnet|opus|haiku)-\d+(?:[-.]\d+)?'
                         r'|gemini-\d+(?:\.\d+)?-(?:pro|flash)'
                         r'|glm-\d+(?:\.\d+)?(?:-(?:turbo|air|flash|vision))?)')


def public_token_quantity_question(text):
    """Recognise one bounded public quantity question, never a token value.

    Unknown short brand names are allowed in the product position; mixed
    identifiers are allowed only as explicit public model version syntax.
    This is routing evidence for research, not proof of a quota or identity.
    """
    value = str(text or '').strip()
    if not value or len(value) > 240 or '\n' in value or '\r' in value:
        return False
    if re.search(r'[:={}\[\]"\'`@/\\]|\b(?:мой|моя|мои|наш|ваш|твой|bearer|password|session|cookie)\b'
                 r'|sk-|eyj|access.?token|refresh.?token|session.?key', value, re.I):
        return False
    # A one-word product label is deliberately narrower than arbitrary prose.
    # Cyrillic labels and familiar model versions remain ordinary public text.
    brand = r'(?:' + _PUBLIC_MODEL_PATTERN + r'|[a-zа-яё][a-zа-яё-]{1,35})'
    scope = r'(?:(?:\s+в\s+(?:контексте|контекстном\s+окне))|(?:\s+на\s+(?:запрос|сообщение|ответ)))?'
    product = r'(?:\s+(?:у|в|для)\s+(?:(?:модели|сервиса|модель|сервис)\s+)?' + brand + r')?'
    question = (r'(?:а\s+)?(?:сколько\s+(?:всего\s+)?токен(?:ов|а)?'
                r'|какой\s+(?:максимальный\s+)?лимит\s+токенов'
                r'|каково\s+(?:максимальное\s+)?количество\s+токенов)'
                + scope + product + r'\s*\??')
    # Also accept «в контексте ChatGPT» without the extra preposition.
    direct = (r'(?:а\s+)?сколько\s+(?:всего\s+)?токен(?:ов|а)?'
              r'\s+в\s+(?:контексте|контекстном\s+окне)\s+'
              + brand + r'\s*\??')
    return bool(re.fullmatch(question, value, re.I) or re.fullmatch(direct, value, re.I))


def _redact(text):
    text=str(text or '')[:16000]
    if re.search(r'(?i)access.?token|refresh.?token|session.?key|\bbearer\b|eyJ[\w-]{20}|sk-[\w-]{16}',text):
        return '[Скрыто: сообщение может содержать данные доступа]',['access_secret']
    categories=[]
    rules=[('url',r'https?://\S+','[ссылка скрыта]'),
           ('credential_line',r'(?im)^.*(?:пароль|password|\bpass\s*:|\bcvv\b|код (?:входа|подтверждения))\s*[:=].*$','[данные доступа скрыты]'),
           ('email',r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}','[email скрыт]'),
           ('payment_number',r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)','[номер скрыт]'),
           ('voucher_code',r'\b(?:GPT\w*|CL\w*)-(?:[A-Za-z0-9]{3,}-){2,}[A-Za-z0-9]{3,}\b','[код скрыт]')]
    # Voucher codes have several groups. GPT-4.1 / GPT-5 are model names, not secrets.
    for category,pattern,replacement in rules:
        text,count=re.subn(pattern,replacement,text)
        if count:categories.append(category)
    return text,categories


def scrub(text):
    return _redact(text)[0]


def redaction_categories(text):
    """Only safe condition labels; never the matched values or raw input."""
    return _redact(text)[1]


def sensitive(text):
    # Prefer manual handling over leaking unlabelled passwords or one-time codes.
    if 'access_secret' in redaction_categories(text):return True
    # Recognition uses the original model label, before model-name projection.
    original_lines=re.split(r'(?<=[?!])\s*|\n',text or '')
    quantity_questions=[public_token_quantity_question(line) for line in original_lines]
    text=re.sub(r'(?i)\b(?:gpt-\d+(?:\.\d+)?(?:-(?:mini|nano|pro|turbo|preview))?|claude-(?:sonnet|opus|haiku)-\d+(?:[-.]\d+)?|gemini-\d+(?:\.\d+)?-(?:pro|flash)|glm-\d+(?:\.\d+)?(?:-(?:turbo|air|flash|vision))?)(?![\w-]|\.(?=\S))',
                '[название модели]',text or '')
    # An explicitly labelled public order reference is not an OTP merely
    # because the number happens to contain 4–8 digits on the following line.
    # Only the detection copy changes; output scrub still masks card-sized IDs.
    text=re.sub(r'(?i)((?:номер\s+(?:заказа|платежа)|заказ\w*\s*[#№]|free\s*kassa|фри\s*касс\w*|\bintid\b)\s*[:#№]?\s*)\d{1,18}\b',
                r'\1[номер заказа]',text)
    # Discussing a required field is not supplying its value. Keep that intent
    # so support can answer e.g. whether Apple ID credentials are required.
    # This affects intake only; output scrub/redaction stays unchanged.
    # A collected batch may combine a harmless field question with several
    # other messages. Remove only independently recognised question lines from
    # the detection copy, never from stored text. A secret on another line is
    # still detected normally (including an unlabelled password/code).
    # A message may contain several independent questions on one line. A
    # purchase question can mention an unfamiliar brand/amount; neither is a
    # credential value belonging to the following field question. Split only
    # at explicit question/exclamation boundaries (not dots in URLs/emails).
    # Keep every unrecognised fragment for the sensitive-value checks below.
    lines=re.split(r'(?<=[?!])\s*|\n',text)
    field_questions=[_credential_field_question(line) or _credential_help_question(line)
                     or (i<len(quantity_questions) and quantity_questions[i])
                     for i,line in enumerate(lines)]
    remaining=[line for line,is_question in zip(lines,field_questions) if not is_question]
    if any(quantity_questions) and any(re.fullmatch(
            r'\s*(?:вот|держи(?:те)?)\s+[a-zA-Z0-9!@$%^&*_.+\-]{5,}\s*',line,re.I)
            for line in remaining):return True
    if len(remaining)!=len(lines):
        # A password question followed by a bare Latin string can be the
        # supplied password on the next line. Do not erase that association.
        if any(re.fullmatch(r'[a-zA-Z0-9!@$%^&*_.+\-]{5,}',line.strip().rstrip('?!'))
               and line.strip().rstrip('?!').casefold() not in _PUBLIC_FIELD_WORDS for line in remaining):return True
        first_question=field_questions.index(True)
        # An unknown Latin string following the field question may be its
        # value even when introduced with prose ("Вот qwerty"). Earlier,
        # independent product questions do not have that association.
        if any(word.casefold() not in _PUBLIC_FIELD_WORDS
               for line,is_question in zip(lines[first_question+1:],field_questions[first_question+1:])
               if not is_question for word in re.findall(r'[a-zA-Z]+',line)):
            return True
    text='\n'.join(remaining)
    return bool(re.search(r'(?im)парол|password|\bpass\b|токен|token|session|cookie|cvv|код.{0,15}(?:почт|вход|подтверж)|^\s*\d{4,8}\s*$|(?=\S{8,})(?=\S*[a-zA-Z])(?=\S*\d)\S{8,}',text or ''))


_PUBLIC_FIELD_WORDS={'apple','id','google','krea','zoom','canva','cursor','github','suno',
                     'midjourney','spotify','youtube','openrouter','gemini','grok','chatgpt',
                     'claude','password','token','session','cookie','cvv','email','login'}


def _credential_help_question(text):
    """A request for instructions is not a supplied credential value.

    Narrow syntax only; assignment, numbers, payloads and unknown Latin values
    stay manual. A separate secret line is still inspected by sensitive().
    """
    text=str(text or '').strip()
    if not text or len(text)>500:return False
    lower=text.casefold().replace('ё','е')
    if re.search(r'[:={}\[\]"\'`@\d]|sk-|eyj|bearer|access.?token|refresh.?token|session.?key',lower):return False
    if not re.search(r'токен|пароль|сесси[яию]',lower):return False
    if any(w not in _PUBLIC_FIELD_WORDS for w in re.findall(r'[a-z]+',lower)):return False
    if not re.search(r'(?:где|откуда|как)[^.!?]{0,70}(?:получить|взять|найти|открыть|скопировать)|не\s+(?:могу|получается|удается)[^.!?]{0,50}(?:получить|взять|найти|открыть|скопировать)',lower):return False
    if re.search(r'(?:токен|пароль|сесси[яию])\s+(?!для\b|на\b|в\b|от\b|не\b|нуж\w*\b|получ\w*\b|взять\b|найти\b|скопировать\b|открыть\b)\w+',lower):return False
    return True


def _credential_field_question(text):
    """Narrow non-secret questions; fail closed on payload-like syntax/values."""
    text=str(text or '').strip()
    if not text or len(text)>500 or '\n' in text:return False
    lower=text.casefold().replace('ё','е')
    # Values, JSON, credential assignments, email addresses, long mixed
    # identifiers and numeric codes remain sensitive even beside a question.
    if re.search(r'[:={}\[\]"\'`]|@|\d|sk-|eyj|bearer|access.?token|refresh.?token|session.?key',lower):return False
    if not re.search(r'парол|password|токен|token|session|cookie|cvv|код\s+(?:входа|подтверждения)',lower):return False
    if not re.search(r'\?$|^(?:как|где|почему|что\s+значит)\b',lower):return False
    if not re.search(r'нуж(?:ен|на|но|ны)|требу\w*|зачем|как|где|почему|что\s+значит|надо\s+(?:ли\s+)?(?:прислать|отправить)|можно\s+ли',lower):
        if not re.fullmatch(r'(?:а\s+)?(?:пароль|токен|код\s+входа)\s+тоже\?',lower):return False
    # Unrecognised Latin text may be an unlabelled password. Public field and
    # common brand words are harmless; new names can still be handled manually.
    if any(word not in _PUBLIC_FIELD_WORDS for word in re.findall(r'[a-z]+',lower)):return False
    # "пароль qwerty нужен?" is not an approved field-name pattern even if
    # somebody deliberately uses a harmless brand as a purported secret.
    # Apple ID is the public name of the account field, not a supplied value.
    # Do not generalise this to arbitrary words after "пароль": those can be
    # actual passwords (including names of other products).
    field_syntax=re.sub(r'\b(пароль|password)\s+(apple\s+id)\b',r'\1 от \2',lower)
    if re.search(r'(?:пароль|password|токен|token)\s+(?!от\b|для\b|вам\b|мне\b|нуж\w*\b|надо\b|треб\w*\b|присыл\w*\b|отправ\w*\b|перед\w*\b|в\b|это\b|тоже\b|можно\b|обязател\w*\b)[a-zа-я]+',field_syntax):return False
    return True
