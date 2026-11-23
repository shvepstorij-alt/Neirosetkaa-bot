"""Owner 24.11.2026: the assistant learns from live dialogs; Alexander approves every lesson."""
import asyncio
import json
import logging
import time
import unittest

from support_agent import stress_suite as ss
from support_agent import learning
from support_agent.service import Service
from support_agent.storage import Store


class _AI(ss.StressModel):
    model = 'test'

    def __init__(self):
        super().__init__()
        self.requests = []

    async def request(self, path, **kwargs):
        self.requests.append(kwargs['json'])
        out = {'lessons': [{'text': 'Когда клиент спрашивает, как оплатить, сразу давай шаги в @neirosetkaa_bot и предлагай помочь с выбором тарифа.', 'why': 'Александр дописал шаги'},
                           {'text': 'ChatGPT Plus стоит 2000 ₽', 'why': 'цена'}]}
        return {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(out, ensure_ascii=False)}]}]}


class LearningTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.store = Store(':memory:', batch_seconds=0)
        self.tg = ss._Telegram({})
        self.ai = _AI()
        self.service = Service(self.store, self.tg, self.ai, ss._Shop(ss.shop_catalog_from_config()),
                               owner=1, allowed={1, 50}, daily_limit=500, auto_for_all=True)
        self.store.connection('c1', True, True)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def _case(self):
        now = time.time() - 600
        cid = self.store.ingest('c1', 50, 5, 'client', 'как оплатить гпт', {}, now)
        self.store.db.execute("INSERT INTO drafts(chat,mid,revision,global_revision,text,summary,handoff,created,state) VALUES (?,?,?,?,?,?,?,?,?)",
                              (cid, 5, 0, 0, 'Уточните, о каком сервисе речь.', '', 0, now, 'sent'))
        self.store.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
                              (cid, 6, 'owner', 'Откройте @neirosetkaa_bot → Каталог → ChatGPT → тариф, оплата по СБП', '{}', now + 60, 'stored'))
        return cid

    def test_owner_follow_up_becomes_a_proposal_then_a_rule(self):
        self._case()
        cases = learning.signals(self.store.db, time.time() - 86400)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]['kind'], 'ответ ушёл, но Александр дописал клиенту')
        asyncio.run(self.service.learn_now(manual=True))
        rows = self.store.db.execute("SELECT id,text,state FROM lessons").fetchall()
        self.assertEqual(len(rows), 1)  # the price lesson is dropped: prices come only from the catalog
        self.assertEqual(rows[0]['state'], 'proposed')
        self.assertEqual(learning.active(self.store.db), [])
        self.assertEqual(learning.signals(self.store.db, time.time() - 86400), [])  # each case is used once
        asyncio.run(self.service.command('lesson:ok:%d' % rows[0]['id']))
        self.assertEqual(len(learning.active(self.store.db)), 1)
        self.assertIn('УРОКИ АЛЕКСАНДРА', learning.prompt_block(learning.active(self.store.db)))

    def test_lessons_reach_the_model(self):
        learning.ensure(self.store.db)
        self.store.db.execute("INSERT INTO lessons(text,why,state,created) VALUES ('Всегда предлагай помочь с выбором тарифа.','', 'active', 0)")
        seen = {}
        original = self.ai.understand_turn

        async def spy(context, images=None):
            seen['lessons'] = context.get('owner_lessons')
            return await original(context, images)
        self.ai.understand_turn = spy
        self.store.switch('auto')
        cid = self.store.ingest('c1', 50, 9, 'client', 'сколько стоит суно?', {}, time.time())
        self.store.prepare_now(cid, time.time())
        self.ai.turn = {'id': 'x', 'situation': 'price'}
        asyncio.run(self.service.work_once())
        self.assertEqual(seen.get('lessons'), ['Всегда предлагай помочь с выбором тарифа.'])

    def test_lesson_can_be_removed_and_edited(self):
        learning.ensure(self.store.db)
        lid = self.store.db.execute("INSERT INTO lessons(text,why,state,created) VALUES ('Старый урок про оплату.','', 'active', 0)").lastrowid
        self.assertTrue(learning.edit(self.store.db, lid, 'Новый урок про оплату в боте.'))
        self.assertEqual(learning.active(self.store.db), ['Новый урок про оплату в боте.'])
        learning.remove(self.store.db, lid)
        self.assertEqual(learning.active(self.store.db), [])


if __name__ == '__main__':
    unittest.main()
