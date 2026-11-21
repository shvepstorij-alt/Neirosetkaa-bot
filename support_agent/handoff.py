"""Owner handoff wording. Owner 21.11.2026: he is online almost 24/7 — never promise times."""
import datetime as dt
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo('Europe/Moscow')
HANDOFF = 'Александр ответит здесь при первой возможности'
OFF_HOURS = ('Сейчас вне обычного времени 10:00–22:00 по Москве, '
             'Александр может ответить в рабочий день или с задержкой')
CONTINUE = 'Пока могу помочь с другими вопросами)'


def outside_working_hours(now=None):
    current = now or dt.datetime.now(MOSCOW)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MOSCOW)
    else:
        current = current.astimezone(MOSCOW)
    return False  # owner 21.11.2026: almost 24/7, no off-hours note


def handoff_text(now=None):
    parts = [HANDOFF]
    if outside_working_hours(now):
        parts.append(OFF_HOURS)
    parts.append(CONTINUE)
    return '. '.join(parts)
