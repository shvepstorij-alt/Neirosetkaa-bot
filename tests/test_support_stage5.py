"""Conflict, the owner's own words, the daily limit and a fresh catalogue price."""
import asyncio
import tempfile
import time
import unittest

from support_agent.auto_policy import block_reason
from support_agent.dialog_probe import NoTelegram
from support_agent.handoff import HANDOFF
from support_agent.service import Service
from support_agent.storage import Store
from support_agent.turn_understanding import UNDERSTANDING_INSTRUCTIONS


class RecordingTelegram(NoTelegram):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def call(self, method, **kwargs):
        self.sent.append((method, kwargs.get('text', '')))
        return {'message_id': 1}


def catalog():
    return {'catalog': [{'service': 'Zoom', 'service_key': 'zoom', 'plan': 'Pro',
                         'catalog_price_rub': 1000, 'fulfillment': 'account_details'}],
            'catalog_status': 'available',
            'appstore': {'status': 'not_connected', 'regions': []},
            'bot_navigation': {'status': 'available', 'username': 'neirosetkaa_bot', 'catalog_webapp': True}}


class Shop:
    def __init__(self):
        self.calls = 0
        self.fail = False

    async def facts(self, uid):
        self.calls += 1
        if self.fail:
            raise RuntimeError('catalog down')
        return catalog()


class Model:
    def __init__(self):
        self.calls = 0
        self.alignment = 'follows'
        self.conflict = 'none'
        self.reason = ''

    async def understand_turn(self, context, images=None):
        self.calls += 1
        self.context = context
        burst = (context.get('burst') or '').strip()
        client_lines = [item.get('text') or '' for item in context.get('history') or [] if item.get('role') == 'client']
        conflict = self.conflict
        reason = self.reason
        if client_lines.count(burst) >= 2 and burst:
            conflict = 'high'
            reason = 'Тот же вопрос повторяется без решения.'
        price = 'price' in burst.casefold() or 'стоит' in burst.casefold()
        reply = 'Zoom Pro: 1000 ₽.' if price else 'Откройте сообщение заказа в боте и идите по шагу, который там указан.'
        if context.get('owner_statements') and self.alignment == 'follows':
            reply = 'Продолжаю от вашей реплики: токен вставляется в окно бота.'
        if self.alignment == 'contradicts':
            reply = 'Нет, делайте по-другому.'
        if self.alignment == 'reasks':
            reply = 'Куда вставлять токен?'
        return {'intent': 'price' if price else 'how_to', 'service_id': 'zoom', 'tariff_id': 'zoom|Pro',
                'confidence': 'high', 'clarify_ids': [], 'topic_changed': False,
                'known_facts': [], 'missing_facts': [], 'questions': [burst or 'вопрос'],
                'vpn_advice': False, 'conflict': conflict, 'conflict_reason': reason,
                'owner_alignment': self.alignment, 'risk': 'catalog_price' if price else 'instruct',
                'order_number': '', 'receipt_amount_unverified': '',
                'reply': reply, 'summary': 'Проверка хода.'}


class Stage5Tests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(suffix='.sqlite')
        self.store = Store(self.file.name, batch_seconds=0)
        self.store.put('mode', 'auto')
        self.store.connection('biz', True, True)
        self.shop = Shop()
        self.tg = RecordingTelegram()
        self.model = Model()
        self.service = Service(self.store, self.tg, self.model, self.shop, owner=1, allowed={1, 50}, daily_limit=100)
        self.mid = 0

    def tearDown(self):
        self.file.close()

    def ask(self, text, role='client'):
        self.mid += 1
        now = time.time()
        cid = self.store.ingest('biz', 50, self.mid, role, text, {}, now)
        self.store.db.execute("UPDATE chats SET mode='auto' WHERE id=?", (cid,))
        if role != 'client':
            return None
        self.store.prepare_now(cid, now)
        job = self.store.claim(now + 1)
        if job is None:
            return None
        answer = asyncio.run(self.service.generate(job))
        self.store.db.execute("UPDATE messages SET state='done' WHERE chat=? AND mid=?", (cid, job['mid']))
        self.mid += 1
        self.store.ingest('biz', 50, self.mid, 'assistant', 'ок', {}, now)
        return answer

    def test_high_conflict_holds_and_alerts_once(self):
        self.assertIn('conflict', UNDERSTANDING_INSTRUCTIONS)
        self.model.conflict = 'high'
        self.model.reason = 'Клиент требует возврат и грозит жалобой.'
        first = self.ask('верните деньги')
        self.assertTrue(first['handoff'])
        self.assertTrue(first['evidence']['conflict_hold'])
        self.assertIsNotNone(block_reason(first))
        notes = [text for method, text in self.tg.sent if 'Конфликт' in text]
        self.assertEqual(len(notes), 1)
        self.assertIn('жалоб', notes[0])
        self.assertIn('верните деньги', notes[0])
        second = self.ask('и ещё раз верните')
        self.assertTrue(second['handoff'])
        notes = [text for method, text in self.tg.sent if 'Конфликт' in text]
        self.assertEqual(len(notes), 1)

    def test_annoyed_client_still_gets_the_answer(self):
        self.model.conflict = 'annoyed'
        answer = self.ask('как открыть')
        self.assertFalse(answer['handoff'])
        self.assertFalse(any('Конфликт' in text for method, text in self.tg.sent))

    def test_repeated_question_is_a_high_conflict(self):
        self.ask('где заказ')
        self.mid += 1
        self.store.ingest('biz', 50, self.mid, 'assistant', 'Уточните номер.', {}, time.time())
        again = self.ask('где заказ')
        self.assertTrue(again['handoff'])
        self.assertTrue(again['evidence']['conflict_hold'])
        self.assertTrue(any('Конфликт' in text for method, text in self.tg.sent))

    def test_owner_words_pause_then_lead_the_next_reply(self):
        self.ask('как активировать')
        self.ask('Токен вставляется в окно бота, не в эту переписку.', role='owner')
        paused = self.ask('а что дальше')
        self.assertIsNone(paused)
        cid = self.store.db.execute('SELECT id FROM chats').fetchone()[0]
        self.store.db.execute('UPDATE chats SET manual_pause_until=0 WHERE id=?', (cid,))
        self.store.prepare_now(cid, time.time())
        job = self.store.claim(time.time() + 1)
        answer = asyncio.run(self.service.generate(job))
        history = self.service._last_turn_context['history']
        owner = [item for item in history if item.get('authority') == 'owner']
        self.assertEqual(len(owner), 1)
        self.assertEqual(owner[0]['role'], 'owner')
        self.assertIn('окно бота', owner[0]['text'])
        self.assertIn('окно бота', answer['reply'])
        self.assertFalse(answer['handoff'])
        self.model.alignment = 'contradicts'
        clash = self.ask('тогда скажите ещё раз')
        self.assertTrue(clash['handoff'])
        self.assertTrue(clash['evidence']['owner_alignment_hold'])
        self.model.alignment = 'reasks'
        reask = self.ask('и куда всё-таки')
        self.assertTrue(reask['handoff'])
        self.assertTrue(reask['evidence']['owner_alignment_hold'])

    def test_daily_limit_warns_and_then_uses_the_hold_phrase(self):
        self.service.daily_limit = 5
        for _ in range(4):
            self.ask('как открыть')
        eighties = [text for method, text in self.tg.sent if '80%' in text]
        self.assertEqual(len(eighties), 1)
        self.ask('как открыть')
        full = [text for method, text in self.tg.sent if 'исчерпан' in text]
        self.assertEqual(len(full), 1)
        calls = self.model.calls
        blocked = self.ask('как открыть')
        self.assertTrue(blocked['handoff'])
        self.assertTrue(blocked['reply'].startswith(HANDOFF))
        self.assertEqual(self.model.calls, calls)
        self.assertEqual(len([text for method, text in self.tg.sent if 'исчерпан' in text]), 1)
        self.assertEqual(len([text for method, text in self.tg.sent if '80%' in text]), 1)

    def test_stale_catalog_is_reread_before_a_price(self):
        first = self.ask('сколько стоит')
        self.assertFalse(first['handoff'])
        self.assertIn('1000', first['reply'])
        self.assertEqual(self.shop.calls, 1)
        second = self.ask('сколько стоит ещё раз')
        self.assertEqual(self.shop.calls, 1)
        self.assertFalse(second['handoff'])
        self.service.catalog_cache._rows[50]['at'] = time.time() - 1000
        self.shop.fail = True
        held = self.ask('сколько стоит теперь')
        self.assertEqual(self.shop.calls, 2)
        self.assertTrue(held['handoff'])
        self.assertTrue(held['evidence']['stale_price_hold'])
        self.service.catalog_cache._rows[50]['at'] = time.time() - 1000
        guide = self.ask('как открыть')
        self.assertFalse(guide['handoff'])
        self.assertNotIn('1000', guide['reply'])


if __name__ == '__main__':
    unittest.main()
