"""Tariff choice follows the list just shown, for every catalogue service."""
import asyncio
import tempfile
import time
import unittest

from support_agent.auto_policy import block_reason, consultation_evidence
from support_agent.catalog_context import ALIASES
from support_agent.dialog_probe import NoTelegram
from support_agent.service import Service, present_outgoing
from support_agent.storage import Store
from support_agent.tariff_choice import resolve_selection, suggest


PLANS = (('Pro', 1890), ('Business', 2290), ('Pro на год', 16990), ('Business на год', 21990))


def catalog():
    rows = []
    for key in ALIASES:
        if key in ('appstore', 'telegram_premium'):
            continue
        service = key.replace('_', ' ').title()
        for plan, price in PLANS:
            rows.append({'service': service, 'service_key': key, 'plan': plan,
                         'catalog_price_rub': price, 'fulfillment': 'account_details'})
    rows.append({'service': 'Telegram Premium', 'service_key': 'telegram_premium', 'plan': '1 месяц',
                 'catalog_price_rub': 299, 'fulfillment': 'account_details'})
    return rows


def zoom_rows():
    return [row for row in catalog() if row['service_key'] == 'zoom' and row['plan'] in dict(PLANS)]


class ResolverTests(unittest.TestCase):
    def test_zoom_screenshot_phrases_do_not_widen(self):
        candidates = [{'id': 'zoom|' + plan, 'service': 'Zoom', 'plan': plan, 'price': price}
                      for plan, price in PLANS]
        expectations = {
            'Тариф про': 'Pro',
            'Тариф про на месяц': 'Pro',
            'Про': 'Pro',
            'годовой про': 'Pro на год',
            'бизнес': 'Business',
            'тариф бизнес на год': 'Business на год',
        }
        for text, plan in expectations.items():
            choice = suggest(candidates, text)
            self.assertEqual(choice['decision'], 'chosen', text)
            self.assertEqual(choice['row']['plan'], plan, text)
            self.assertLessEqual(set(row['plan'] for row in choice['candidates']), set(dict(PLANS)))

    def test_every_service_plan_can_be_named(self):
        phrases = {
            'Pro': ['pro', 'PRO', 'про', 'тариф про', 'про на месяц'],
            'Business': ['business', 'бизнес'],
            'Pro на год': ['pro на год', 'годовой про'],
            'Business на год': ['business на год', 'годовой бизнес'],
        }
        for key in ALIASES:
            if key == 'appstore':
                continue
            candidates = [{'id': key + '|' + plan, 'service': key, 'plan': plan, 'price': price}
                          for plan, price in PLANS]
            for plan, uttered in phrases.items():
                for text in uttered:
                    choice = suggest(candidates, text)
                    self.assertEqual(choice['decision'], 'chosen', (key, text))
                    self.assertEqual(choice['row']['plan'], plan, (key, text))
                    self.assertEqual(choice['row']['price'], dict(PLANS)[plan])

    def test_model_cannot_pick_an_id_outside_the_list_or_a_year_without_asking(self):
        candidates = [{'id': 'zoom|' + plan, 'service': 'Zoom', 'plan': plan, 'price': price}
                      for plan, price in PLANS]

        class BadModel:
            async def choose_tariff(self, shown, text):
                self.shown = [row['plan'] for row in shown]
                if 'месяц' in text:
                    return {'decision': 'chosen', 'tariff_id': 'zoom|Business на год', 'tariff_ids': [], 'reason': 'ошибся'}
                return {'decision': 'chosen', 'tariff_id': 'missing', 'tariff_ids': [], 'reason': 'нет в списке'}

        model = BadModel()
        month = asyncio.run(resolve_selection(model, candidates, 'тариф про на месяц'))
        self.assertNotIn('на год', model.shown)
        self.assertEqual(month['row']['plan'], 'Pro')
        missing = asyncio.run(resolve_selection(model, candidates, 'тариф про'))
        self.assertEqual(missing['row']['plan'], 'Pro')


class Shop:
    def __init__(self, rows):
        self.rows = rows

    async def facts(self, uid):
        return {'catalog': self.rows, 'catalog_status': 'available', 'appstore': {'status': 'available', 'regions': []},
                'bot_navigation': {'status': 'available', 'username': 'neirosetkaa_bot', 'catalog_webapp': True}}


class QuietModel:
    async def draft(self, history, facts, image=None):
        return {'reply': 'Могу подсказать по каталогу.', 'summary': 'Общий ответ.',
                'handoff': False, 'source_ids': [], 'research': {'topic': 'none', 'products': []}}

    async def check_answer(self, answer, facts, images=None):
        return {'supported': True, 'reason': 'ok'}


class DialogTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(suffix='.sqlite')
        self.store = Store(self.file.name, batch_seconds=0)
        self.store.put('mode', 'draft')
        self.store.db.execute("INSERT INTO connections VALUES ('biz',1,1)")
        self.service = Service(self.store, NoTelegram(), QuietModel(), Shop(catalog()),
                               owner=1, allowed={1, 50}, daily_limit=100)
        self.mid = 0

    def tearDown(self):
        self.file.close()

    def say(self, text):
        self.mid += 1
        now = time.time()
        cid = self.store.ingest('biz', 50, self.mid, 'client', text, {}, now)
        self.store.prepare_now(cid, now)
        job = self.store.claim(now + 1)
        self.assertIsNotNone(job, text)
        answer = asyncio.run(self.service.generate(job))
        self.assertTrue(answer and answer.get('reply'), text)
        self.assertNotIn('Уже известно', answer['reply'])
        self.assertNotIn('недостаточно без', answer['reply'])
        self.mid += 1
        self.store.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                              (cid, self.mid, 'assistant', answer['reply'], '{}', time.time(), 'done'))
        self.store.db.execute("UPDATE messages SET state='done' WHERE chat=? AND mid=? AND role='client'",
                              (cid, job['mid']))
        return answer['reply']

    def test_screenshot_zoom_then_telegram(self):
        listed = self.say('Сколько стоит Zoom?')
        for plan, price in PLANS:
            self.assertIn(str(price), listed)
        chosen = self.say('Тариф про')
        self.assertIn('1890', chosen)
        self.assertNotIn('16990', chosen)
        self.assertNotIn('21990', chosen)
        self.assertNotIn('Подходят несколько', chosen)
        self.assertIn('neirosetkaa_bot', chosen)
        again = self.say('Про')
        self.assertNotIn('Уже известно', again)
        self.assertNotIn('недостаточно без', again)
        switched = self.say('У вас можно купить телеграм подписку?')
        self.assertNotIn('Zoom', switched)
        self.assertNotIn('1890', switched)
        self.assertTrue('Telegram' in switched or 'телеграм' in switched.casefold(), switched)


class SendPathTests(unittest.TestCase):
    def test_real_presentation_keeps_a_workflow_consultation_sendable(self):
        body = ('Откройте @neirosetkaa_bot, нажмите /start, затем «Каталог».\n\n'
                'Zoom: выберите тариф и оплатите заказ.\n'
                'После оплаты пришлите сюда почту и пароль от аккаунта.\n'
                '1. Откройте тариф.\n'
                '2. Оплатите заказ.')
        answer = {'reply': body, 'summary': 'Как оформить Zoom.', 'handoff': False,
                  'evidence': consultation_evidence(body, 'workflow', owner_action_required=False,
                                                    diagnostic='Этап: консультация об оформлении.')}
        presented, replaced = present_outgoing(answer, introduced=False)
        self.assertFalse(replaced)
        self.assertIn('Здравствуйте', presented['reply'])
        reason = block_reason(presented, presented['evidence'], 'Как оформить подписку на Zoom?')
        self.assertIsNone(reason, reason)

        store = Store(':memory:', batch_seconds=0)
        store.put('mode', 'auto')
        store.connection('biz', True, True)
        now = time.time()
        cid = store.ingest('biz', 50, 1, 'client', 'Как оформить подписку на Zoom?', {}, now)
        store.db.execute("UPDATE chats SET mode='auto' WHERE id=?", (cid,))
        store.db.execute("UPDATE messages SET state='working' WHERE chat=?", (cid,))
        chat = store.chat(cid)
        job = {'chat': cid, 'mid': 1, 'revision': chat['revision'], 'global_revision': int(store.get('revision'))}
        did = store.finish(job, presented['reply'], presented['summary'], False, presented['evidence'])
        self.assertIsNotNone(did)
        service = Service(store, NoTelegram(), QuietModel(), Shop(catalog()),
                          owner=1, allowed={1, 50}, daily_limit=100)
        draft = store.db.execute('SELECT * FROM drafts WHERE id=?', (did,)).fetchone()
        self.assertIsNone(service.auto_content_reason(draft))


if __name__ == '__main__':
    unittest.main()
