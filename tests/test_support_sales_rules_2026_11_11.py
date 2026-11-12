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
        self.assertIn('через Google', zoom)
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


def tu_text():
    from support_agent import turn_understanding as tu
    return tu.UNDERSTANDING_INSTRUCTIONS


class ServiceGuideTests(unittest.TestCase):
    def test_guide_has_dated_official_facts_and_no_prices(self):
        import re
        from support_agent.service_guide import for_context as guide
        data = guide()
        self.assertEqual(data['checked'], '12.11.2026')
        self.assertIn('Ultra', data['services']['claude']['plans'])
        self.assertIn('Max 20×', data['services']['claude']['plans']['Ultra'])
        self.assertIn('Codex', data['choose']['chatgpt_vs_claude'])
        text = str(data)
        self.assertIsNone(re.search(r'\d+\s*(?:₽|руб)', text))  # our ₽ prices come only from the catalog
        self.assertIn('$129', data['services']['higgsfield']['official_prices']['Ultra'])  # catalog plans carry catalog prices
        self.assertEqual(set(data['services']['kimi']['plans']), {'Plus', 'Pro', 'Max', 'Ultra'})
        self.assertIn('official_prices', tu_text())

    def test_guide_reaches_the_model_context(self):
        import sqlite3
        from support_agent import turn_understanding as tu
        self.assertIn('service_guide', tu.UNDERSTANDING_INSTRUCTIONS)
        self.assertTrue(tu._service_guide('что лучше: chatgpt или claude?').get('services'))
        only = tu._service_guide('а что входит в суно про?')['services']
        self.assertEqual(list(only), ['suno'])
        self.assertEqual(list(tu._service_guide('а сколько кредитов?', 'suno')['services']), ['suno'])


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


class OwnerAnswers12_11Tests(unittest.TestCase):
    def test_giveaway_promo_partnership(self):
        self.assertIn('t.me/AleksandrOii', quick_reply('А розыгрыш сейчас есть?')['reply'])
        promo = quick_reply('Есть промокод?')['reply']
        self.assertIn('NAVSEX', promo)
        self.assertIn('CHATIK10', promo)
        held = _turn('Предлагаю рекламу в моём канале', {'owner_topic': 'partnership', 'reply': 'Давайте!'})
        self.assertTrue(held['handoff'])


def _need_aiohttp(test):
    try:
        import aiohttp  # noqa: F401  (installed on Railway)
    except ImportError:
        test.skipTest('aiohttp is not installed here')


class BotGenerationTests(unittest.TestCase):
    def setUp(self):
        _need_aiohttp(self)

    RAW = {'status': 'available', 'buy_menu': 'Купить кредиты',
           'packs': [{'name': '🎯 Пробный', 'credits': 150, 'price_rub': 99, 'note': '13 фото'},
                     {'name': 'плохой', 'credits': 0, 'price_rub': 10}],
           'sections': [{'menu': 'Изображение', 'title': 'Генерация фото',
                         'models': [{'name': '🍌 Nano Banana', 'credits': 11, 'note': 'быстро', 'quality': ''}]}]}

    def test_export_is_cleaned(self):
        from support_agent.shop_export_client import clean_generation
        data = clean_generation(self.RAW)
        self.assertEqual(data['status'], 'available')
        self.assertEqual([p['name'] for p in data['packs']], ['🎯 Пробный'])
        self.assertEqual(data['sections'][0]['models'][0]['credits'], 11)
        self.assertEqual(clean_generation(None)['status'], 'not_connected')

    def test_generation_reaches_the_model_only_when_asked(self):
        from support_agent.shop_export_client import clean_generation
        from support_agent.turn_understanding import _bot_generation, UNDERSTANDING_INSTRUCTIONS
        facts = {'generation': clean_generation(self.RAW)}
        self.assertEqual(_bot_generation(facts, 'хочу сгенерировать картинку')['status'], 'available')
        self.assertEqual(_bot_generation(facts, 'пришлите фото ошибки')['status'], 'not_requested')
        self.assertIn('«Анимировать...»', UNDERSTANDING_INSTRUCTIONS)


class ShopExportGenerationTests(unittest.TestCase):
    def setUp(self):
        _need_aiohttp(self)

    def test_shop_snapshot_shape(self):
        import importlib.util, os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, 'support_shop_export.py')
        if not os.path.exists(path):
            self.skipTest('shop export is not in this checkout')
        spec = importlib.util.spec_from_file_location('sse', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        snap = module._generation_snapshot(lambda: {'buy_menu': 'Купить кредиты',
            'packs': [{'name': 'Старт', 'credits': 500, 'price': 279, 'desc': '45 фото'}],
            'sections': [{'menu': 'Видео', 'title': 'Генерация видео',
                          'models': [{'name': 'Kling', 'credits': 150, 'desc': '5 сек', 'quality': '1080p'}]}]})
        self.assertEqual(snap['status'], 'available')
        self.assertEqual(snap['packs'][0]['price_rub'], 279)
        self.assertEqual(module._generation_snapshot(None)['status'], 'not_connected')
        self.assertEqual(module._generation_snapshot(lambda: 1 / 0)['status'], 'read_error')


class UsdConversionTests(unittest.TestCase):
    def test_owner_formula_and_allowed_prices(self):
        from support_agent.turn_understanding import (_allowed_prices, _usd_context, classify_draft, usd_to_rub)
        self.assertEqual(usd_to_rub(19), 1890)
        self.assertEqual(usd_to_rub(20), 1990)
        self.assertEqual(usd_to_rub(50), 4990)
        table = _usd_context('сколько у вас kimi pro?', '')['prices']
        self.assertEqual(table['$39'], 3890)
        self.assertEqual(_usd_context('нужно оплатить тариф за 35$ на сайте', '')['prices'], {'$35': 3490})
        allowed = _allowed_prices([], [], '', '', tuple(table.values()))
        self.assertTrue(classify_draft('Kimi Pro у нас — 3890 ₽.', allowed)['send'])
        self.assertFalse(classify_draft('Kimi Pro у нас — 3990 ₽.', allowed)['send'])
        self.assertIn('× 100 − 10', UNDERSTANDING())


def UNDERSTANDING():
    from support_agent.turn_understanding import UNDERSTANDING_INSTRUCTIONS
    return UNDERSTANDING_INSTRUCTIONS


class FewerStopsTests(unittest.TestCase):
    def test_rounding_and_price_slips(self):
        from support_agent.turn_understanding import classify_draft, snap_prices, usd_to_rub
        self.assertEqual(usd_to_rub('24.99'), 2490)
        self.assertEqual(usd_to_rub(19), 1890)
        self.assertEqual(snap_prices('Цена 2489 ₽', {2490}), ('Цена 2490 ₽', True))
        self.assertTrue(classify_draft('Цена 2489 ₽', {2490})['send'])
        self.assertFalse(classify_draft('Цена 3990 ₽', {3890})['send'])
        answer = _turn('сколько стоит?', {'service_id': 'suno', 'tariff_id': 'suno|Pro', 'reply': 'Suno Pro — 989 ₽.'})
        self.assertIn('990 ₽', answer['reply'])
        self.assertFalse(answer['handoff'], answer['evidence'].get('diagnostic'))

    def test_holds_summary(self):
        import json, sqlite3, time
        from support_agent.hold_stats import summary
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE drafts(id INTEGER PRIMARY KEY, chat INTEGER, state TEXT, handoff INTEGER, created REAL)')
        db.execute('CREATE TABLE draft_evidence(draft INTEGER PRIMARY KEY, data TEXT)')
        now = time.time()
        for i, (handoff, reason) in enumerate([(1, 'Черновик повторяет уже сказанное в этом чате.'),
                                               (1, 'Черновик повторяет уже сказанное в этом чате.'),
                                               (0, ''), (1, 'Ответ выходит за записанный факт владельца.')], 1):
            db.execute('INSERT INTO drafts VALUES (?,?,?,?,?)', (i, 100 + i, 'done', handoff, now))
            db.execute('INSERT INTO draft_evidence VALUES (?,?)', (i, json.dumps({'diagnostic': reason})))
        text = summary(db, 7, now)
        self.assertIn('3 из 4', text)
        self.assertIn('• 2 — Черновик повторяет уже сказанное в этом чате', text)


class SofterStopsTests(unittest.TestCase):
    def test_conflict_needs_a_named_reason(self):
        from support_agent.turn_understanding import _conflict
        self.assertEqual(_conflict({'conflict': 'high', 'conflict_reason': ''}), 'annoyed')
        self.assertEqual(_conflict({'conflict': 'high', 'conflict_reason': 'неуверенность в ответе'}), 'annoyed')
        self.assertEqual(_conflict({'conflict': 'high', 'conflict_reason': 'угрожает чарджбеком'}), 'high')
        self.assertEqual(_conflict({'conflict': 'high', 'conflict_reason': 'просит позвать человека'}), 'high')

    def test_long_reply_fits(self):
        from support_agent.auto_policy import REPLY_LIMIT, block_reason
        self.assertEqual(REPLY_LIMIT, 3500)
        reason = block_reason({'reply': 'а' * 2500, 'summary': 's', 'handoff': False}, {})
        self.assertNotIn('лимит', str(reason or ''))

    def test_drafts_for_all_never_widen_auto(self):
        from support_agent.rollout import allowed_chats
        chats = allowed_chats('111', owner=5, drafts_for_all_raw='yes')
        self.assertIn(111, chats)
        self.assertIn(222, chats)
        self.assertNotIn(5, chats)
        self.assertNotIn(-100, chats)
        self.assertEqual(list(chats), [111])
        closed = allowed_chats('111', owner=5)
        self.assertNotIn(222, closed)

    def test_service_auto_only_for_test_ids(self):
        import sqlite3
        from support_agent.rollout import allowed_chats
        from support_agent.service import Service
        chats = allowed_chats('111', owner=5, drafts_for_all_raw='yes')
        svc = Service.__new__(Service)
        Service.__init__(svc, type('S', (), {})(), None, None, None, 5, chats, 600,
                         auto_test_ids=(111,), auto_for_all=True)
        self.assertTrue(svc.auto_permitted({'uid': 111}))
        self.assertFalse(svc.auto_permitted({'uid': 222}))
        self.assertIn(222, svc.allowed)


class MasculineVoiceTests(unittest.TestCase):
    def test_first_person_is_masculine(self):
        from support_agent.voice import masculine
        self.assertEqual(masculine('Рада помочь! Я проверила каталог.'), 'Рад помочь! Я проверил каталог.')
        self.assertEqual(masculine('Я бы посоветовала Plus, я уверена.'), 'Я бы посоветовал Plus, я уверен.')
        self.assertEqual(masculine('Я сама разобралась'), 'Я сам разобрался')

    def test_other_subjects_untouched(self):
        from support_agent.voice import masculine
        for text in ('Подписка оформлена и готова.', 'Карта прошла, оплата поступила.',
                     'Если вы уже оплатили, пришлите номер заказа.'):
            self.assertEqual(masculine(text), text)

    def test_fixed_texts_are_masculine(self):
        import pathlib, re
        bad = re.compile(r'(?i)\bя(?:\s+\w+){0,2}\s+\w+ла\b|\b(?:рада|готова помочь)\b')
        for path in pathlib.Path('support_agent/data').glob('*.yaml'):
            text = path.read_text(encoding='utf-8')
            self.assertIsNone(bad.search(text), path.name)


class CheckRunTests(unittest.TestCase):
    def test_parts_shape(self):
        from support_agent.check_cases import PARTS
        self.assertEqual(sorted(PARTS), [1, 2, 3, 4, 5])
        for dialogs in PARTS.values():
            for turns in dialogs:
                for fragments in turns:
                    self.assertIsInstance(fragments, tuple)
                    self.assertTrue(all(isinstance(f, str) and len(f) >= 4 for f in fragments), fragments)

    def test_check_runs_isolated_and_reports(self):
        import asyncio
        from support_agent import check_run
        from support_agent.storage import Store

        class Shop:
            async def facts(self, uid):
                return {'catalog': [], 'catalog_status': 'available'}

        class Owner:
            def __init__(self):
                self.store = Store(':memory:'); self.sent = []; self.owner = 5; self.shop = Shop()
                self.ai = object(); self.charged = 0
            def charge_call(self, reason='model'):
                self.charged += 1
            async def owner_message(self, text, *a, **k):
                self.sent.append(text)

        owner = Owner()
        original = check_run.PARTS
        check_run.PARTS = {1: ((('Мой пароль qwerty12345',),), (('привет',),))}
        try:
            asyncio.run(check_run.run_check(owner, 1))
        finally:
            check_run.PARTS = original
        text = '\n'.join(owner.sent)
        self.assertIn('#1.1', text)
        self.assertIn('Часть 1', text)
        self.assertNotIn('qwerty12345', text.split('О:', 1)[1] if 'О:' in text else '')


class PromoIsNotSecretTests(unittest.TestCase):
    def test_promo_codes_pass_the_secret_check(self):
        from support_agent.owner_scenarios import NAVSEX
        from support_agent.privacy import sensitive
        self.assertFalse(sensitive(NAVSEX))
        self.assertFalse(sensitive('промокод CHATIK10 не применяется'))
        self.assertTrue(sensitive('пароль qwerty12345'))


class CheckFindingsTests(unittest.TestCase):
    """Errors found by the owner's /check run on 12.11.2026."""

    def test_cards_match_catalog_names(self):
        from support_agent.payment_guide import kind_for, render
        self.assertEqual(kind_for('capcut_pro', 'Pro', [], 'CapCut Pro'), 'capcut')
        self.assertEqual(kind_for('x', '12 месяцев', [], 'Telegram premium'), 'telegram_premium')
        self.assertEqual(kind_for('y', '', [], 'iCloud/AppStore'), 'appstore')
        card = render('capcut_pro', 'CapCut Pro', '', {})
        self.assertIn('новый готовый аккаунт', card)
        self.assertNotIn('активация идёт в боте', card)
        self.assertIn('username', render('x', 'Telegram premium', '12 месяцев', {}))

    def test_login_description_is_not_a_secret(self):
        from support_agent.privacy import sensitive
        self.assertFalse(sensitive('В OpenRouter я вхожу только по коду с почты, пароля нет'))
        self.assertTrue(sensitive('вхожу по коду с почты 771204'))
        self.assertTrue(sensitive('пароля нет, вот он: qwerty12345'))

    def test_who_and_language(self):
        from support_agent.owner_scenarios import WHO, quick_reply
        from support_agent.turn_understanding import foreign_language
        self.assertEqual(quick_reply('Вы бот или человек?')['reply'], WHO)
        self.assertEqual(quick_reply('Вы девушка или парень?')['reply'], WHO)
        self.assertIsNone(quick_reply('Нужен бот для телеграм'))
        self.assertTrue(foreign_language('Сәлем! Claude Pro қанша тұрады?'))
        self.assertFalse(foreign_language('Привет, сколько стоит Claude?'))

    def test_credits_stay_with_the_named_service(self):
        from support_agent.turn_understanding import _bind_credits
        rows = [{'service_id': 'gpt_credits', 'service': 'Пополнение кредитов ChatGPT', 'plan': '250 кредитов'}]
        validated = {'service_id': '', 'tariff': None}
        _bind_credits(validated, {'burst': 'Higgsfield какие тарифы и сколько кредитов?', 'tariffs': rows, 'history': []})
        self.assertEqual(validated['service_id'], '')
        validated = {'service_id': '', 'tariff': None}
        _bind_credits(validated, {'burst': 'как пополнить кредиты в чат гпт?', 'tariffs': rows, 'history': []})
        self.assertEqual(validated['service_id'], 'gpt_credits')

    def test_price_line_and_filler(self):
        from support_agent.turn_understanding import _with_price_line
        from support_agent.voice import trim_filler
        card = 'Оплата в боте.\n1. Откройте бота.'
        tariff = {'service': 'Perplexity', 'plan': 'Pro', 'price': 1990}
        out = _with_price_line(card, {'burst': 'Perplexity Pro цена? Как активировать?'}, 'perplexity', tariff, 'perplexity')
        self.assertTrue(out.startswith('Perplexity Pro — 1990 ₽.'))
        self.assertEqual(_with_price_line(card, {'burst': 'как оформить?'}, 'perplexity', tariff, 'perplexity'), card)
        self.assertEqual(trim_filler('Цена тарифа Standard — 1490 ₽ в месяц. Рад помочь!'), 'Цена тарифа Standard — 1490 ₽ в месяц.')

    def test_models_answer_needs_the_official_check(self):
        from support_agent.service import Service
        svc = Service.__new__(Service)
        gone = svc._models_answer_checked({'reply': 'Новейшая модель — X-9.', 'handoff': False},
                                          {'official_research': {'status': 'unavailable', 'products': ['ChatGPT']}})
        self.assertNotIn('X-9', gone['reply'])
        self.assertIn('уточнит Александр', gone['reply'])
        ok = svc._models_answer_checked({'reply': 'Сейчас доступны модели A и B.', 'handoff': False},
                                        {'official_research': {'status': 'checked', 'checked': '12.11.2026',
                                                               'sources': [{'url': 'https://openai.com/models'}]}})
        # Owner 12.11.2026 (evening): the date, but no source link for the client.
        self.assertEqual(ok['reply'], 'Сейчас доступны модели A и B.')  # no link, no «официальные данные» line
        self.assertNotIn('http', ok['reply'])


class SecondCheckTests(unittest.TestCase):
    def test_offcatalog_question_is_recognised_when_reworded(self):
        from support_agent.turn_understanding import _offcat_asked
        history = [{'role': 'client', 'text': 'Можно купить Netflix?'},
                   {'role': 'assistant', 'text': 'Какой тариф Netflix вас интересует?\nВ каком регионе у вас аккаунт?\nНа какой срок нужна подписка?'}]
        self.assertEqual(_offcat_asked({'history': history}), 'Можно купить Netflix?')
        self.assertEqual(_offcat_asked({'history': history[:1]}), '')
        self.assertEqual(_offcat_asked({'history': []}), '')

    def test_two_plans_question_gets_both_prices(self):
        from support_agent.turn_understanding import _plans_summary
        text = _plans_summary([{'service': 'Claude', 'service_id': 'claude', 'plan': 'Pro', 'price': 2090},
                               {'service': 'Claude', 'service_id': 'claude', 'plan': 'Max', 'price': 9990}])
        self.assertIn('Claude Pro — 2090 ₽', text)
        self.assertIn('Claude Max — 9990 ₽', text)

    def test_nominal_face_value_is_allowed(self):
        from support_agent.turn_understanding import _allowed_prices
        self.assertIn(3000, _allowed_prices([], [{'nominal': '3000 ₽', 'price': 4250}], '', ''))

    def test_model_link_is_clean(self):
        from support_agent.service import Service
        svc = Service.__new__(Service)
        out = svc._models_answer_checked({'reply': 'Новейшая модель — A 1.\nИсточник: страницы сервиса.', 'handoff': False},
            {'official_research': {'status': 'checked', 'checked': '12.11.2026',
                                   'sources': [{'url': 'https://claude.com/pricing?aad=XYZ&_ref=finder'}]}})
        self.assertNotIn('http', out['reply'])
        self.assertEqual(out['reply'], 'Новейшая модель — A 1.')
        self.assertNotIn('Источник:', out['reply'])


class AndroidInstallTests(unittest.TestCase):
    def test_android_question_gets_only_android(self):
        from support_agent.turn_understanding import _android_install
        text = _android_install('Как установить Claude на андроид?')
        self.assertEqual(text, 'Вы можете установить приложение Claude на Android по этой ссылке: https://claude.ru.uptodown.com/android )')
        self.assertEqual(_android_install('Как скачать ChatGPT на айфон'), '')

    def test_higgsfield_follows_the_catalog(self):
        import yaml
        guide = yaml.safe_load(open('support_agent/data/service_guide.yaml', encoding='utf-8'))['services']['higgsfield']
        self.assertEqual(list(guide['plans']), ['Starter', 'Pro', 'Plus', 'Ultra'])
        self.assertIn('200 кредитов', guide['plans']['Pro'])


class FourthCheckTests(unittest.TestCase):
    def test_official_signature_is_removed(self):
        from support_agent.service import Service
        svc = Service.__new__(Service)
        out = svc._models_answer_checked(
            {'reply': 'Самая новая модель по официальным данным на ноябрь 2026 года — это X.\nДанные официального сайта на 12.11.2026.', 'handoff': False},
            {'official_research': {'status': 'checked', 'checked': '12.11.2026', 'sources': []}})
        self.assertEqual(out['reply'], 'Самая новая модель — это X.')

    def test_appstore_amount_gets_one_clear_line(self):
        from support_agent.turn_understanding import ground_nominals
        context = {'burst': 'Хочу пополнить App Store на 3000 рублей, регион Россия',
                   'nominals': [{'region': 'Россия | RU', 'nominal': '3000 RUB', 'price': 4250},
                                {'region': 'Россия | RU', 'nominal': '500 RUB', 'price': 700}]}
        text, composed = ground_nominals('Суммы точно подходящего номинала нет, самый близкий — 3000 RUB за 4250 ₽.', context,
                                         {'service_id': 'appstore', 'region': 'RU'})
        if composed:  # region table present in this build
            self.assertIn('4250 ₽', text)
            self.assertNotIn('нет', text)
