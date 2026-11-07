"""Real model-call accounting. Logs contain ids, paths and reasons, never message text."""
import logging
import time
from contextvars import ContextVar

_METER = ContextVar('support_call_meter', default=None)


def current_meter():
    return _METER.get()


def use_meter(meter):
    return _METER.set(meter)


def reset_meter(token):
    _METER.reset(token)


class CallMeter:
    """One dialog's API calls. The daily quota moves only when a request is sent."""

    def __init__(self, store, limit, chat_id):
        self.store = store
        self.limit = limit
        self.chat_id = chat_id
        self.reason = 'model'
        self.events = []

    def arm(self, reason):
        self.reason = str(reason or 'model')[:80]
        if self.store.calls_today() >= self.limit:
            raise ValueError('Daily API request limit reached')

    def consume(self, path):
        day_ok = self.store.reserve_call(_utc_day(), self.limit)
        if not day_ok:
            raise ValueError('Daily API request limit reached')
        event = {'path': str(path)[:40], 'reason': self.reason, 'at': round(time.time(), 3)}
        self.events.append(event)
        logging.info('support_api chat=%s path=%s reason=%s', self.chat_id, event['path'], event['reason'])
        self.reason = 'follow_up'
        return event


def _utc_day():
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).date().isoformat()
