"""Owner 20.11.2026 (№302, systemic): the draft card must render for every kind of answer.

A new answer type that lacks a service field must never cost the owner his draft and buttons.
"""
import asyncio
import tempfile
import time
import unittest

from support_agent.dialog_probe import NoTelegram
from support_agent.service import Service
from support_agent.storage import Store


class Recording(NoTelegram):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return {'message_id': len(self.calls)}


class Shop:
    async def facts(self, uid):
        return {'catalog': [], 'catalog_status': 'available'}


EVIDENCE_KINDS = {
    'paid_notice': {'status': 'catalog_direct', 'paid_notice': {'order': '1'}, 'owner_text_used': True},
    'checked_without_time': {'status': 'checked', 'sources': []},
    'checked_with_sources': {'status': 'checked', 'checked_at': time.time(),
                             'sources': [{'id': 'S1', 'url': 'https://example.com'}],
                             'discoveries': {'x': {'corroboration_url': 'https://example.com/a'}}, 'failures': {'y': 'нет'}},
    'insufficient': {'status': 'insufficient'},
    'owner_policy': {'status': 'owner_policy', 'handoff_reason': 'нужен Александр'},
    'unavailable': {'status': 'unavailable', 'diagnostic': 'сбой поиска'},
    'auto_blocked': {'status': 'catalog_direct', 'auto_block_reason': 'ситуация не в списке'},
    'owner_step': {'status': 'owner_policy', 'owner_step_hold': True, 'handoff_reason': 'Александр ведёт шаг'},
    'situation_hold': {'status': 'catalog_direct', 'situation_hold': True},
    'secret': {'status': 'owner_policy', 'manual_secret_intake': True},
    'atomic': {'status': 'catalog_review', 'atomic_diagnostics': ['утверждение 1', 2]},
    'empty': {},
    'odd_types': {'status': None, 'sources': None, 'discoveries': None, 'diagnostic': 5},
}


class DraftCardTests(unittest.TestCase):
    def _draft(self, evidence, text='Ответ клиенту <b>тест</b> & ещё'):
        self.file = tempfile.NamedTemporaryFile(suffix='.sqlite')
        store = Store(self.file.name, batch_seconds=0)
        store.put('mode', 'draft')
        store.connection('biz', True, True)
        tg = Recording()
        service = Service(store, tg, object(), Shop(), owner=1, allowed={1, 50}, daily_limit=10)
        now = time.time()
        cid = store.ingest('biz', 50, 1, 'client', 'Вопрос', {}, now)
        store.prepare_now(cid, now)
        job = store.claim(now + 1)
        did = store.finish(job, text, 'Разбор', bool(evidence.get('handoff_reason')), evidence)
        return service, tg, did

    def test_every_kind_renders_the_full_card(self):
        for name, evidence in EVIDENCE_KINDS.items():
            with self.subTest(kind=name):
                service, tg, did = self._draft(dict(evidence))
                asyncio.run(service.show_draft(did))
                sent = [kw.get('text', '') for method, kw in tg.calls if method == 'sendMessage']
                self.assertTrue(sent, name)
                self.assertNotIn('Полная карточка не собралась', sent[-1], name)
                asyncio.run(service.show_draft(did, view='info'))

    def test_broken_card_still_gives_draft_and_buttons(self):
        service, tg, did = self._draft({'status': 'catalog_direct'})

        async def broken(*args, **kwargs):
            raise KeyError('checked_at')
        service._show_draft_full = broken
        asyncio.run(service.show_draft(did))
        method, kwargs = [c for c in tg.calls if c[0] == 'sendMessage'][-1]
        self.assertIn('Ответ клиенту', kwargs['text'])
        self.assertIn('KeyError', kwargs['text'])
        self.assertIn('reply_markup', kwargs)
