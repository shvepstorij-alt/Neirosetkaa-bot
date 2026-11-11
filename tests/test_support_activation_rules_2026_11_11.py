"""Owner rules approved on 11.11.2026: activation and typical errors."""
import unittest

from support_agent.activation_guidance import _BROWSERS
from support_agent.faq import collect_fields, for_context, usable_answer
from support_agent.turn_understanding import DOCUMENTED_FACTS, enforce_turn_policy, package

PLAIN = ('activation_where', 'token_copy', 'token_expired', 'must_be_free', 'still_free_after_activation',
         'page_not_opening', 'wrong_account', 'claude_org_id', 'plan_upgrade', 'limits_exhausted',
         'google_login_notice', 'token_as_file')
COLLECTING = ('activation_expired', 'activation_failed', 'subscription_ended_early')


def _turn(topic, reply, order=''):
    context = {'tariffs': [], 'nominals': [], 'history': [], 'faq': for_context()}
    understanding = {'intent': 'payment_issue', 'service_id': '', 'tariff_id': '', 'confidence': 'high',
                     'reply': reply, 'summary': 'Ответ владельца.', 'questions': ['Вопрос клиента'],
                     'owner_topic': topic, 'beyond_owner_fact': False, 'refund_order': order,
                     'refund_service': '', 'refund_reason': '',
                     'conflict': 'none', 'owner_alignment': 'follows'}
    return enforce_turn_policy(package(understanding, context), understanding, context, {'status': 'fresh'})


class ActivationRulesTests(unittest.TestCase):
    def test_plain_topics_send_the_owner_text(self):
        for topic in PLAIN:
            text = usable_answer(topic)
            self.assertTrue(text, topic)
            answer = _turn(topic, text)
            self.assertFalse(answer['handoff'], (topic, answer.get('evidence', {}).get('diagnostic')))

    def test_collecting_topics_ask_first_and_then_call_the_owner(self):
        for topic in COLLECTING:
            self.assertEqual(collect_fields(topic), ['order_number'])
            asked = _turn(topic, usable_answer(topic))
            self.assertFalse(asked['handoff'], (topic, asked.get('evidence', {}).get('diagnostic')))
            ready = _turn(topic, 'Спасибо, номер заказа получил.', order='5498')
            self.assertTrue(ready['handoff'], topic)
            self.assertEqual(ready['evidence'].get('collect_topic'), topic)
            self.assertTrue(ready['evidence'].get('refund_hold'))

    def test_a_promised_result_is_held(self):
        answer = _turn('activation_expired', 'Повторная активация уже оформил, подписка активирована.')
        self.assertTrue(answer['handoff'])

    def test_key_wording(self):
        self.assertIn('2 часа', usable_answer('activation_where'))
        self.assertIn('со всеми скобками', usable_answer('token_copy'))
        self.assertIn('через настройки', usable_answer('token_expired'))
        self.assertIn('Оплата сохраняется', usable_answer('must_be_free'))
        self.assertIn('10–15 минут', usable_answer('still_free_after_activation'))
        self.assertIn('Яндекс', usable_answer('page_not_opening'))
        self.assertIn('Яндекс', _BROWSERS)
        self.assertIn('возврат не оформляется', usable_answer('wrong_account'))
        self.assertIn('возврат за активированную подписку не оформляется', usable_answer('transfer'))
        self.assertIn('claude.ai', usable_answer('claude_org_id'))
        self.assertIn('напишет Александр', usable_answer('google_login_notice'))
        self.assertTrue(any('действует 2 часа' in fact for fact in DOCUMENTED_FACTS))


if __name__ == '__main__':
    unittest.main()
