"""Owner 21.11.2026: the assistant continues Alexander's conversation (dialog-first input, current step,
continue-step mode, history-aware quick replies)."""
import tempfile
import time
import unittest

from support_agent.storage import Store
from support_agent.turn_understanding import (build_context, continue_step, current_step, enforce_turn_policy,
                                              model_input_text, model_view, package, render_current_step,
                                              render_dialog)

NOW = 1_800_000_000.0
HOUR = 3600


def suno_facts():
    rows = [('Suno', 'suno', 'Pro', 990), ('Suno', 'suno', 'Premier', 2990), ('ChatGPT', 'chatgpt', 'Plus', 1990),
            ('ChatGPT', 'chatgpt', 'Pro', 19990), ('Zoom', 'zoom', 'Pro', 1890)]
    return {'catalog': [{'service': s, 'service_key': k, 'plan': p, 'catalog_price_rub': price,
                         'fulfillment': 'account_details'} for s, k, p, price in rows],
            'catalog_status': 'available',
            'bot_navigation': {'status': 'available', 'username': 'neirosetkaa_bot', 'catalog_webapp': True}}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(suffix='.db')
        self.store = Store(self.file.name)
        self.mid = 0
        self.cid = None

    def tearDown(self):
        self.file.close()

    def put(self, role, text, at):
        self.mid += 1
        self.cid = self.store.ingest('biz', 77, self.mid, role, text, {}, at)

    def context(self, facts=None):
        return build_context(self.store.db, self.cid, facts or suno_facts(), now=NOW)


class RenderTests(unittest.TestCase):
    def test_roles_times_and_gaps(self):
        history = [{'role': 'client', 'text': 'Хочу Suno', 'at': NOW - 3 * 86400},
                   {'role': 'owner', 'text': 'Добрый день! Какой тариф?', 'at': NOW - 3 * 86400 + 60},
                   {'role': 'client', 'text': 'Pro', 'at': NOW - 12 * 60 - 30},
                   {'role': 'owner', 'text': 'Пришлите почту', 'at': NOW - 12 * 60},
                   {'role': 'assistant', 'text': 'Александр продолжит здесь.', 'at': NOW - 2 * 60}]
        text = render_dialog(history, NOW)
        lines = text.split('\n')
        self.assertEqual(lines[0], '[-3 дн] Клиент: Хочу Suno')
        self.assertEqual(lines[1], '[-2 дн] Александр: Добрый день! Какой тариф?')
        self.assertIn('— перерыв 2 дня —', lines[2])
        self.assertEqual(lines[3], '[-12 мин] Клиент: Pro')
        self.assertEqual(lines[4], '[-12 мин] Александр: Пришлите почту')
        self.assertEqual(lines[5], '[-2 мин] Помощник: Александр продолжит здесь.')
        self.assertEqual(text.count('перерыв'), 1)

    def test_hours_gap_and_missing_time(self):
        text = render_dialog([{'role': 'client', 'text': 'а', 'at': NOW - 8 * HOUR},
                              {'role': 'client', 'text': 'б', 'at': NOW - HOUR},
                              {'role': 'owner', 'text': 'в'}], NOW)
        self.assertIn('— перерыв 7 ч —', text)
        self.assertIn('\nАлександр: в', text)


class CurrentStepTests(unittest.TestCase):
    def test_several_consecutive_owner_messages(self):
        history = [{'role': 'client', 'text': 'Хочу Claude', 'at': NOW - 3 * HOUR},
                   {'role': 'owner', 'text': 'Отлично!', 'at': NOW - HOUR},
                   {'role': 'owner', 'text': 'Сейчас отправил код на вашу почту.', 'at': NOW - HOUR + 10},
                   {'role': 'owner', 'text': 'Пришлите, пожалуйста, код из письма', 'at': NOW - HOUR + 20},
                   {'role': 'client', 'text': 'Секунду, ищу', 'at': NOW - 120}]
        step = current_step(history)
        self.assertEqual(step['owner_messages'], ['Отлично!', 'Сейчас отправил код на вашу почту.',
                                                  'Пришлите, пожалуйста, код из письма'])
        self.assertIn('Пришлите, пожалуйста, код из письма', step['asked'])
        self.assertEqual(step['client_answered'], ['Секунду, ищу'])
        text = render_current_step(step, NOW)
        self.assertIn('ТЕКУЩИЙ ШАГ', text)
        for said in step['owner_messages']:
            self.assertIn(said, text)
        self.assertIn('Александр просил у клиента: Пришлите, пожалуйста, код из письма; клиент ответил: Секунду, ищу', text)

    def test_no_owner_no_step(self):
        self.assertEqual(current_step([{'role': 'client', 'text': 'Привет'}]), {})


class BuildContextTests(StoreCase):
    def test_dialog_first_and_trimmed_model_view(self):
        self.put('client', 'Здравствуйте, хочу Suno', NOW - 2 * HOUR)
        self.put('owner', 'Добрый день! Suno Pro оформляем на вашу почту. Пришлите почту от аккаунта Suno', NOW - HOUR)
        self.put('client', 'Продолжим оформление суно?', NOW - 30)
        context = self.context()
        self.assertEqual([row['role'] for row in context['history']], ['client', 'owner', 'client'])
        self.assertTrue(context['ongoing_dialog'])
        self.assertTrue(context['continue_step'])
        text = model_input_text(context)
        self.assertTrue(text.startswith('ПЕРЕПИСКА'))
        self.assertLess(text.index('[-1 ч] Александр: Добрый день!'), text.index('ДАННЫЕ (JSON'))
        self.assertLess(text.index('ТЕКУЩИЙ ШАГ'), text.index('ДАННЫЕ (JSON'))
        view = model_view(context)
        self.assertLessEqual(len(view['faq']), 8)
        self.assertGreater(len(view['faq_index']), 8)
        self.assertEqual(len(context['faq']), len(view['faq_index']))  # code keeps the full FAQ
        self.assertNotIn('dialog_text', view)

    def test_window_is_fifteen_messages_and_six_k_chars(self):
        for n in range(20):
            self.put('client' if n % 2 else 'owner', 'сообщение %d ' % n + 'x' * 500, NOW - (20 - n) * 60)
        context = self.context()
        self.assertLessEqual(len(context['history']), 15)
        self.assertLessEqual(sum(len(row['text']) for row in context['history']), 6000)
        self.assertIn('сообщение 19', context['history'][-1]['text'])

    def test_tariffs_cut_to_named_services(self):
        facts = suno_facts()
        facts['catalog'] += [{'service': 'Svc%d' % n, 'service_key': 'svc%d' % n, 'plan': 'Pro', 'catalog_price_rub': 1000 + n,
                              'fulfillment': 'account_details'} for n in range(60)]
        self.put('client', 'Сколько стоит Suno?', NOW - 10)
        context = build_context(self.store.db, self.cid, facts, now=NOW)
        view = model_view(context)
        self.assertEqual({row['service_id'] for row in view['tariffs']}, {'suno'})
        self.assertGreater(len(context['tariffs']), 60)  # code keeps every tariff for price checks
        self.assertIn(['svc5', 'Svc5'], view['services_index'])


def _understanding(reply, **extra):
    base = {'intent': 'how_to', 'service_id': 'suno', 'tariff_id': '', 'confidence': 'high', 'clarify_ids': [],
            'topic_changed': False, 'known_facts': [], 'missing_facts': [], 'questions': ['Продолжим оформление суно?'],
            'vpn_advice': False, 'region': '', 'payment_asked': True, 'fulfillment_choice': '', 'setup_email': '',
            'setup_password': '', 'setup_recovery': '', 'reask_advisories': [], 'matched_tariff_ids': [],
            'conflict': 'none', 'conflict_reason': '', 'owner_alignment': 'follows', 'owner_topic': 'none',
            'situation': 'buy', 'situations': ['buy'], 'beyond_owner_fact': False, 'refund_order': '',
            'refund_service': '', 'refund_reason': '', 'risk': 'instruct', 'order_number': '',
            'receipt_amount_unverified': '', 'reply': reply, 'summary': 'Продолжение.'}
    base.update(extra)
    return base


def _pipeline(understanding, context):
    from support_agent.playbook import finalize, tidy
    from support_agent.rescue import answer_instead_of_stop
    from support_agent.turn_understanding import validate
    answer = package(understanding, context)
    answer = enforce_turn_policy(answer, understanding, context, {'status': 'fresh'})
    answer = answer_instead_of_stop(answer, understanding, context)
    answer = finalize(answer, validate(understanding, context), context)
    return tidy(answer, context)


class ContinueStepTests(StoreCase):
    REPLY = 'Да, продолжаем. Пришлите, пожалуйста, почту от аккаунта Suno, как просил Александр.'

    def test_mid_flow_suno_keeps_the_model_answer(self):
        self.put('client', 'Здравствуйте, хочу Suno', NOW - 2 * HOUR)
        self.put('owner', 'Добрый день! Suno Pro оформляем на вашу почту. Пришлите почту от аккаунта Suno', NOW - HOUR)
        self.put('client', 'Продолжим оформление суно?', NOW - 30)
        context = self.context()
        self.assertTrue(continue_step(context))
        answer = _pipeline(_understanding(self.REPLY), context)
        self.assertFalse(answer['handoff'])
        self.assertEqual(answer['reply'], self.REPLY)
        self.assertNotIn('Есть такие варианты', answer['reply'])  # no tariff list
        self.assertNotIn('Premier', answer['reply'])
        self.assertTrue(answer['evidence']['continue_step'])
        self.assertFalse(answer['evidence'].get('composed_by_code'))

    def test_mid_flow_faq_topic_is_not_appended(self):
        self.put('owner', 'Добрый день! Suno Pro оформляем на вашу почту. Пришлите почту от аккаунта Suno', NOW - HOUR)
        self.put('client', 'Продолжим оформление суно? И сколько длится подписка?', NOW - 30)
        context = self.context()
        reply = 'Да, продолжаем: пришлите почту от аккаунта Suno. Подписка на месяц.'
        understanding = _understanding(reply, payment_asked=False, owner_topic='subscription_term',
                                       questions=['Продолжим?', 'Сколько длится?'], situation='other', situations=['other'])
        answer = _pipeline(understanding, context)
        self.assertEqual(answer['reply'], reply)
        self.assertFalse(answer['evidence'].get('owner_text_used'))

    def test_fresh_chat_still_gets_the_tariff_list(self):
        self.put('client', 'Хочу оплатить суно', NOW - 30)
        context = self.context()
        self.assertFalse(continue_step(context))
        answer = _pipeline(_understanding('Конечно!'), context)
        self.assertIn('Есть такие варианты', answer['reply'])
        self.assertIn('990 ₽', answer['reply'])

    def test_purchase_of_another_service_is_not_continue_step(self):
        self.put('owner', 'Добрый день! Suno Pro оформляем на вашу почту. Пришлите почту от аккаунта Suno', NOW - HOUR)
        self.put('client', 'А сколько стоит ChatGPT?', NOW - 30)
        self.assertFalse(continue_step(self.context()))

    def test_old_owner_message_is_not_continue_step(self):
        self.put('owner', 'Пришлите почту от аккаунта Suno', NOW - 30 * HOUR)
        self.put('client', 'Продолжим оформление суно?', NOW - 30)
        self.assertFalse(continue_step(self.context()))

    def test_token_draft_kept_in_alexanders_step(self):
        self.put('owner', 'Зайдите на chatgpt.com и пришлите токен из api/auth/session', NOW - HOUR)
        self.put('client', 'А куда именно вставить токен?', NOW - 30)
        context = self.context()
        reply = 'Токен пришлите сюда, в этот чат, одним сообщением — Александр продолжит оформление.'
        understanding = _understanding(reply, service_id='', payment_asked=False, owner_topic='token_copy',
                                       situation='other', situations=['other'])
        answer = _pipeline(understanding, context)
        self.assertIn('Токен пришлите сюда', answer['reply'])


class QuickReplyTests(unittest.TestCase):
    def _mid(self, burst):
        return {'burst': burst,
                'history': [{'role': 'owner', 'text': 'Пришлите, пожалуйста, код из письма', 'at': NOW - HOUR},
                            {'role': 'client', 'text': burst, 'at': NOW - 30}]}

    def test_not_a_closing_mid_step(self):
        from support_agent.owner_scenarios import quick_reply
        self.assertIsNone(quick_reply('спасибо, пошла искать', context=self._mid('спасибо, пошла искать')))
        self.assertIsNone(quick_reply('Спасибо', context=self._mid('Спасибо')))

    def test_calling_alexander_mid_step_is_honest(self):
        from support_agent.owner_scenarios import STEP_PING, quick_reply
        out = quick_reply('Александр?)', context=self._mid('Александр?)'))
        self.assertEqual(out['reply'], STEP_PING)
        self.assertIn('ИИ-помощник', out['reply'])
        self.assertNotIn('это Александр', out['reply'])

    def test_fresh_chat_unchanged(self):
        from support_agent.owner_scenarios import HUMAN, quick_reply
        self.assertEqual(quick_reply('Спасибо!')['reply'], 'Рад помочь, обращайтесь!')
        self.assertEqual(quick_reply('Спасибо!', context={'burst': 'Спасибо!',
                                                          'history': [{'role': 'client', 'text': 'Спасибо!'}]})['reply'],
                         'Рад помочь, обращайтесь!')
        self.assertEqual(quick_reply('Александр?)')['reply'], HUMAN)
        self.assertIn('NAVSEX', quick_reply('Есть промокод?')['reply'])

    def test_closed_step_thanks_gets_closing(self):
        from support_agent.owner_scenarios import quick_reply
        ctx = {'burst': 'Спасибо!', 'history': [{'role': 'owner', 'text': 'Подписка успешно активирована', 'at': NOW - HOUR},
                                                 {'role': 'client', 'text': 'Спасибо!', 'at': NOW - 30}]}
        self.assertEqual(quick_reply('Спасибо!', context=ctx)['reply'], 'Рад помочь, обращайтесь!')

    def test_never_claims_to_be_alexander(self):
        from support_agent.playbook import tidy
        out = tidy({'reply': 'Да, это Александр 🙂 Код пришёл?', 'handoff': False, 'evidence': {}}, {'burst': 'Александр?'})
        self.assertNotIn('это Александр', out['reply'])
        self.assertIn('ИИ-помощник Александра', out['reply'])


class PromptTests(unittest.TestCase):
    def test_priority_puts_alexanders_step_first(self):
        from support_agent.clients import IDENTITY_RULES, priority_header
        ongoing = priority_header({'ongoing_dialog': True})
        self.assertLess(ongoing.index('продолжить шаг Александра'), ongoing.index('situation'))
        self.assertIn('при первой возможности', ongoing)
        fresh = priority_header({'ongoing_dialog': False})
        self.assertNotIn('ЕГО разговор', fresh)
        self.assertIn('никогда', IDENTITY_RULES.casefold())
        self.assertIn('при первой возможности', IDENTITY_RULES)


if __name__ == '__main__':
    unittest.main()
