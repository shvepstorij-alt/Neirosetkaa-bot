"""Fixes after the owner's Telegram tests on 11.11.2026."""
import unittest

from support_agent.customer_format import format_customer
from support_agent.faq import for_context, usable_answer
from support_agent.payment_guide import render
from support_agent.service import present_outgoing
from support_agent.turn_understanding import (_repeats_assistant, asks_how_to_pay, enforce_turn_policy,
                                               nominal_index, package)

NAV = {'status': 'available', 'username': 'neirosetkaa_bot', 'catalog_webapp': True}
CLAUDE = [{'id': 'claude:0', 'service_id': 'claude', 'service': 'Claude', 'plan': 'Pro', 'price': 2090},
          {'id': 'claude:1', 'service_id': 'claude', 'service': 'Claude', 'plan': 'Max 20×', 'price': 19990}]


class FormattingTests(unittest.TestCase):
    def test_numbered_steps_stay_on_one_line(self):
        card = render('chatgpt', 'ChatGPT', '', {'bot_navigation': NAV}, [], [], '', None)
        shown, _ = present_outgoing({'reply': card, 'evidence': {}}, {}, True)
        plain, _html = format_customer(shown['reply'])
        self.assertIn('1. Откройте бота', plain)
        self.assertIn('\n2. Выберите ChatGPT.', plain)
        self.assertNotIn('1.\n', plain)


class PaymentQuestionTests(unittest.TestCase):
    def test_how_to_buy_wording(self):
        for text in ('Как его оформить?', 'Что нужно будет кроме оплаты', 'А какая инструкция оплаты',
                     'как купить клод про', 'где оплатить?'):
            self.assertTrue(asks_how_to_pay(text), text)
        for text in ('Оплатил, как активировать?', 'Как продлить подписку?', 'Закончились лимиты',
                     'После оплаты ничего не пришло, что нужно сделать после оплаты?'):
            self.assertFalse(asks_how_to_pay(text), text)

    def test_how_to_buy_gets_the_payment_card_not_the_activation_button_text(self):
        context = {'tariffs': CLAUDE, 'nominals': [], 'history': [], 'faq': for_context(),
                   'burst': 'Как его оформить?', 'bot_navigation': NAV,
                   'dialog_state': {'service_id': 'claude', 'tariff': 'Pro'}}
        understanding = {'intent': 'how_to', 'service_id': 'claude', 'tariff_id': 'claude:0', 'confidence': 'high',
                         'reply': usable_answer('activation_where'), 'summary': 'Как оформить.',
                         'questions': ['Как оформить Claude Pro?'], 'owner_topic': 'activation_where',
                         'beyond_owner_fact': False, 'conflict': 'none', 'owner_alignment': 'follows'}
        answer = enforce_turn_policy(package(understanding, context), understanding, context, {'status': 'fresh'})
        self.assertFalse(answer['handoff'], answer.get('evidence', {}).get('diagnostic'))
        self.assertIn('«Каталог»', answer['reply'])
        self.assertIn('1. Откройте бота', answer['reply'])
        self.assertIn('Organization ID', answer['reply'])
        self.assertNotEqual(answer['reply'], usable_answer('activation_where'))


class GuardTests(unittest.TestCase):
    def test_one_repeat_is_allowed_and_the_third_is_held(self):
        text = 'Продление оформляется как новая покупка в боте.'
        once = {'history': [{'role': 'assistant', 'text': text}]}
        twice = {'history': [{'role': 'assistant', 'text': text}, {'role': 'assistant', 'text': 'Здравствуйте!\n\n' + text}]}
        self.assertFalse(_repeats_assistant(text, once))
        self.assertTrue(_repeats_assistant(text, twice))

    def test_all_regions_reach_the_model(self):
        facts = {'appstore': {'regions': [{'name': 'Регион %d' % index,
                                           'denoms': [{'nominal': str(n), 'price_rub': 100 + n} for n in range(10)]}
                                          for index in range(20)] + [{'name': 'Россия', 'denoms': [{'nominal': '3000', 'price_rub': 4350}]}]}}
        self.assertIn('Россия', {row['region'] for row in nominal_index(facts)})

    def test_owner_wording(self):
        self.assertIn('помочь не можем', usable_answer('vpn_help'))
        self.assertIn('Пополнение кредитов ChatGPT', usable_answer('limits_exhausted'))
        self.assertTrue(usable_answer('limits_exhausted').startswith('Лимиты ChatGPT можно пополнить'))


if __name__ == '__main__':
    unittest.main()
