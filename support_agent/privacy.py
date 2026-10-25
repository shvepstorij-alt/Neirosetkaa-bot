"""Conservative filtering. Not a guarantee that all secrets can be detected."""
import re


def scrub(text):
    text=str(text or '')[:16000]
    if re.search(r'(?i)access.?token|refresh.?token|session.?key|\bbearer\b|eyJ[\w-]{20}|sk-[\w-]{16}',text):
        return '[Скрыто: сообщение может содержать данные доступа]'
    text=re.sub(r'https?://\S+', '[ссылка скрыта]',text)
    text=re.sub(r'(?im)^.*(?:пароль|password|\bpass\s*:|\bcvv\b|код (?:входа|подтверждения))\s*[:=].*$', '[данные доступа скрыты]',text)
    text=re.sub(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', '[email скрыт]',text)
    text=re.sub(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)', '[номер скрыт]',text)
    # Voucher codes have several groups. GPT-4.1 / GPT-5 are model names, not secrets.
    text=re.sub(r'\b(?:GPT\w*|CL\w*)-(?:[A-Za-z0-9]{3,}-){2,}[A-Za-z0-9]{3,}\b', '[код скрыт]',text)
    return text


def sensitive(text):
    # Prefer manual handling over leaking unlabelled passwords or one-time codes.
    text=re.sub(r'(?i)\b(?:gpt-\d+(?:\.\d+)?(?:-(?:mini|nano|pro|turbo|preview))?|claude-(?:sonnet|opus|haiku)-\d+(?:[-.]\d+)?|gemini-\d+(?:\.\d+)?-(?:pro|flash))(?![\w-])',
                '[название модели]',text or '')
    return bool(re.search(r'(?i)парол|password|\bpass\b|токен|token|session|cookie|cvv|код.{0,15}(?:почт|вход|подтверж)|^\s*\d{4,8}\s*$|(?=\S{8,})(?=\S*[a-zA-Z])(?=\S*\d)\S{8,}',text or ''))
