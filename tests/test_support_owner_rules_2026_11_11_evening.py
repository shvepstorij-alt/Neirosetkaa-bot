"""Owner decisions of 11.11.2026 (evening): VPN, Outlook, app download, Claude ban, course bonus."""
import asyncio
import unittest

from support_agent.customer_format import format_customer
from support_agent.faq import collect_fields, for_context, usable_answer
from support_agent.service import present_outgoing
from support_agent.turn_understanding import enforce_turn_policy, package


def _turn(topic, reply, burst='', setup=None, state=None, service_id=''):
    context = {'tariffs': [], 'nominals': [], 'history': [], 'faq': for_context(), 'burst': burst,
               'dialog_state': state or {}}
    understanding = {'intent': 'question', 'service_id': service_id, 'tariff_id': '', 'confidence': 'high',
                     'reply': reply, 'summary': 'Ответ.', 'questions': ['Вопрос клиента'],
                     'owner_topic': topic, 'beyond_owner_fact': False, 'refund_order': '',
                     'refund_service': '', 'refund_reason': '', 'conflict': 'none', 'owner_alignment': 'follows'}
    understanding.update(setup or {})
    return enforce_turn_policy(package(understanding, context), understanding, context, {'status': 'fresh'})


class VpnTests(unittest.TestCase):
    def test_two_vpn_links_reach_the_client_intact(self):
        answer = _turn('vpn_help', 'Подберу и настрою вам VPN.')
        self.assertFalse(answer['handoff'], answer.get('evidence', {}).get('diagnostic'))
        shown, _ = present_outgoing({'reply': answer['reply'], 'evidence': {}}, {}, True)
        plain, _html = format_customer(shown['reply'])
        self.assertIn('https://telegram.me/DureVpnBot?start=YYFpHPog9', plain)
        self.assertIn('https://t.me/v2raytnn_bot?start=facdaf71-69b8-4155-a3f7-bc245a6135fe', plain)
        self.assertIn('поддержку самого VPN', plain)


class PlainTopicTests(unittest.TestCase):
    def test_owner_text_is_sent(self):
        for topic in ('app_download', 'claude_account_blocked'):
            answer = _turn(topic, usable_answer(topic))
            self.assertFalse(answer['handoff'], (topic, answer.get('evidence', {}).get('diagnostic')))
        self.assertIn('Медиаматериалы и покупки', usable_answer('app_download'))
        self.assertIn('баланс', usable_answer('app_download'))
        self.assertIn('uptodown', usable_answer('app_download'))
        self.assertIn('не получится', usable_answer('claude_account_blocked'))


class OutlookTests(unittest.TestCase):
    def test_first_question_gets_gmail_then_outlook_text(self):
        self.assertEqual(collect_fields('new_account_email'), ['setup_email', 'setup_password', 'setup_recovery'])
        answer = _turn('new_account_email', 'Создам вам аккаунт, обещаю.', burst='Нужен новый аккаунт клод')
        self.assertFalse(answer['handoff'])
        self.assertEqual(answer['reply'], usable_answer('new_account_email'))
        self.assertIn('gmail.com', answer['reply'])
        self.assertIn('outlook.com', answer['reply'])

    def test_partial_details_ask_only_what_is_missing(self):
        answer = _turn('new_account_email', 'Хорошо.', burst='адрес ivan.petrov2026',
                       setup={'setup_email': 'ivan.petrov2026@outlook.com'})
        self.assertFalse(answer['handoff'])
        self.assertIn('пароль', answer['reply'])
        self.assertIn('резервную', answer['reply'])
        self.assertNotIn('какой адрес', answer['reply'])

    def test_complete_details_go_to_the_owner_once(self):
        setup = {'setup_email': 'ivan.petrov2026@outlook.com', 'setup_password': 'Qwerty2026',
                 'setup_recovery': 'ivan@mail.ru'}
        ready = _turn('new_account_email', 'Спасибо!', burst='...', setup=setup, service_id='claude')
        self.assertTrue(ready['handoff'])
        self.assertTrue(ready['evidence'].get('account_setup_hold'))
        self.assertEqual(ready['evidence']['account_setup']['setup_recovery'], 'ivan@mail.ru')
        later = _turn('none', 'Пожалуйста! Обращайтесь.', burst='спасибо',
                      state={'collected': dict(setup, setup_topic=1)})
        self.assertFalse(later['evidence'].get('account_setup_hold'))

    def test_password_message_alerts_the_owner_without_the_password(self):
        from support_agent.service import Service
        sent = []

        class Fake:
            store = type('S', (), {'db': None})()
            chat_identity = lambda self, chat: 'Клиент'

            async def owner_message(self, text):
                sent.append(text)

        import support_agent.dialog_state as ds
        original = ds.load
        ds.load = lambda db, chat: {'collected': {'setup_topic': 1, 'setup_email': 'ivan@outlook.com'},
                                    'service_label': 'Claude'}
        try:
            notice = {'reply': 'С этим вложением автоматически не получится продолжить.',
                      'summary': 'В обращении возможны данные доступа. Они не переданы в подготовку ответа.',
                      'handoff': True, 'evidence': {}}
            out = asyncio.run(Service.maybe_secret_setup_notice(Fake(), {'chat': 1}, notice))
        finally:
            ds.load = original
        self.assertTrue(out['evidence'].get('account_setup_manual'))
        self.assertIn('Александр', out['reply'])
        self.assertEqual(len(sent), 1)
        self.assertIn('outlook.com для Claude', sent[0])
        self.assertIn('ivan@outlook.com', sent[0])


class CourseTests(unittest.TestCase):
    def test_ask_for_course_email_then_hand_over(self):
        asked = _turn('course_bonus', 'Подписку от курса уже активировали.', burst='Мне положен бонус Клод от курса')
        self.assertFalse(asked['handoff'])
        self.assertEqual(asked['reply'], usable_answer('course_bonus'))
        got = _turn('none', 'Спасибо!', burst='вот почта anna.k@gmail.com', state={'collected': {'course_topic': 1}})
        self.assertTrue(got['handoff'])
        self.assertEqual(got['evidence'].get('course_email'), 'anna.k@gmail.com')


if __name__ == '__main__':
    unittest.main()
