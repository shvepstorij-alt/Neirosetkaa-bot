"""Owner handoff wording. Working hours are 10:00–22:00 Europe/Moscow."""
import datetime as dt
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo('Europe/Moscow')
HANDOFF = 'Александр будет на связи в течение получаса-часа'
OFF_HOURS = ('Сейчас вне обычного времени 10:00–22:00 по Москве, '
             'Александр может ответить в рабочий день или с задержкой')
CONTINUE = 'Пока могу помочь с другими вопросами)'


def outside_working_hours(now=None):
    current = now or dt.datetime.now(MOSCOW)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MOSCOW)
    else:
        current = current.astimezone(MOSCOW)
    return not 10 <= current.hour < 22


def handoff_text(now=None):
    parts = [HANDOFF]
    if outside_working_hours(now):
        parts.append(OFF_HOURS)
    parts.append(CONTINUE)
    return '. '.join(parts)
