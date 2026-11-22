"""End-to-end through Service.generate with the stress model (22.11.2026).

A rule tested alone passed while the real pipeline never let it act (bench 15: the generic «что дальше» text asked for
the order number, and that silently disabled the paid-service step). These cases run the whole chain."""
import asyncio
import logging
import tempfile
import time
import unittest

from support_agent import stress_suite as ss
from support_agent.service import Service
from support_agent.storage import Store


class PipelineEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.file = tempfile.NamedTemporaryFile(suffix='.sqlite')
        cls.store = Store(cls.file.name, batch_seconds=0)
        cls.store.put('mode', 'auto')
        cls.model = ss.StressModel()
        cls.service = Service(cls.store, ss._Telegram({}), cls.model, ss._Shop(ss.shop_catalog_from_config()),
                              owner=1, allowed={1, 50}, daily_limit=5000)
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)
        cls.store.db.close()
        cls.file.close()

    def ask(self, text, **turn):
        type(self).n += 1
        conn = 'e2e%d' % self.n
        self.store.connection(conn, True, True)
        cid = self.store.ingest(conn, 50, 1, 'client', text, {}, time.time())
        self.store.prepare_now(cid, time.time())
        job = self.store.claim(time.time() + 1)
        self.model.turn = dict(turn, id='e2e')
        return asyncio.run(self.service.generate(job))

    def test_paid_suno_gets_its_own_step(self):
        out = self.ask('оплатил суно, что дальше?', owner_topic='what_next')
        self.assertIn('почту и пароль от аккаунта Suno', out['reply'])

    def test_price_question_names_the_bot(self):
        out = self.ask('сколько стоит суно?')
        self.assertIn('₽', out['reply'])
        self.assertIn('@neirosetkaa_bot', out['reply'])

    def test_unknown_service_gets_other_service(self):
        out = self.ask('нетфликс можете оплатить?', payment_asked=True)
        self.assertNotIn('Выберите нужный сервис и тариф', out['reply'])


if __name__ == '__main__':
    unittest.main()
