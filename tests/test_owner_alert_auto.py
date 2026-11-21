"""Owner 21.11.2026: in AUTO the owner gets one note when he is needed, not one per reply."""
import asyncio
import unittest
from types import SimpleNamespace

from support_agent.service import Service


class _Store:
    def __init__(self, mode='auto'):
        self.mode, self.kv = mode, {}
        self.db = SimpleNamespace(execute=lambda q, a: SimpleNamespace(fetchone=lambda: ((self.kv[a[0]],) if a[0] in self.kv else None)))

    def effective_mode(self, cid):
        return self.mode

    def put(self, k, v):
        self.kv[k] = v


def _service(mode='auto'):
    svc = Service.__new__(Service)
    svc.store = _Store(mode)
    svc.sent = []

    async def owner_message(text):
        svc.sent.append(text)
    svc.owner_message = owner_message
    svc.chat_identity = lambda cid: 'Чат %s' % cid
    return svc


ANSWER = {'reply': 'Подскажите номер заказа…', 'handoff': False,
          'evidence': {'owner_alert': True, 'hard_routes': ['refund_request']}}


class OwnerAlertAuto(unittest.TestCase):
    def test_one_note_per_chat(self):
        svc = _service()
        asyncio.run(svc.maybe_owner_alert({'chat': 1}, ANSWER))
        asyncio.run(svc.maybe_owner_alert({'chat': 1}, ANSWER))
        self.assertEqual(len(svc.sent), 1)
        self.assertIn('возврат', svc.sent[0])

    def test_draft_mode_has_its_card(self):
        svc = _service('draft')
        asyncio.run(svc.maybe_owner_alert({'chat': 1}, ANSWER))
        self.assertEqual(svc.sent, [])

    def test_plain_reply_is_silent(self):
        svc = _service()
        asyncio.run(svc.maybe_owner_alert({'chat': 1}, {'reply': 'ok', 'handoff': False, 'evidence': {}}))
        self.assertEqual(svc.sent, [])

    def test_urgent_conflict_uses_its_own_note(self):
        svc = _service()
        asyncio.run(svc.maybe_owner_alert({'chat': 1}, {'reply': 'x', 'handoff': False,
                                                         'evidence': {'owner_alert': True, 'urgent': True}}))
        self.assertEqual(svc.sent, [])


if __name__ == '__main__':
    unittest.main()
