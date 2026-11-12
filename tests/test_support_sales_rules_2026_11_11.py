"""Owner test of 11.11.2026 (night) and the «Продажи» rules."""
import unittest

from support_agent.auto_policy import block_reason
from support_agent.faq import for_context, usable_answer
from support_agent.owner_scenarios import quick_reply
from support_agent.payment_guide import render
from support_agent.turn_understanding import enforce_turn_policy, package

NAV = {'status': 'available', 'username': 'neirosetkaa_bot', 'catalog_webapp': True}
GPT = [{'id': 'chatgpt|Plus 1 месяц', 'service_id': 'chatgpt', 'service': 'ChatGPT', 'plan': 'Plus 1 месяц', 'price': 1990},
       {'id': 'chatgpt|Новый аккаунт с подпиской Plus', 'service_id': 'chatgpt', 'service': 'ChatGPT',
        'plan': 'Новый аккаунт с подпиской Plus', 'price': 2490},
       {'id': 'gpt_credits|250 кредитов (10$)', 'service_id': 'gpt_credits', 'service': 'Пополнение кредитов ChatGPT',
        'plan': '250 кредитов (10$)', 'price': 1090},
       {'id': 'suno|Pro', 'service_id': 'suno', 'service': 'Suno', 'plan': 'Pro', 'price': 990}]


def _turn(burst, understanding, history=()):
    context = {'tariffs': GPT, 'nominals': [], 'history': list(history) + [{'role': 'client', 'text': burst}],
               'faq': for_context(), 'burst': burst, 'bot_navigation': NAV, 'dialog_state': {}}
    base = {'intent': 'question', 'service_id': '', 'tariff_id': '', 'confidence': 'high', 'reply': 'Ответ.',
            'summary': 'Ответ.', 'questions': ['Вопрос'], 'owner_topic': 'none', 'beyond_owner_fact': False,
            'conflict': 'none', 'owner_alignment': 'follows'}
    base.update(understanding)
    return enforce_turn_policy(package(base, context), base, context, {'status': 'fresh'})


class PaymentCardTests(unittest.TestCase):
    def test_no_coins_in_the_card(self):
        self.assertNotIn('монет', render('chatgpt', 'ChatGPT', 'Plus', {'bot_navigation': NAV}, [], GPT, '', None))

    def test_suno_card_is_sent_automatically(self):
        answer = _turn('Как оплатить суно?', {'service_id': 'suno', 'payment_asked': True})
        self.assertFalse(answer['handoff'], answer['evidence'].get('diagnostic'))
        self.assertIn('почту и пароль от аккаунта Suno', answer['reply'])
        self.assertIsNone(block_reason(answer, answer['evidence'], 'Как оплатить суно?'))

    def test_a_model_draft_asking_for_a_password_is_still_held(self):
        answer = {'reply': 'Пришлите пароль от аккаунта.', 'handoff': False,
                  'evidence': {'status': 'catalog_direct', 'reply_class': 'instruct', 'sources': []}}
        self.assertIsNotNone(block_reason(answer, answer['evidence'], 'как оплатить'))

    def test_credentials_and_email_only_services(self):
        zoom = render('zoom', 'Zoom', 'Pro', {'bot_navigation': NAV}, [], [], '', None)
        self.assertIn('почту и пароль от аккаунта Zoom', zoom)
        self.assertIn('только по коду с почты', zoom)
        canva = render('canva', 'Canva', 'Pro', {'bot_navigation': NAV}, [], [], '', None)
        self.assertIn('Пароль не нужен', canva)

    def test_appstore_card_explains_how_to_redeem_the_code(self):
        card = render('appstore', 'App Store', '', {'bot_navigation': NAV}, [], [], '', None)
        self.assertIn('Погасить подарочную карту или код', card)
        self.assertIn('\n1. Откройте App Store', card)


class CreditsTests(unittest.TestCase):
    def test_credits_follow_up_points_to_the_credits_product(self):
        history = [{'role': 'assistant', 'text': 'Для пополнения кредитов ChatGPT доступны: 250 кредитов (10$) - 1090 ₽'}]
        answer = _turn('На 10$', {'service_id': 'chatgpt', 'payment_asked': True}, history)
        self.assertIn('«Пополнение кредитов ChatGPT»', answer['reply'])
        self.assertNotIn('Выберите ChatGPT', answer['reply'])

    def test_plain_chatgpt_subscription_is_not_redirected(self):
        answer = _turn('Как оплатить ChatGPT Plus?', {'service_id': 'chatgpt', 'tariff_id': 'chatgpt|Plus 1 месяц',
                                                      'payment_asked': True})
        self.assertNotIn('кредит', answer['reply'])


class TariffLoopTests(unittest.TestCase):
    def test_answer_to_our_question_is_taken(self):
        history = [{'role': 'assistant', 'text': 'ChatGPT Plus 1 месяц или ChatGPT Новый аккаунт с подпиской Plus?\n\n'
                                                 'Это разные варианты, напишите какой из них'}]
        answer = _turn('Plus', {'service_id': 'chatgpt'}, history)
        self.assertNotIn('Это разные варианты', answer['reply'])
        self.assertIn('Plus 1 месяц', answer['reply'])


class OwnerRuleTests(unittest.TestCase):
    def test_quick_rules(self):
        self.assertIn('NAVSEX', quick_reply('А можно промокодик на оплату чата Гпт?)')['reply'])
        referral = quick_reply('А у вас есть рефералка?')['reply']
        self.assertIn('Мой профиль', referral)
        self.assertIn('10%', referral)
        self.assertIsNone(quick_reply('Как оплатить ChatGPT?'))

    def test_new_topics_send_owner_text(self):
        for topic in ('codex', 'claude_ready_account', 'appstore_redeem'):
            answer = _turn('вопрос', {'owner_topic': topic, 'reply': usable_answer(topic)})
            self.assertFalse(answer['handoff'], (topic, answer['evidence'].get('diagnostic')))
        self.assertIn('США', usable_answer('app_download'))
        self.assertIn('тариф выше', usable_answer('limits_exhausted'))


if __name__ == '__main__':
    unittest.main()


class ServiceGuideTests(unittest.TestCase):
    def test_guide_has_dated_official_facts_and_no_prices(self):
        import re
        from support_agent.service_guide import for_context as guide
        data = guide()
        self.assertEqual(data['checked'], '11.11.2026')
        self.assertIn('Ultra', data['services']['claude']['plans'])
        self.assertIn('Max 20×', data['services']['claude']['plans']['Ultra'])
        self.assertIn('Codex', data['choose']['chatgpt_vs_claude'])
        text = str(data)
        self.assertIsNone(re.search(r'\d+\s*(?:₽|руб|\$)', text))

    def test_guide_reaches_the_model_context(self):
        import sqlite3
        from support_agent import turn_understanding as tu
        self.assertIn('service_guide', tu.UNDERSTANDING_INSTRUCTIONS)
        self.assertTrue(tu._service_guide().get('services'))


class TopicSwitchTests(unittest.TestCase):
    """Owner test 11.11.2026 23:46: CapCut question answered about Suno with a ChatGPT plan name."""

    def _run(self, burst, understanding, state):
        context = {'tariffs': GPT, 'nominals': [], 'history': [{'role': 'client', 'text': burst}],
                   'faq': for_context(), 'burst': burst, 'bot_navigation': NAV, 'dialog_state': state}
        base = {'intent': 'how_to', 'service_id': '', 'tariff_id': '', 'confidence': 'high', 'reply': 'x',
                'summary': 'x', 'questions': ['q'], 'owner_topic': 'none', 'beyond_owner_fact': False,
                'conflict': 'none', 'owner_alignment': 'follows'}
        base.update(understanding)
        return enforce_turn_policy(package(base, context), base, context, {'status': 'fresh'})['reply']

    def test_remembered_plan_of_another_service_is_not_used(self):
        reply = self._run('А как оплатить суно?', {'service_id': 'suno', 'payment_asked': True},
                          {'service_id': 'chatgpt', 'tariff': 'Новый аккаунт с подпиской Plus'})
        self.assertIn('Выберите Suno', reply)
        self.assertNotIn('Новый аккаунт', reply)

    def test_new_service_named_with_a_space_is_not_replaced_by_the_old_one(self):
        for proposed in ('suno', ''):
            reply = self._run('А что нужно для кап ката?', {'service_id': proposed, 'payment_asked': True},
                              {'service_id': 'suno'})
            self.assertIn('CapCut', reply)
            self.assertNotIn('Suno', reply)

    def test_short_follow_up_keeps_the_open_service(self):
        reply = self._run('а как оплатить?', {'service_id': '', 'payment_asked': True}, {'service_id': 'suno'})
        self.assertIn('Suno', reply)

    def test_course_word_is_not_cursor(self):
        from support_agent.turn_understanding import _names_unknown_service
        self.assertEqual(_names_unknown_service({'burst': 'я с курсов, где бонус'}), set())


class TermAndClaudeRulesTests(unittest.TestCase):
    def test_owner_rules_12_11(self):
        self.assertIn('Spotify', usable_answer('subscription_term'))
        self.assertIn('один VPN', usable_answer('claude_block_risk'))
        self.assertIn('загранпаспорт', usable_answer('claude_verification'))
        self.assertIn('Российские номера', usable_answer('claude_verification'))
        for topic in ('subscription_term', 'claude_block_risk', 'claude_verification'):
            answer = _turn('вопрос', {'owner_topic': topic, 'reply': usable_answer(topic)})
            self.assertFalse(answer['handoff'], (topic, answer['evidence'].get('diagnostic')))

    def test_renamed_chatgpt_plus_keeps_the_activation_button(self):
        from support_agent.payment_guide import kind_for
        self.assertEqual(kind_for('chatgpt', 'Plus 1 месяц', []), 'chatgpt_auto')
        self.assertEqual(kind_for('chatgpt', 'Новый аккаунт с подпиской Plus', []), 'chatgpt_new_account')


class DiagnosticsRulesTests(unittest.TestCase):
    def test_owner_rules_12_11_errors(self):
        for topic in ('payment_error', 'login_problem', 'service_error', 'feature_missing', 'tech_setup',
                      'claude_review_pending'):
            answer = _turn('вопрос', {'owner_topic': topic, 'reply': usable_answer(topic)})
            self.assertFalse(answer['handoff'], (topic, answer['evidence'].get('diagnostic')))
        self.assertIn('выключите VPN', usable_answer('payment_error'))
        self.assertNotIn('Александр', usable_answer('payment_error'))
        self.assertIn('Спам', usable_answer('login_problem'))
        self.assertIn('CHATIK10', usable_answer('subscription_ended_early'))


class VerbatimTests(unittest.TestCase):
    def test_early_drop_answer_keeps_the_promo_codes(self):
        answer = _turn('У меня слетела подписка', {'owner_topic': 'subscription_ended_early', 'reply': 'Пришлите скрин.'})
        self.assertFalse(answer['handoff'])
        self.assertIn('NAVSEX', answer['reply'])
        self.assertIn('CHATIK10', answer['reply'])
