"""Conservative filtering. Not a guarantee that all secrets can be detected."""
import re


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
    text=re.sub(r'(?i)\b(?:gpt-\d+(?:\.\d+)?(?:-(?:mini|nano|pro|turbo|preview))?|claude-(?:sonnet|opus|haiku)-\d+(?:[-.]\d+)?|gemini-\d+(?:\.\d+)?-(?:pro|flash))(?![\w-])',
                '[название модели]',text or '')
    return bool(re.search(r'(?i)парол|password|\bpass\b|токен|token|session|cookie|cvv|код.{0,15}(?:почт|вход|подтверж)|^\s*\d{4,8}\s*$|(?=\S{8,})(?=\S*[a-zA-Z])(?=\S*\d)\S{8,}',text or ''))
