"""Owner rules approved on 11.11.2026: payment, renewal, transfer, receipt."""
import unittest

from support_agent.faq import for_context, usable_answer
from support_agent.owner_scenarios import reply as scenario
from support_agent.turn_understanding import DOCUMENTED_FACTS, enforce_turn_policy, package

TOPICS = ('card_payment', 'bot_stuck', 'renewal', 'cancel_notice', 'transfer',
          'foreign_payment', 'for_other_person', 'receipt', 'off_catalog')


def _turn(topic, reply, question, beyond=False):
    context = {'tariffs': [], 'nominals': [], 'history': [], 'faq': for_context()}
    understanding = {'intent': 'other', 'service_id': '', 'tariff_id': '', 'confidence': 'high',
                     'reply': reply, 'summary': 'Ответ владельца.', 'questions': [question],
                     'owner_topic': topic, 'beyond_owner_fact': beyond,
                     'conflict': 'none', 'owner_alignment': 'follows'}
    return enforce_turn_policy(package(understanding, context), understanding, context, {'status': 'fresh'})


class OwnerRulesTests(unittest.TestCase):
    def test_every_approved_topic_has_the_owner_text(self):
        for topic in TOPICS:
            self.assertTrue(usable_answer(topic), topic)

    def test_owner_text_is_sent_without_a_hold(self):
        questions = {'card_payment': 'А картой можно оплатить?', 'bot_stuck': 'Бот завис, что делать?',
                     'renewal': 'Как продлить подписку?', 'cancel_notice': 'Пришло письмо, что подписка отменена',
                     'transfer': 'Можно перенести подписку на другой аккаунт?',
                     'foreign_payment': 'PayPal принимаете?', 'for_other_person': 'Можно купить для жены?',
                     'receipt': 'Чек нужно присылать?', 'off_catalog': 'А сервиса нет в каталоге, можно оплатить?'}
        for topic, question in questions.items():
            answer = _turn(topic, usable_answer(topic), question)
            self.assertFalse(answer['handoff'], (topic, answer.get('evidence', {}).get('diagnostic')))

    def test_invented_condition_is_not_sent(self):
        # Owner 12.11.2026: the invented condition is dropped, the owner's rule goes out
        # with «остальное уточнит Александр» instead of stopping the reply.
        answer = _turn('renewal', 'Продление спишется автоматически через 30 дней, вернём деньги за 14 дней.',
                       'Как продлить подписку?', beyond=True)
        self.assertFalse(answer['handoff'])
        self.assertTrue(answer['reply'].startswith(usable_answer('renewal').rstrip()))
        self.assertIn('уточнит Александр', answer['reply'])
        self.assertNotIn('14 дней', answer['reply'])
        self.assertTrue(answer['evidence'].get('owner_followup'))

    def test_drifted_draft_is_replaced_by_the_owner_text(self):
        answer = _turn('renewal', 'Продление спишется автоматически через 30 дней.', 'Как продлить подписку?')
        self.assertFalse(answer['handoff'])
        self.assertEqual(answer['reply'], usable_answer('renewal'))
        self.assertTrue(answer['evidence'].get('owner_text_used'))

    def test_key_wording(self):
        self.assertIn('/start', usable_answer('card_payment'))
        self.assertIn('СБП', usable_answer('card_payment'))
        self.assertIn('/start', usable_answer('bot_stuck'))
        self.assertIn('автосписаний нет', usable_answer('renewal'))
        self.assertIn('бесплатный тариф', usable_answer('renewal'))
        self.assertIn('нельзя перенести', usable_answer('transfer'))
        self.assertIn('не обязателен', usable_answer('receipt'))
        self.assertIn('FreeKassa', usable_answer('receipt'))
        self.assertIn('деньги не возвращаются', usable_answer('account_ban'))
        self.assertNotIn('Grok', ' '.join(DOCUMENTED_FACTS))
        self.assertTrue(any('ChatGPT, Claude и Perplexity' in fact for fact in DOCUMENTED_FACTS))

    def test_promo_only_on_a_direct_request(self):
        asked = scenario('Есть промокод?')
        self.assertIn('NAVSEX', asked['reply'])
        self.assertIsNone(scenario('Сколько стоит ChatGPT Plus?'))
        self.assertIsNone(scenario('Как оплатить Claude?'))


if __name__ == '__main__':
    unittest.main()
