"""A payment problem asks for the order facts. It does not announce an operation result."""
import asyncio
import tempfile
import time
import unittest

from support_agent.auto_notice import TEXTS, kind_for, text_for
from support_agent.auto_policy import block_reason
from support_agent.dialog_probe import NoTelegram
from support_agent.handoff import HANDOFF
from support_agent.reply_assertion import asserts_outcome, collects_case_data
from support_agent.service import Service, present_outgoing
from support_agent.storage import Store

REMOVED = ('Результат операции пока не подтверждён. Могу объяснить порядок действий по сообщению заказа '
           'или тексту ошибки без личных данных; оплату, возврат или активацию без проверки не подтверждаю. '
           'С другими вопросами тоже помогу.')


class Shop:
    async def facts(self, uid):
        return {'catalog': [{'service': 'ChatGPT', 'service_key': 'chatgpt', 'plan': 'Plus',
                             'catalog_price_rub': 1990, 'fulfillment': 'code_activation_conditional'}],
                'catalog_status': 'available', 'appstore': {'status': 'available', 'regions': []},
                'bot_navigation': {'status': 'available', 'username': 'neirosetkaa_bot', 'catalog_webapp': True}}

    async def order_facts(self, uid, query):
        return {'orders_status': 'not_connected', 'ownership_scope': 'authenticated_user', 'orders': []}


class QuietModel:
    async def draft(self, history, facts, image=None):
        return {'reply': 'Могу подсказать по каталогу.', 'summary': 'Общий ответ.',
                'handoff': False, 'source_ids': [], 'research': {'topic': 'none', 'products': []}}

    async def check_answer(self, answer, facts, images=None):
        return {'supported': True, 'reason': 'ok'}


class PaidActivationTests(unittest.TestCase):
    def test_screenshot_sequence_sends_the_request_not_a_template(self):
        store = Store(tempfile.NamedTemporaryFile(suffix='.sqlite').name, batch_seconds=0)
        store.put('mode', 'auto')
        store.connection('biz', True, True)
        now = time.time()
        cid = store.ingest('biz', 50, 1, 'client', 'Куда платить за ChatGPT Plus?', {}, now)
        store.db.execute("UPDATE chats SET mode='auto' WHERE id=?", (cid,))
        store.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                         (cid, 2, 'assistant', 'Для оплаты подписки ChatGPT Plus откройте каталог и выберите Plus.',
                          '{}', now, 'done'))
        store.ingest('biz', 50, 3, 'client', 'Оплатил, но не могу активировать', {}, now + 1)
        store.prepare_now(cid, now + 1)
        store.db.execute("UPDATE messages SET state='done' WHERE chat=? AND mid=1", (cid,))
        job = store.claim(now + 2)
        self.assertEqual(job['mid'], 3)
        service = Service(store, NoTelegram(), QuietModel(), Shop(), owner=1, allowed={1, 50}, daily_limit=100)
        answer = asyncio.run(service.generate(job))
        presented, replaced = present_outgoing(answer, introduced=True)
        self.assertFalse(replaced)
        reply = presented['reply']
        self.assertNotIn(REMOVED, reply)
        for canned in TEXTS.values():
            self.assertNotEqual(reply, canned)
        self.assertNotEqual(reply, text_for('human'))
        self.assertIn('номер заказа', reply.casefold())
        self.assertIn('FreeKassa', reply)
        self.assertIn('скриншот', reply.casefold())
        self.assertIn('Александр', reply)
        self.assertIn('окно активации в боте', reply)
        self.assertFalse(asserts_outcome(reply))
        self.assertTrue(collects_case_data(reply))
        self.assertIsNone(block_reason(presented, presented['evidence'], 'Оплатил, но не могу активировать'))
        did = store.finish(job, presented['reply'], presented['summary'], False, presented['evidence'])
        draft = store.db.execute('SELECT * FROM drafts WHERE id=?', (did,)).fetchone()
        self.assertIsNone(service.auto_content_reason(draft))

    def test_held_outcome_uses_the_handoff_sentence(self):
        reply = 'Заказ №10: оплата отмечена в магазине как полученная. Подписка активирована.'
        self.assertTrue(asserts_outcome(reply))
        evidence = {'status': 'order_status', 'sources': [], 'owner_action_required': True}
        reason = block_reason({'reply': reply, 'summary': 'Статус назван.', 'handoff': True, 'evidence': evidence},
                              evidence, 'Оплатил, но не могу активировать')
        self.assertIsNotNone(reason)
        kind = kind_for(evidence, reason, 'Оплатил, но не могу активировать', usable_reply=True)
        self.assertEqual(kind, 'human')
        notice = text_for(kind)
        self.assertTrue(notice.startswith(HANDOFF))
        self.assertNotIn('Результат операции пока не подтверждён', notice)
        self.assertNotIn('manual', TEXTS)


if __name__ == '__main__':
    unittest.main()
