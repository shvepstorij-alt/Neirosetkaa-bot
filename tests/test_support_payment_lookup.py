"""Paid claims: ask for the number and the receipt, then read only this user's order."""
import asyncio
import re
import tempfile
import time
import unittest

from support_agent.auto_notice import text_for
from support_agent.auto_policy import block_reason
from support_agent.dialog_probe import NoTelegram
from support_agent.handoff import HANDOFF
from support_agent.service import Service
from support_agent.storage import Store

OTHER_USER_AMOUNT = 777777


class RecordingTelegram(NoTelegram):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return {'message_id': 1}


class Shop:
    def __init__(self):
        self.calls = []

    async def facts(self, uid):
        return {'catalog': [{'service': 'ChatGPT', 'service_key': 'chatgpt', 'plan': 'Plus',
                             'catalog_price_rub': 1990, 'fulfillment': 'code_activation_conditional'}],
                'catalog_status': 'available',
                'appstore': {'status': 'available', 'regions': []},
                'bot_navigation': {'status': 'available', 'username': 'neirosetkaa_bot', 'catalog_webapp': True}}

    async def order_facts(self, uid, reference):
        self.calls.append((uid, dict(reference)))
        number = str(reference.get('untyped') or '')
        base = {'ownership_scope': 'authenticated_user', 'orders': []}
        if uid != 50 or number not in ('555', '556'):
            return dict(base, orders_status='not_found')
        if number == '556':
            return dict(base, orders_status='available', orders=[{
                'num': '556', 'fk_intid': '900', 'status': 'pending', 'amount_rub': 1990}])
        return dict(base, orders_status='available', orders=[{
            'num': '555', 'fk_intid': '901', 'status': 'paid', 'amount_rub': 1990,
            'service_key': 'chatgpt', 'plan_name': 'Plus', 'activation_status': 'pending_window',
            'activation_note': 'В боте ещё открыто окно активации. Это не подтверждение, что подписка уже включилась.'}])


class PayModel:
    def __init__(self):
        self.drafts = []

    async def understand_turn(self, context):
        burst = (context.get('burst') or '').strip()
        numbers = re.findall(r'(?<!\d)(\d{3,18})(?!\d)', burst)
        if numbers:
            amount = '100' if 'сумма 100' in burst else ''
            candidates = [item for item in numbers if item != amount]
            number = candidates[-1] if candidates else numbers[-1]
            return {'intent': 'payment_issue', 'service_id': 'chatgpt', 'tariff_id': 'chatgpt|Plus',
                    'topic_changed': False, 'known_facts': ['order_number'], 'missing_facts': [],
                    'risk': 'collect', 'order_number': number, 'receipt_amount_unverified': amount,
                    'reply': 'Принял номер.', 'summary': 'Номер заказа для проверки.'}
        return {'intent': 'payment_issue', 'service_id': 'chatgpt', 'tariff_id': '',
                'topic_changed': False, 'known_facts': [], 'missing_facts': ['order_number', 'receipt'],
                'risk': 'collect', 'order_number': '', 'receipt_amount_unverified': '',
                'reply': ('Пришлите номер заказа из сообщения бота или номер платежа FreeKassa '
                          'и чек об оплате из банка: скриншот или PDF.'),
                'summary': 'Нужны номер и чек. Оплата не подтверждена.'}

    async def draft_with_facts(self, facts):
        self.drafts.append(facts)
        note = facts.get('next_step') or facts.get('activation_note') or 'Откройте сообщение заказа в боте.'
        receipt = ' Пришлите также чек об оплате из банка: скриншот или PDF.' if facts.get('receipt_missing') else ''
        return {'reply': 'Магазин учёл оплату заказа №' + str(facts.get('order_number')) + '. Это не значит, что подписка уже включилась. ' + note + receipt,
                'summary': 'Следующий шаг по оплаченному заказу. Активация не подтверждена.',
                'service_id': facts.get('service_key') or 'chatgpt'}


class PaymentLookupTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(suffix='.sqlite')
        self.store = Store(self.file.name, batch_seconds=0)
        self.store.put('mode', 'auto')
        self.store.connection('biz', True, True)
        self.shop = Shop()
        self.tg = RecordingTelegram()
        self.model = PayModel()
        self.service = Service(self.store, self.tg, self.model, self.shop, owner=1, allowed={1, 50}, daily_limit=100)
        self.mid = 0

    def tearDown(self):
        self.file.close()

    def say(self, text, media=None):
        self.mid += 1
        now = time.time()
        cid = self.store.ingest('biz', 50, self.mid, 'client', text, media or {}, now)
        self.store.db.execute("UPDATE chats SET mode='auto' WHERE id=?", (cid,))
        self.store.prepare_now(cid, now)
        job = self.store.claim(now + 1)
        self.service._job = job
        answer = asyncio.run(self.service.generate(job))
        self.store.db.execute("UPDATE messages SET state='done' WHERE chat=? AND mid=?", (cid, job['mid']))
        return answer

    def owner_text(self):
        return '\n'.join(kwargs.get('text', '') for method, kwargs in self.tg.calls if method == 'sendMessage')

    def test_paid_flow_asks_then_looks_up_only_this_user(self):
        asked = self.say('я оплатил')
        self.assertEqual(self.shop.calls, [])
        self.assertIn('номер заказа', asked['reply'].casefold())
        self.assertIn('чек', asked['reply'].casefold())
        self.assertFalse(asked['handoff'])

        paid = self.say('555')
        self.assertEqual(self.shop.calls[-1][0], 50)
        self.assertIn('555', str(self.shop.calls[-1][1]))
        self.assertIn('окно активации в боте', paid['reply'])
        self.assertIn('не в эту переписку', paid['reply'])
        self.assertIn('чек', paid['reply'].casefold())
        self.assertNotIn('активирована', paid['reply'].casefold())
        self.assertFalse(paid['handoff'])
        self.assertIsNone(block_reason(paid, current_text='555'))

        with_receipt = self.say('555', {'kind': 'photo', 'file_id': 'receipt-photo-1'})
        self.assertNotIn('чек об оплате', with_receipt['reply'].casefold())
        self.assertIn('окно активации', with_receipt['reply'])
        self.assertTrue(any(method == 'sendPhoto' and kwargs.get('photo') == 'receipt-photo-1'
                            for method, kwargs in self.tg.calls))
        self.assertNotIn('card', self.owner_text().casefold())

        pending = self.say('556')
        self.assertTrue(pending['handoff'])
        self.assertTrue(pending['reply'].startswith(HANDOFF))
        note = self.owner_text()
        self.assertIn('556', note)
        self.assertIn('pending', note)
        self.assertIn('1990', note)
        self.assertEqual(text_for('human')[:len(HANDOFF)], HANDOFF)

        missing = self.say('777')
        self.assertTrue(missing['handoff'])
        self.assertIn('not_found', self.owner_text())
        self.assertNotIn(str(OTHER_USER_AMOUNT), self.owner_text())

    def test_pdf_receipt_is_forwarded_without_its_bytes(self):
        now = int(time.time())
        asyncio.run(self.service.ingest({'business_message': {
            'business_connection_id': 'biz', 'chat': {'type': 'private', 'id': 50},
            'from': {'id': 50}, 'message_id': 40, 'date': now,
            'document': {'file_id': 'receipt-pdf-1', 'mime_type': 'application/pdf',
                         'file_name': 'card-4111.pdf'},
            'caption': 'карта 4111111111111111'}}))
        self.mid = 40
        stored = self.store.db.execute("SELECT text, media FROM messages WHERE mid=40").fetchone()
        self.assertNotIn('4111111111111111', stored['text'])
        self.assertIn('receipt-pdf-1', stored['media'])
        self.assertNotIn('card-4111', stored['media'])
        answer = self.say('555')
        self.assertFalse(answer['handoff'])
        self.assertTrue(any(method == 'sendDocument' and kwargs.get('document') == 'receipt-pdf-1'
                            for method, kwargs in self.tg.calls))
        self.assertNotIn('4111111111111111', self.owner_text())

    def test_unverified_receipt_amount_does_not_confirm_payment(self):
        answer = self.say('555 сумма 100')
        self.assertTrue(answer['handoff'])
        self.assertTrue(answer['reply'].startswith(HANDOFF))
        self.assertIn('не совпала', self.owner_text())
        self.assertEqual(self.model.drafts, [])


if __name__ == '__main__':
    unittest.main()
