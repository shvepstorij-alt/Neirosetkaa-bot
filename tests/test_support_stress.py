"""The offline stress dialogs and an owner answer that replaces a hold."""
import unittest

from support_agent.faq import collect_fields, load_faq, usable_answer
from support_agent.stress_suite import run_suite
from support_agent.turn_understanding import enforce_turn_policy, package


class StressTests(unittest.TestCase):
    def test_offline_dialogs_cover_the_catalog_and_pass(self):
        report = run_suite()
        self.assertGreaterEqual(report['cases'], 80)
        self.assertLessEqual(report['cases'], 120)
        self.assertEqual(set(report['shop_keys']), set(report['named']) & set(report['shop_keys']))
        self.assertEqual(set(report['glossary_ids']), set(report['named']))
        self.assertGreater(report['before_failed_turns'], 0)
        self.assertEqual(report['failed'], [])
        self.assertEqual(report['passed'], report['checked'])

    def test_owner_facts_match_the_given_words_only(self):
        self.assertEqual(usable_answer('guarantee'), 'Гарантия есть на все сервисы, кроме Claude.')
        self.assertIn('заблокировать', usable_answer('account_ban'))
        self.assertIn('гарантии на Claude нет', usable_answer('account_ban'))
        self.assertEqual(usable_answer('delivery_time'),
                         'Активация занимает 5–30 минут. У сервисов с автоактивацией — 2–5 минут.')
        self.assertEqual(usable_answer('refund_terms'), '')
        self.assertEqual(collect_fields('refund_terms'), ['order_number', 'service', 'reason'])
        self.assertIn('регион у вашего аккаунта', usable_answer('off_catalog'))
        self.assertEqual(usable_answer('region_policy'), '')
        self.assertGreaterEqual(len(load_faq()['topics']), 6)

    def test_owner_answer_is_sent_when_the_file_has_text(self):
        context = {'tariffs': [], 'nominals': [], 'history': [],
                   'faq': [{'id': 'guarantee', 'title': 'Гарантии',
                            'answer': 'Отдельной гарантии от блокировки нет.', 'known': True}]}
        understanding = {'intent': 'other', 'service_id': '', 'tariff_id': '', 'confidence': 'high',
                         'reply': 'Отдельной гарантии от блокировки нет.', 'summary': 'Ответ владельца.',
                         'questions': ['Есть ли гарантия?'], 'owner_topic': 'guarantee',
                         'conflict': 'none', 'owner_alignment': 'follows'}
        answer = enforce_turn_policy(package(understanding, context), understanding, context, {'status': 'fresh'})
        self.assertFalse(answer['handoff'])
        self.assertIn('гарантии', answer['reply'].casefold())


if __name__ == '__main__':
    unittest.main()
