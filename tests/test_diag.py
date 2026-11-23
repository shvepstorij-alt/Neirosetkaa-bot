"""Owner 23.11.2026: /diag shows why a client got no automatic reply."""
import asyncio
import logging
import tempfile
import time
import unittest

from support_agent import stress_suite as ss
from support_agent.service import Service
from support_agent.storage import Store
from support_agent import diag


class DiagTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.file = tempfile.NamedTemporaryFile(suffix='.sqlite')
        self.store = Store(self.file.name, batch_seconds=0)
        self.store.put('mode', 'draft')
        self.service = Service(self.store, ss._Telegram({}), ss.StressModel(), ss._Shop(ss.shop_catalog_from_config()),
                               owner=1, allowed={1, 50}, daily_limit=50, auto_for_all=True)

    def tearDown(self):
        logging.disable(logging.NOTSET)
        self.store.db.close()
        self.file.close()

    def test_report_names_the_problems(self):
        self.store.connection('c1', True, False)
        cid = self.store.ingest('c1', 50, 7, 'client', 'привет', {}, time.time())
        self.store.switch('off', cid)
        text, rows = diag.report(self.service)
        self.assertIn('только черновики', text)
        self.assertIn('право отвечать: нет', text)
        self.assertIn('на паузе', text)
        flat = [b for row in rows for b in row]
        self.assertIn(('▶️ Снять паузу во всех чатах', 'diag:unpause'), flat)
        self.assertIn(('🤖 Включить автоответы для всех', 'auto:settings'), flat)
        self.assertEqual(diag.unpause_all(self.store), 1)
        self.assertEqual(self.store.chat(cid)['mode'], 'draft')

    def test_dropped_message_is_recorded(self):
        update = {'business_message': {'chat': {'id': 77, 'type': 'private'}, 'from': {'id': 77},
                                       'message_id': 1, 'date': int(time.time()), 'text': 'привет',
                                       'business_connection_id': 'c1'}}
        asyncio.run(self.service.ingest(update))
        self.assertTrue(self.store.get('diag:last_drop').startswith('not_allowed|'))
        text, _ = diag.report(self.service)
        self.assertIn('чат не разрешён', text)


if __name__ == '__main__':
    unittest.main()
