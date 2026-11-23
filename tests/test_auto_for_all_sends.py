"""Owner 24.11.2026 (/diag): with «AUTO для всех» drafts stayed «ждёт вас» and nothing was sent.

The last send check read the chat's own mode ('draft') instead of the effective mode. This runs the real
worker: client message → answer → sendMessage to the client."""
import asyncio
import logging
import tempfile
import time
import unittest

from support_agent import stress_suite as ss
from support_agent.service import Service
from support_agent.storage import Store


class _TG(ss._Telegram):
    async def call(self, method, **kwargs):
        self.sent.append((method, kwargs))
        if method == 'getBusinessConnection':
            return {'id': kwargs.get('business_connection_id'), 'user': {'id': 1}, 'is_enabled': True,
                    'rights': {'can_reply': True}}
        return {'message_id': 900 + len(self.sent)}


class AutoForAllSends(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.file = tempfile.NamedTemporaryFile(suffix='.sqlite')
        self.store = Store(self.file.name, batch_seconds=0)
        self.tg = _TG({})
        self.model = ss.StressModel()
        self.service = Service(self.store, self.tg, self.model, ss._Shop(ss.shop_catalog_from_config()),
                               owner=1, allowed={1, 50}, daily_limit=500, auto_for_all=True)
        self.store.switch('auto')
        self.store.connection('c1', True, True)

    def tearDown(self):
        logging.disable(logging.NOTSET)
        self.store.db.close()
        self.file.close()

    def test_client_gets_answer_by_itself(self):
        cid = self.store.ingest('c1', 50, 5, 'client', 'сколько стоит суно?', {}, time.time())
        self.assertEqual(self.store.chat(cid)['mode'], 'draft')
        self.assertEqual(self.store.effective_mode(cid), 'auto')
        self.store.prepare_now(cid, time.time())
        self.model.turn = {'id': 'x', 'situation': 'price'}
        asyncio.run(self.service.work_once())
        to_client = [kw for method, kw in self.tg.sent if method == 'sendMessage' and kw.get('chat_id') == 50]
        self.assertTrue(to_client, [m for m, _ in self.tg.sent])
        self.assertIn('₽', str(to_client[-1].get('text')))
        state = self.store.db.execute('SELECT state FROM drafts WHERE chat=? ORDER BY id DESC', (cid,)).fetchone()[0]
        self.assertEqual(state, 'sent')


if __name__ == '__main__':
    unittest.main()


class OwnerGate(unittest.TestCase):
    """Owner 24.11.2026 (@antonidrudiger): a good rescued answer was stopped by «Сбой разбора: RemoteError»."""

    def test_rescued_answer_goes_out(self):
        from support_agent.auto_policy import owner_gate
        reply = ('Оформить подписку можно в нашем боте @neirosetkaa_bot:\n1. Откройте бота и нажмите «Каталог».\n'
                 '2. Выберите сервис и нужный тариф.\n3. Оплатите по СБП или картой «Мир».')
        ev = {'status': 'owner_policy', 'reply_class': 'unverified', 'diagnostic': 'Сбой разбора: RemoteError.'}
        self.assertIsNone(owner_gate({'reply': reply, 'handoff': False}, ev, 'Привет! Тут?\nКак оплатить Гпт'))

    def test_real_stops_stay(self):
        from support_agent.auto_policy import owner_gate
        self.assertIsNotNone(owner_gate({'reply': 'Хорошо', 'handoff': False}, {'refund_hold': True}))
        self.assertIsNotNone(owner_gate({'reply': 'Александр ответит здесь', 'handoff': True}, {}))
        self.assertIsNotNone(owner_gate({'reply': 'Подписка активирована!', 'handoff': False}, {}))

    def test_failure_is_named(self):
        from support_agent.clients import RemoteError
        from support_agent.service import model_failure
        self.assertIn('429', model_failure(RemoteError('x', reason='api_http', status=429)))
        self.assertIn('150 секунд', model_failure(RemoteError('x', reason='api_timeout')))
