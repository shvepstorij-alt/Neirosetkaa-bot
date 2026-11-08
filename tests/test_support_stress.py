"""The offline stress dialogs and an owner answer that replaces a hold."""
import unittest

from support_agent.faq import load_faq, usable_answer
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

    def test_empty_faq_is_not_an_answer(self):
        topics = load_faq()['topics']
        self.assertGreaterEqual(len(topics), 6)
        self.assertTrue(all(usable_answer(row['id']) == '' for row in topics))

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
