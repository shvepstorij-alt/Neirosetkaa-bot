"""Dialog state carries a short reply for any service, not a fixed phrase list."""
import sqlite3
import tempfile
import unittest

from support_agent.auto_notice import kind_for
from support_agent.auto_policy import block_reason
from support_agent.autonomous_fallback import reply as fallback_reply
from support_agent.dialog_state import bind_turn, ground_claims, prepare_outgoing, record_outgoing
from support_agent.turn_contract import project_turn


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def catalog(service, key, rows):
    items = [{'service': service, 'service_key': key, 'plan': plan,
              'catalog_price_rub': price, 'fulfillment': 'manual'} for plan, price in rows]
    return {'catalog': items, 'requested_catalog': items}


def apple_facts(region_name, denoms):
    return {'catalog': [{'service': 'App Store', 'service_key': 'appstore', 'plan': 'код',
                         'catalog_price_rub': 1, 'fulfillment': 'manual'}],
            'appstore': {'status': 'available', 'regions': [{'name': region_name, 'denoms': denoms}]}}


class DialogFollowupTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile()
        self.db = connect(self.file.name)

    def tearDown(self):
        self.db.close()
        self.file.close()

    def test_short_option_uses_offered_plans_for_several_services(self):
        suno = catalog('Suno', 'suno', [('Premier', 2990), ('Pro', 990), ('Pro на 1 год', 9590), ('Premier на 1 год', 28790)])
        record_outgoing(self.db, 1, 'Suno Premier: 2990 ₽\nSuno Pro: 990 ₽\nSuno Pro на 1 год: 9590 ₽\n\nКакой тариф вас интересует?', suno)
        ambiguous = bind_turn(self.db, 1, 'Про', suno)['grounded']['reply']
        self.assertIn('990 ₽', ambiguous)
        self.assertIn('9590 ₽', ambiguous)
        self.assertNotIn('Какой сервис', ambiguous)
        self.assertNotIn('Что хотите узнать', ambiguous)

        cursor = catalog('Cursor', 'cursor', [('Pro', 2100), ('Business', 6400)])
        record_outgoing(self.db, 2, 'Cursor Pro: 2100 ₽\nCursor Business: 6400 ₽\n\nКакой тариф выбрать?', cursor)
        chosen = bind_turn(self.db, 2, 'pro', cursor)['grounded']['reply']
        self.assertIn('2100 ₽', chosen)
        self.assertNotIn('6400', chosen)

        spotify = catalog('Spotify', 'spotify', [('Individual', 390), ('Family', 590), ('Individual на 1 год', 3900)])
        record_outgoing(self.db, 3, 'Spotify Individual: 390 ₽\nSpotify Family: 590 ₽\nSpotify Individual на 1 год: 3900 ₽\n\nКакой вариант нужен?', spotify)
        yearly = bind_turn(self.db, 3, 'на год', spotify)['grounded']['reply']
        self.assertIn('3900 ₽', yearly)
        self.assertNotIn('590 ₽', yearly)
        family = bind_turn(self.db, 3, 'family', spotify)['grounded']['reply']
        self.assertIn('590 ₽', family)

    def test_restating_the_service_keeps_the_tariff_question(self):
        suno = catalog('Suno', 'suno', [('Pro', 990), ('Premier', 2990)])
        record_outgoing(self.db, 1, 'Suno Pro: 990 ₽\nSuno Premier: 2990 ₽\n\nКакой тариф вас интересует?', suno)
        again = bind_turn(self.db, 1, 'Суно', suno)
        self.assertIn('990 ₽', again['grounded']['reply'])
        self.assertNotIn('Что хотите узнать', again['grounded']['reply'])
        self.assertNotIn('Какой сервис', again['grounded']['reply'])

    def test_unmatched_colloquial_option_stays_on_the_topic_without_a_price(self):
        mid = catalog('Midjourney', 'midjourney', [('Basic', 1200), ('Standard', 2400)])
        record_outgoing(self.db, 1, 'Midjourney Basic: 1200 ₽\nMidjourney Standard: 2400 ₽\n\nКакой тариф вас интересует?', mid)
        bound = bind_turn(self.db, 1, 'стандарт', mid)
        self.assertTrue(bound['facts']['dialog_state']['bound'])
        self.assertIsNone(bound['grounded'])
        self.assertIn('Midjourney', bound['continue_text'])
        reference = {'goals': [], 'reason': 'no_client_question', 'entities': {'labels': ['Midjourney']}}
        turn = project_turn(bound['facts'], bound['continue_text'], 'стандарт', reference)
        self.assertFalse(turn['clarification_required'])

    def test_amount_and_region_follow_the_open_app_store_topic(self):
        russia = apple_facts('Россия | RU', [
            {'nominal': '1000 RUB', 'price_rub': 1450},
            {'nominal': '3000 RUB', 'price_rub': 4350},
            {'nominal': '500 RUB', 'price_rub': 790},
        ])
        record_outgoing(self.db, 1, 'Для Apple ID России есть пополнение на 1000 RUB — 1450 ₽.', russia)
        next_amount = bind_turn(self.db, 1, 'А 3000?', russia)['grounded']['reply']
        self.assertIn('4350', next_amount)
        self.assertIn('3000', next_amount)
        self.assertNotIn('Какой сервис', next_amount)

        bare = bind_turn(self.db, 1, '500?', russia)['grounded']['reply']
        self.assertIn('790', bare)

        germany = apple_facts('Германия | DE', [{'nominal': '25 EUR', 'price_rub': 2800}, {'nominal': '50 EUR', 'price_rub': 5400}])
        record_outgoing(self.db, 2, 'App Store: сумма 25 EUR. Какой регион у вашего Apple ID?', germany)
        quoted = bind_turn(self.db, 2, 'германия', germany)['grounded']['reply']
        self.assertIn('2800', quoted)
        changed = bind_turn(self.db, 2, 'а 50?', germany)
        # The region was not saved until a reply is recorded; this turn only has the asked amount.
        # Record the Germany quote, then a different amount must keep Germany.
        record_outgoing(self.db, 2, quoted, germany)
        changed = bind_turn(self.db, 2, 'а 50?', germany)['grounded']['reply']
        self.assertIn('5400', changed)
        self.assertNotIn('На какую сумму', changed)

    def test_missing_nominal_is_not_a_new_service_question(self):
        russia = apple_facts('Россия | RU', [
            {'nominal': '1000 RUB', 'price_rub': 1450},
            {'nominal': '2500 RUB', 'price_rub': 3600},
        ])
        record_outgoing(self.db, 1, 'Для Apple ID России есть пополнение на 1000 RUB — 1450 ₽.', russia)
        missing = bind_turn(self.db, 1, 'а 3000?', russia)['grounded']['reply']
        self.assertIn('3000', missing)
        self.assertNotIn('Какой сервис', missing)
        self.assertNotIn('4350', missing)
        self.assertTrue('3600' in missing or 'не получилось' in missing or 'подтвердить' in missing)

    def test_answer_to_the_asked_goal_uses_catalog_prices(self):
        suno = catalog('Suno', 'suno', [('Pro', 990), ('Premier', 2990)])
        record_outgoing(self.db, 1, 'Что хотите узнать о Suno: стоимость, порядок оформления или возможности?', suno)
        prices = bind_turn(self.db, 1, 'стоимость', suno)['grounded']['reply']
        self.assertIn('990 ₽', prices)
        self.assertIn('2990 ₽', prices)
        features = bind_turn(self.db, 1, 'возможности', suno)
        self.assertIsNone(features['grounded'])
        self.assertTrue(features['facts']['dialog_state']['bound'])
        self.assertEqual(features['facts']['dialog_state']['topic'], 'features')

    def test_another_named_service_does_not_keep_the_old_topic(self):
        suno = catalog('Suno', 'suno', [('Pro', 990)])
        record_outgoing(self.db, 1, 'Suno Pro: 990 ₽\n\nКакой тариф вас интересует?', suno)
        switched = bind_turn(self.db, 1, 'сколько стоит курсор', suno)
        self.assertFalse(switched['facts']['dialog_state']['bound'])
        self.assertIsNone(switched['grounded'])

    def test_state_is_stored_per_chat(self):
        suno = catalog('Suno', 'suno', [('Pro', 990)])
        record_outgoing(self.db, 7, 'Suno Pro: 990 ₽\n\nКакой тариф вас интересует?', suno)
        self.db.close()
        again = connect(self.file.name)
        try:
            bound = bind_turn(again, 7, 'про', suno)
            self.assertIn('990 ₽', bound['grounded']['reply'])
            empty = bind_turn(again, 8, 'про', suno)
            self.assertIsNone(empty['grounded'])
        finally:
            again.close()
            self.db = connect(self.file.name)


class IsolatedResetTests(unittest.TestCase):
    def test_keyword_clarification_does_not_override_a_bound_topic(self):
        reference = {'goals': [], 'reason': 'unavailable_client_context', 'entities': {'labels': ['Suno']},
                     'context_condition': 'gap'}
        loose = project_turn({'catalog': []}, 'суно', 'суно', reference)
        self.assertTrue(loose['clarification_required'])
        held = project_turn({'catalog': [], 'dialog_state': {'bound': True, 'service_label': 'Suno'}}, 'Suno\nсуно', 'суно', reference)
        self.assertFalse(held['clarification_required'])

    def test_fallback_does_not_drop_a_bound_service(self):
        facts = catalog('Suno', 'suno', [('Pro', 990), ('Premier', 2990)])
        facts['dialog_state'] = {'bound': True, 'service_label': 'Suno', 'service_key': 'suno', 'offered': []}
        facts['conversation'] = {'latest_message': 'про', 'latest_client_message': 'про'}
        facts['conversation_turn'] = {'current_text': 'про', 'context_text': 'про'}
        answer = fallback_reply(facts, 'черновик не принят', 'про')
        self.assertIsNotNone(answer)
        self.assertNotIn('Какой сервис вас интересует?', answer['reply'])
        self.assertIn('Suno', answer['reply'])


class DeliveryTests(unittest.TestCase):
    def _collection(self):
        return ('По условиям вам может быть доступна подписка на месяц, ChatGPT или Claude. '
                'Пришлите почту регистрации, я проверю и напишу, что делать дальше. Пароль не нужен.')

    def test_collection_is_sent_for_varied_requests_and_versions_are_removed(self):
        raw = ('В подписке будут GPT-5.6 Sol, Opus 5.5 и Sonnet 5.5. ' + self._collection())
        cleaned = ground_claims(raw, {'requested_catalog': [{'service': 'ChatGPT', 'plan': 'Plus'}]})
        self.assertNotIn('GPT-5.6', cleaned)
        self.assertNotIn('Opus 5.5', cleaned)
        self.assertNotIn('Sonnet 5.5', cleaned)
        self.assertIn('Пришлите почту', cleaned)
        answer = {'reply': cleaned, 'summary': 'Нужна почта, чтобы владелец проверил условие.',
                  'handoff': True, 'evidence': {'status': 'checked', 'sources': [], 'diagnostic': ''}}
        for client in (
            'Нужно оформить бонусную подписку на месяц',
            'Я купил обучение, сказали ChatGPT или Claude',
            'после курса обещали месяц подписки',
            'куратор написал, что будет бонус',
        ):
            self.assertIsNone(block_reason(answer, answer['evidence'], client), client)

    def test_empty_owner_reason_uses_the_summary_and_a_grant_stays_manual(self):
        held = {'reply': 'Я уже оформил бонус и активировал подписку.',
                'summary': 'Черновик обещает выдачу, это проверяет владелец.',
                'handoff': True, 'evidence': {'status': 'checked', 'sources': []}}
        reason = block_reason(held, held['evidence'], 'хочу бонус')
        self.assertIn('Черновик обещает выдачу', reason)
        self.assertNotIn('отсутствует', reason)
        granted = {'reply': 'Бонус вам одобрен.',
                   'summary': 'Преждевременное подтверждение.', 'handoff': False,
                   'evidence': {'status': 'catalog_direct', 'sources': []}}
        self.assertIn('выдачу бонуса', block_reason(granted, granted['evidence'], 'спасибо'))

    def test_held_draft_is_not_replaced_by_the_gap_text(self):
        evidence = {'status': 'checked', 'diagnostic': 'условия не подтверждены'}
        self.assertEqual(kind_for(evidence, 'не подтверждено', 'бонус', usable_reply=True), 'human')
        self.assertEqual(kind_for(evidence, 'не подтверждено', 'бонус', usable_reply=False), 'information')
        replaced, dropped = prepare_outgoing('Там есть GPT-9.1 и Opus 8.2.', {})
        self.assertTrue(dropped)
        self.assertIn('Александр будет на связи', replaced)


if __name__ == '__main__':
    unittest.main()
