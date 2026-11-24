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


class ScreenshotTests(unittest.TestCase):
    """Owner 24.11.2026: the assistant reads client screenshots and answers by the owner's rules."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.file = tempfile.NamedTemporaryFile(suffix='.sqlite')
        self.store = Store(self.file.name, batch_seconds=0)
        self.tg = _TG({})
        import io
        from PIL import Image
        buf = io.BytesIO(); Image.new('RGB', (64, 64), 'white').save(buf, 'PNG')
        png = buf.getvalue()

        async def download(file_id):
            return png
        self.tg.download = download
        self.model = ss.StressModel()
        self.service = Service(self.store, self.tg, self.model, ss._Shop(ss.shop_catalog_from_config()),
                               owner=1, allowed={1, 50}, daily_limit=500, auto_for_all=True)
        self.store.switch('auto')
        self.store.connection('c1', True, True)

    def tearDown(self):
        logging.disable(logging.NOTSET)
        self.store.db.close()
        self.file.close()

    def test_screenshot_is_read_and_remembered(self):
        cid = self.store.ingest('c1', 50, 5, 'client', '', {'kind': 'photo', 'file_id': 'f1', 'file_size': 500}, time.time())
        self.store.prepare_now(cid, time.time())
        self.model.turn = {'id': 'x', 'situation': 'paid_not_working',
                           'image_text': 'ChatGPT, настройки аккаунта: тариф Free, ошибка «Unsupported country»'}
        asyncio.run(self.service.work_once())
        import json
        media = json.loads(self.store.db.execute('SELECT media FROM messages WHERE chat=? AND mid=5', (cid,)).fetchone()[0])
        self.assertIn('Unsupported country', media.get('seen', ''))
        from support_agent.turn_understanding import burst_and_window
        burst, window = burst_and_window(self.store.db, cid)
        self.assertIn('[скриншот: ChatGPT', burst)
        to_client = [kw for method, kw in self.tg.sent if method == 'sendMessage' and kw.get('chat_id') == 50]
        self.assertTrue(to_client)

    def test_secret_on_screen_is_not_kept(self):
        cid = self.store.ingest('c1', 50, 5, 'client', '', {'kind': 'photo', 'file_id': 'f1', 'file_size': 500}, time.time())
        self.store.prepare_now(cid, time.time())
        self.model.turn = {'id': 'x', 'situation': 'account_login', 'image_text': 'Вход: пароль Qwerty123!'}
        asyncio.run(self.service.work_once())
        import json
        media = json.loads(self.store.db.execute('SELECT media FROM messages WHERE chat=? AND mid=5', (cid,)).fetchone()[0])
        self.assertNotIn('seen', media)


class NoticeNotRepeated(unittest.TestCase):
    """Owner 24.11.2026: «Александр ответит здесь…» is not sent twice in a row."""

    def _draft(self, prior):
        from support_agent.handoff import handoff_text
        store = Store(':memory:', batch_seconds=0)
        store.switch('auto'); store.put('auto_all', '1')
        store.connection('c1', True, True)
        now = time.time()
        cid = store.ingest('c1', 50, 5, 'client', 'а когда?', {}, now - 5)
        if prior:
            store.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", (cid, 4, 'assistant', handoff_text(), '{}', now - 60, 'stored'))
        store.prepare_now(cid, now)
        job = store.claim(now + 1)
        did = store.finish(job, 'x', '', True)
        return store, did

    def test_second_notice_is_skipped(self):
        store, did = self._draft(prior=False)
        self.assertIsNotNone(store.auto_notice(did, 'handoff'))
        store, did = self._draft(prior=True)
        self.assertIsNone(store.auto_notice(did, 'handoff'))
