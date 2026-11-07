"""End-to-end turns: stored history, situation, then the real generate() path.

PR #7 tested bind_turn on one already-saved slot and one short utterance.
It never stored the assistant question plus the next client lines, and never
ran Service.generate, so the App Store clarifier still answered from the
current message and forgot the other slot.
"""
import asyncio
import re
import tempfile
import time
import unittest

from support_agent.dialog_probe import NoTelegram
from support_agent.service import Service
from support_agent.situation import _same_question, analyze, question_targets, window
from support_agent.dialog_state import load
from support_agent.storage import Store


def apple_regions():
    return {'status': 'available', 'regions': [
        {'name': 'Россия | RU', 'denoms': [
            {'nominal': '1000 RUB', 'price_rub': 1450},
            {'nominal': '3000 RUB', 'price_rub': 4350},
        ]},
        {'name': 'Турция | TR', 'denoms': [
            {'nominal': '250 TRY', 'price_rub': 890},
            {'nominal': '500 TRY', 'price_rub': 1600},
        ]},
        {'name': 'Германия | DE', 'denoms': [
            {'nominal': '25 EUR', 'price_rub': 2800},
            {'nominal': '50 EUR', 'price_rub': 5400},
        ]},
    ]}


def shop_facts():
    catalog = [
        {'service': 'App Store', 'service_key': 'appstore', 'plan': 'код',
         'catalog_price_rub': 1, 'fulfillment': 'manual'},
        {'service': 'Suno', 'service_key': 'suno', 'plan': 'Pro',
         'catalog_price_rub': 990, 'fulfillment': 'manual'},
        {'service': 'Suno', 'service_key': 'suno', 'plan': 'Premier',
         'catalog_price_rub': 2990, 'fulfillment': 'manual'},
        {'service': 'Suno', 'service_key': 'suno', 'plan': 'Pro на 1 год',
         'catalog_price_rub': 9590, 'fulfillment': 'manual'},
        {'service': 'Cursor', 'service_key': 'cursor', 'plan': 'Pro',
         'catalog_price_rub': 2100, 'fulfillment': 'manual'},
        {'service': 'Cursor', 'service_key': 'cursor', 'plan': 'Business',
         'catalog_price_rub': 6400, 'fulfillment': 'manual'},
    ]
    return {'catalog': catalog, 'catalog_status': 'available', 'appstore': apple_regions(),
            'bot_navigation': {'status': 'available', 'username': 'neirosetkaa_bot', 'catalog_webapp': True}}


class Shop:
    def __init__(self, facts):
        self.facts_data = facts

    async def facts(self, uid):
        return dict(self.facts_data)


class LatestOnlyModel:
    """Behaves like the bug: the latest client line is the only line it sees."""

    def __init__(self):
        self.drafts = []

    async def draft(self, history, facts, image=None):
        last = ''
        for item in reversed(history or []):
            if item.get('role') == 'client':
                last = item.get('text') or ''
                break
        if re.search(r'росси|турц|герман', last, re.I) and not re.search(r'\d', last):
            reply = 'На какую сумму хотите пополнить баланс Apple ID?'
        elif re.search(r'\d', last):
            reply = 'Какой регион у вашего Apple ID?'
        else:
            reply = 'Какой сервис вас интересует?'
        self.drafts.append(reply)
        return {'reply': reply, 'summary': 'Модель смотрела только на последнюю реплику.',
                'handoff': False, 'source_ids': [], 'research': {'topic': 'none', 'products': []}}

    async def check_answer(self, answer, facts, images=None):
        return {'supported': True, 'reason': 'ok'}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(suffix='.sqlite')
        self.store = Store(self.file.name, batch_seconds=0)
        self.store.put('mode', 'draft')
        self.store.db.execute("INSERT INTO connections VALUES ('biz',1,1)")
        self.model = LatestOnlyModel()
        self.service = Service(self.store, NoTelegram(), self.model, Shop(shop_facts()),
                               owner=1, allowed={1, 50}, daily_limit=100)
        self.mid = 0
        self.cid = None

    def tearDown(self):
        self.file.close()

    def _check(self, reply):
        situation = analyze(window(self.store.db, self.cid), load(self.store.db, self.cid), shop_facts()['catalog'])
        asked = question_targets(reply)
        self.assertFalse(asked & set(situation['known']), (reply, situation['known'], situation['missing']))
        self.assertFalse(_same_question(reply, situation.get('previous_question')),
                         (reply, situation.get('previous_question')))

    def say(self, text):
        self.mid += 1
        now = time.time()
        self.cid = self.store.ingest('biz', 50, self.mid, 'client', text, {}, now)
        self.store.prepare_now(self.cid, now)
        job = self.store.claim(now + 1)
        self.assertIsNotNone(job, text)
        answer = asyncio.run(self.service.generate(job))
        self.assertTrue(answer and answer.get('reply'), text)
        self._check(answer['reply'])
        self.mid += 1
        self.store.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                              (self.cid, self.mid, 'assistant', answer['reply'], '{}', time.time(), 'done'))
        self.store.db.execute("UPDATE messages SET state='done' WHERE chat=? AND mid=? AND role='client'",
                              (self.cid, job['mid']))
        return answer['reply']

    def test_app_store_loop_keeps_region_and_amount_together(self):
        opening = self.say('Как пополнить App Store?')
        self.assertIn('регион', opening.casefold())
        self.assertIn('сумм', opening.casefold())
        self.assertNotIn('4350', opening)

        both = self.say('У меня Россия\nХочу на 3000')
        self.assertIn('4350', both)
        self.assertIn('3000', both)
        self.assertNotIn('На какую сумму', both)
        self.assertNotIn('Какой регион', both)

        for text in ('3000 рублей', 'Россия', 'На 3000р'):
            again = self.say(text)
            self.assertIn('4350', again, text)
            self.assertNotIn('На какую сумму', again, text)
            self.assertNotIn('Какой регион', again, text)
        self.assertEqual(self.model.drafts, [])

    def test_facts_can_arrive_separately_or_together_and_topics_can_change(self):
        self.say('Как пополнить баланс Apple ID?')
        only_region = self.say('У меня Турция')
        self.assertIn('сумм', only_region.casefold())
        self.assertNotIn('Какой регион', only_region)
        turkey = self.say('500 лир')
        self.assertIn('1600', turkey)
        self.assertNotIn('Какой регион', turkey)

        germany = self.say('А германия, на 50 евро')
        self.assertIn('5400', germany)
        self.assertNotIn('Турции', germany)

        suno = self.say('Сколько стоит Suno?')
        self.assertIn('990', suno)
        self.assertIn('2990', suno)
        self.assertNotIn('Какой сервис', suno)
        self.assertNotIn('Какой регион', suno)

        chosen = self.say('про')
        self.assertIn('990', chosen)
        self.assertNotIn('9590', chosen)
        self.assertNotIn('Какой сервис', chosen)

        cursor = self.say('а курсор про')
        self.assertIn('2100', cursor)
        self.assertNotIn('9590', cursor)
        self.assertNotIn('Какой сервис', cursor)


if __name__ == '__main__':
    unittest.main()
