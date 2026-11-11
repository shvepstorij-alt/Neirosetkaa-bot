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

    def test_a_promised_result_is_replaced_by_the_owner_request(self):
        answer = _turn('activation_expired', 'Повторная активация уже оформил, подписка активирована.')
        self.assertFalse(answer['handoff'])
        self.assertEqual(answer['reply'], usable_answer('activation_expired'))

    def test_limits_question_gets_the_owner_text_not_a_hold(self):
        draft = ('Лимиты закончились - можно пополнить кредиты. 250 кредитов. '
                 'Лимиты обновятся в течение 5-30 минут.')
        answer = _turn('limits_exhausted', draft)
        self.assertFalse(answer['handoff'])
        self.assertEqual(answer['reply'], usable_answer('limits_exhausted'))

    def test_conditional_payment_wording_is_not_an_outcome_claim(self):
        from support_agent.reply_assertion import asserts_outcome
        self.assertFalse(asserts_outcome('Если оплата прошла, дождитесь автоактивации.'))
        self.assertTrue(asserts_outcome('Оплата прошла, подписка активирована.'))

    def test_invalid_token_gets_the_relogin_steps(self):
        from support_agent.activation_guidance import reply
        answer = reply({'catalog_selection': {'mentioned_known_services': ['chatgpt']}},
                       'А при активации чата Гпт пишет что токен недействителен')
        self.assertIsNotNone(answer)
        self.assertIn('через настройки', answer['reply'])
        self.assertFalse(answer['handoff'])

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


class ResendButtonTests(unittest.TestCase):
    def _alert(self, topic, order):
        import asyncio
        from support_agent.service import Service
        sent = []

        class Stub(Service):
            def __init__(self):
                self._last_dialog_facts = {}

            def chat_identity(self, cid):
                return 'Клиент 50'

            async def owner_message(self, text, reply_markup=None, parse_mode=None):
                sent.append((text, reply_markup))

        answer = {'evidence': {'refund_hold': True, 'collect_topic': topic,
                               'refund_collected': {'order_number': order}}}
        asyncio.run(Stub().maybe_refund_alert({'chat': 7, 'uid': 50}, answer))
        return sent

    def test_expired_activation_alert_carries_the_shop_link(self):
        text, markup = self._alert('activation_expired', '№ 5498')[0]
        self.assertIn('Время активации истекло', text)
        url = markup['inline_keyboard'][0][0]['url']
        self.assertEqual(url, 'https://t.me/neirosetkaa_bot?start=resend_50_5498')

    def test_other_topics_have_no_button(self):
        text, markup = self._alert('subscription_ended_early', '5498')[0]
        self.assertIsNone(markup)
        text, markup = self._alert('refund_terms', '5498')[0]
        self.assertIsNone(markup)
        self.assertIn('Возврат', text)


if __name__ == '__main__':
    unittest.main()
