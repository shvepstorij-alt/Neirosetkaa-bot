"""The current client burst chooses the reply. Older facts stay in the background."""
import asyncio
import tempfile
import time
import unittest

from support_agent.auto_notice import kind_for, TEXTS
from support_agent.auto_policy import _local_consultation, block_reason, consultation_evidence
from support_agent.conversation_style import INTRO
from support_agent.dialog_probe import NoTelegram
from support_agent.service import Service
from support_agent.situation import format_offer
from support_agent.storage import Store


def facts():
    catalog = [
        {'service': 'Lovable Pro', 'service_key': 'lovable', 'plan': 'Pro',
         'catalog_price_rub': 2490, 'fulfillment': 'checkout_link'},
        {'service': 'App Store', 'service_key': 'appstore', 'plan': 'код',
         'catalog_price_rub': 1, 'fulfillment': 'manual'},
    ]
    regions = {'status': 'available', 'regions': [{'name': 'Россия | RU', 'denoms': [
        {'nominal': '1000 RUB', 'price_rub': 1450},
        {'nominal': '3000 RUB', 'price_rub': 4250},
        {'nominal': '4000 RUB', 'price_rub': 5600},
        {'nominal': '5000 RUB', 'price_rub': 7500},
        {'nominal': '6000 RUB', 'price_rub': 8900},
        {'nominal': '2000 RUB', 'price_rub': 2900},
    ]}]}
    return {'catalog': catalog, 'catalog_status': 'available', 'appstore': regions,
            'bot_navigation': {'status': 'available', 'username': 'neirosetkaa_bot', 'catalog_webapp': True}}


class Shop:
    def __init__(self, payload):
        self.payload = payload

    async def facts(self, uid):
        return dict(self.payload)


class IntentModel:
    """Answers the classified current question, not the previous price."""

    def __init__(self):
        self.drafts = []

    async def draft(self, history, facts, image=None):
        situation = (facts or {}).get('situation') or {}
        intent = situation.get('intent')
        burst = situation.get('burst') or ''
        self.drafts.append(burst)
        if intent == 'capabilities':
            reply = 'Могу подсказать цены, номиналы и как оформить подписку из каталога.'
        elif intent == 'how_to':
            reply = 'Откройте бота, каталог, выберите Lovable и тариф, затем способ оплаты.'
        else:
            reply = 'Уточните, пожалуйста, что нужно сделать в этом сообщении.'
        return {'reply': reply, 'summary': 'Ответ на текущее сообщение.',
                'handoff': False, 'source_ids': [], 'research': {'topic': 'none', 'products': []}}

    async def check_answer(self, answer, facts, images=None):
        return {'supported': True, 'reason': 'ok'}


class ScreenshotTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(suffix='.sqlite')
        self.store = Store(self.file.name, batch_seconds=0)
        self.store.put('mode', 'draft')
        self.store.db.execute("INSERT INTO connections VALUES ('biz',1,1)")
        self.model = IntentModel()
        self.service = Service(self.store, NoTelegram(), self.model, Shop(facts()),
                               owner=1, allowed={1, 50}, daily_limit=100)
        self.mid = 0
        self.cid = None

    def tearDown(self):
        self.file.close()

    def say(self, text):
        self.mid += 1
        now = time.time()
        self.cid = self.store.ingest('biz', 50, self.mid, 'client', text, {}, now)
        self.store.prepare_now(self.cid, now)
        job = self.store.claim(now + 1)
        self.assertIsNotNone(job, text)
        answer = asyncio.run(self.service.generate(job))
        self.assertTrue(answer and answer.get('reply'), text)
        self.mid += 1
        self.store.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                              (self.cid, self.mid, 'assistant', answer['reply'], '{}', time.time(), 'done'))
        self.store.db.execute("UPDATE messages SET state='done' WHERE chat=? AND mid=? AND role='client'",
                              (self.cid, job['mid']))
        return answer['reply']

    def test_screenshot_sequence_answers_each_message(self):
        self.say('У меня Россия\nХочу на 6000')
        listed = self.say('Назовите все номиналы')
        self.assertNotIn('Вы хотите пополнить', listed)
        self.assertNotIn('6000 RUB. Точную', listed)
        for nominal in ('1000 RUB', '2000 RUB', '3000 RUB', '4000 RUB', '5000 RUB', '6000 RUB'):
            self.assertIn(nominal, listed)
        howto = self.say('Как оформить подписку на Ловабл')
        self.assertNotIn(TEXTS['manual'], howto)
        self.assertNotIn('Вы хотите пополнить', howto)
        self.assertNotIn('Pro Pro', howto)
        self.assertTrue('Lovable' in howto or 'оформ' in howto.casefold(), howto)
        capable = self.say('Что ты можешь')
        self.assertNotIn('2490', capable)
        self.assertNotIn('Pro Pro', capable)
        self.assertIn('Могу подсказать', capable)

    def test_tariff_name_is_not_repeated(self):
        self.assertEqual(format_offer('Lovable Pro', 'Pro'), 'Lovable Pro')
        self.assertEqual(format_offer('Suno', 'Pro'), 'Suno Pro')
        self.assertEqual(format_offer('Suno Pro', 'Pro на 1 год'), 'Suno Pro на 1 год')


class BurstTests(unittest.TestCase):
    def test_message_during_draft_is_answered_with_the_burst(self):
        store = Store(':memory:', batch_seconds=0)
        store.connection('biz', True, True)
        store.switch('draft')
        now = time.time()
        cid = store.ingest('biz', 7, 1, 'client', 'Назовите все номиналы', {}, now)
        store.prepare_now(cid, now)
        job = store.claim(now + 1)
        self.assertEqual(job['mid'], 1)
        store.ingest('biz', 7, 2, 'client', 'Что ты можешь', {}, now + 1)
        self.assertFalse(store.still_current(job))
        self.assertEqual(store.chat(cid)['batch_first'], 1)
        nxt = store.claim(now + 2)
        self.assertEqual(nxt['mid'], 2)
        texts = [row['text'] for row in store.db.execute(
            "SELECT text FROM messages WHERE chat=? AND role='client' AND mid>=? AND mid<=? ORDER BY mid",
            (cid, store.chat(cid)['batch_first'], nxt['mid']))]
        self.assertEqual(texts, ['Назовите все номиналы', 'Что ты можешь'])


class SendGateTests(unittest.TestCase):
    def test_greeting_does_not_invalidate_a_consultation(self):
        body = 'Откройте бота, каталог, выберите Lovable и тариф, затем способ оплаты.'
        evidence = consultation_evidence(body, 'workflow', owner_action_required=False,
                                        diagnostic='Этап: консультация об оформлении.')
        delivered = INTRO + '\n\n' + body
        self.assertTrue(_local_consultation(delivered, evidence))
        answer = {'reply': delivered, 'summary': 'Как оформить.', 'handoff': False, 'evidence': evidence}
        self.assertIsNone(block_reason(answer, evidence, 'Как оформить подписку на Ловабл'))

    def test_howto_is_not_an_operation_failure_notice(self):
        evidence = {'status': 'workflow_consultation', 'owner_action_required': True, 'sources': []}
        kind = kind_for(evidence, 'кодовая консультация не имеет действующего подтверждения неизменённого текста',
                        'Как оформить подписку на Ловабл', usable_reply=True)
        self.assertNotEqual(kind, 'manual')
        self.assertNotEqual(TEXTS.get(kind), TEXTS['manual'])


if __name__ == '__main__':
    unittest.main()
