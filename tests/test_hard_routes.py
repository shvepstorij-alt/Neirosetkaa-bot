import unittest

from support_agent import hard_routes, playbook
from support_agent.hard_routes import UNKNOWN_PHRASE, apply

ASK_ORDER = ('Подскажите, пожалуйста, номер заказа из бота и пришлите скриншот того этапа, на котором не получается, '
             'или текста ошибки. Если номера заказа нет, пришлите чек об оплате из банка.\n\n'
             'Если это ChatGPT: найдите в боте сообщение, которое пришло после оплаты, — в нём есть кнопка '
             '«Активировать подписку».')


def run(burst, reply, history=None, **extra):
    answer = dict({'reply': reply, 'evidence': {}}, **extra)
    return apply(answer, {'burst': burst, 'history': history or []})


class Reseller(unittest.TestCase):
    def test_wholesale_handoff(self):
        out = run('беру подписки оптом для перепродажи, какие у вас цены для партнеров?', 'Оформить подписку можно в боте.')
        self.assertTrue(out['handoff'])
        self.assertEqual(out['reply'], hard_routes.RESELLER_REPLY)
        self.assertEqual(out['evidence']['handoff_reason'], 'Перекупщик/опт — отвечает Александр.')
        self.assertIn('reseller', out['evidence']['hard_routes'])

    def test_english_plati(self):
        out = run('Hi, I resell subscriptions on Plati, what is your wholesale price?', 'Hello!')
        self.assertTrue(out.get('handoff'))

    def test_regular_and_partnership_untouched(self):
        for burst in ('хочу Plus', 'Хочу предложить сотрудничество, у меня канал про нейросети', 'как оптимально выбрать тариф?'):
            out = run(burst, 'Ответ.')
            self.assertFalse(out.get('handoff'), burst)
            self.assertNotIn('hard_routes', out.get('evidence') or {})


class Business(unittest.TestCase):
    def test_gpt_business(self):
        out = run('спасибо большое !!! / Добрый день Саша а как установить жпт бизнес ? / через вас Можно ?',
                  'Здравствуйте!\n\nОплата в @neirosetkaa_bot.\n• ChatGPT Pro50 — 48990 ₽')
        self.assertIn('4990', out['reply'])
        self.assertIn('Другой сервис', out['reply'])
        self.assertNotIn('Pro50', out['reply'])
        self.assertTrue(out['reply'].startswith('Здравствуйте!'))
        self.assertTrue(out['evidence']['owner_text_used'])

    def test_bare_business(self):
        self.assertIn('4990', run('А business тариф есть?', 'Есть Plus.')['reply'])

    def test_canva_business_untouched(self):
        out = run('У вас есть Canva Business?', 'Canva Pro можно оформить.')
        self.assertEqual(out['reply'], 'Canva Pro можно оформить.')

    def test_already_answered(self):
        text = hard_routes._faq('chatgpt_business')
        out = run('ChatGPT Business сколько?', 'Здравствуйте!\n\n' + text)
        self.assertNotIn('hard_routes', out['evidence'])


class OrderNumber(unittest.TestCase):
    def test_known_order(self):
        out = run('Оплатил заказ #12345, подписка не пришла', ASK_ORDER)
        self.assertTrue(out['reply'].startswith('Номер заказа #12345 получил.'), out['reply'])
        self.assertNotRegex(out['reply'].lower(), r'(подскажите|пришлите)[^.]{0,40}номер заказа')  # no re-ask; «Номер заказа #N получил» is fine
        self.assertNotIn('номера заказа', out['reply'].lower())
        self.assertIn('Пришлите, пожалуйста, скриншот', out['reply'])
        self.assertIn('Активировать подписку', out['reply'])

    def test_order_and_freekassa(self):
        burst = 'Заказ #9974 freekassa 346894753 / оплатил, есть ли смысл активировать'
        out = run(burst, 'Напишите, пожалуйста, номер заказа и уточните, какая подписка.')
        self.assertIn('Номер заказа #9974 получил.', out['reply'])
        self.assertNotIn('номер заказа', out['reply'])
        self.assertIn('Уточните, пожалуйста, какая подписка.', out['reply'])

    def test_freekassa_only(self):
        out = run('freekassa 346894753 не пришло', 'Пришлите номер заказа и скриншот экрана, Александр проверит.')
        self.assertEqual(out['reply'], 'Номер FreeKassa 346894753 получил. Пришлите скриншот экрана, Александр проверит.')

    def test_from_history_tail(self):
        history = [{'role': 'client', 'text': 'номер заказа 55123', 'at': 1}]
        out = run('не активируется', 'Здравствуйте! Подскажите, пожалуйста, номер заказа.', history=history)
        self.assertEqual(out['reply'], 'Здравствуйте! Номер заказа #55123 получил.')

    def test_no_number_keeps_request(self):
        out = run('Оплатил, подписка не пришла', ASK_ORDER)
        self.assertEqual(out['reply'], ASK_ORDER)


class UnknownGlued(unittest.TestCase):
    def test_glued_removed(self):
        reply = ('В описании Gemini AI Pro есть Pro-модель, Deep Research и генерация видео. Точный ответ уточнит Александр.'
                 '\n\n' + UNKNOWN_PHRASE)
        out = run('можно ли агентов создавать?', reply)
        self.assertNotIn('точно подсказать', out['reply'])
        self.assertIn('Deep Research', out['reply'])

    def test_whole_reply_kept(self):
        self.assertEqual(run('ццц', UNKNOWN_PHRASE)['reply'], UNKNOWN_PHRASE)
        self.assertEqual(run('ццц', 'Здравствуйте!\n\n' + UNKNOWN_PHRASE)['reply'], 'Здравствуйте!\n\n' + UNKNOWN_PHRASE)


class PaymentMethod(unittest.TestCase):
    def test_crypto(self):
        out = run('можно криптой?', UNKNOWN_PHRASE)
        self.assertIn('СБП', out['reply'])
        self.assertIn('«Мир»', out['reply'])
        self.assertIn('payment_method_unknown', out['evidence']['hard_routes'])

    def test_paypal_and_card_transfer(self):
        for burst in ('Можно оплатить через PayPal?', 'А можно переводом на карту?', 'есть оплата в USDT?'):
            self.assertIn('СБП', run(burst, UNKNOWN_PHRASE)['reply'], burst)

    def test_real_answer_untouched(self):
        out = run('можно криптой?', 'Криптой оплатить нельзя, только СБП или «Мир».')
        self.assertEqual(out['reply'], 'Криптой оплатить нельзя, только СБП или «Мир».')

    def test_not_payment_question(self):
        self.assertEqual(run('как создать агента?', UNKNOWN_PHRASE)['reply'], UNKNOWN_PHRASE)


class ClaudeLicence(unittest.TestCase):
    def test_or_licence(self):
        reply = ('Из российских документов можно попробовать только загранпаспорт РФ или водительские права, '
                 'как написал Александр. Успех не гарантирован.')
        out = run('подойдёт ли паспорт РФ для Claude?', reply)
        self.assertIn('загранпаспорт РФ, как написал Александр.', out['reply'])
        self.assertNotIn('водительск', out['reply'])

    def test_licence_sentence(self):
        out = run('права подойдут?', 'Водительские права тоже подходят. Лучше иностранные документы.')
        self.assertEqual(out['reply'], 'Лучше иностранные документы.')

    def test_untouched(self):
        out = run('паспорт?', 'Подойдёт загранпаспорт РФ.')
        self.assertEqual(out['reply'], 'Подойдёт загранпаспорт РФ.')


class Safety(unittest.TestCase):
    def test_plain_plus_untouched(self):
        answer = {'reply': 'ChatGPT Plus — 1990 ₽. Оплатить можно в боте @neirosetkaa_bot.', 'evidence': {}}
        self.assertIs(apply(answer, {'burst': 'хочу Plus', 'history': []}), answer)

    def test_failing_rule_skipped(self):
        def boom(*a):
            raise RuntimeError
        saved = hard_routes.RULES
        hard_routes.RULES = (('boom', boom, boom),) + saved
        try:
            out = run('можно криптой?', UNKNOWN_PHRASE)
        finally:
            hard_routes.RULES = saved
        self.assertIn('СБП', out['reply'])

    def test_never_empty(self):
        saved = hard_routes.RULES
        hard_routes.RULES = (('empty', lambda *a: True, lambda ans, b, c: dict(ans, reply='')),)
        try:
            out = run('x', 'Исходный ответ.')
        finally:
            hard_routes.RULES = saved
        self.assertEqual(out['reply'], 'Исходный ответ.')

    def test_non_dict(self):
        self.assertIsNone(apply(None, {}))
        self.assertEqual(apply('text', {}), 'text')

    def test_wired_into_tidy(self):
        out = playbook.tidy({'reply': UNKNOWN_PHRASE, 'evidence': {}}, {'burst': 'можно криптой?', 'history': []})
        self.assertIn('СБП', out['reply'])
        out = playbook.tidy({'reply': 'Ок.'}, {'burst': 'беру оптом', 'history': []})
        self.assertTrue(out['handoff'])


if __name__ == '__main__':
    unittest.main()


class OwnerAnswers21Nov(unittest.TestCase):
    """Owner answers 21.11.2026: payment failure text, «оплатил, не работает» draft-only, Claude paid → consent."""

    def test_payment_failure_gets_owner_text(self):
        out = hard_routes.apply({'reply': 'Оплата в @neirosetkaa_bot.', 'handoff': False, 'evidence': {}},
                                {'burst': 'по этим кнопкам никак не могу оплатить'})
        self.assertIn('/start', out['reply'])
        self.assertIn('ошибку', out['reply'])

    def test_paid_issue_model_text_is_draft_only(self):
        out = hard_routes.apply({'reply': 'У Kimi Pro вдвое больше кредитов.', 'handoff': False, 'evidence': {}},
                                {'burst': 'оплатил кими, подписка не появилась'})
        self.assertTrue(out['evidence']['situation_hold'])
        owner = hard_routes.apply({'reply': 'Пришлите номер заказа.', 'handoff': False, 'evidence': {'owner_text_used': True}},
                                  {'burst': 'оплатил кими, подписка не появилась'})
        self.assertFalse(owner['evidence'].get('situation_hold'))

    def test_claude_paid_notice_asks_consent_first(self):
        from support_agent.paid_notice import parse, compose
        kind, text = compose(parse('Оплатил заказ #555\nСервис: Claude\nТариф: Max'),
                             [{'service_id': 'claude', 'service': 'Claude', 'plan': 'Max', 'price': 9990}])
        self.assertIn('согласны с этими рисками', text)
        self.assertNotIn('пароль', text)
        self.assertNotIn('водительск', text)

    def test_plan_after_dash(self):
        from support_agent.paid_notice import parse
        self.assertEqual(parse('Оплатил заказ #1\nСервис: ✨ ChatGPT - Pro 5×')['plan'], 'Pro 5×')


class OwnerAnswers21NovB(unittest.TestCase):
    """Owner 21.11.2026: conflict → calm reply + urgent; «вы бот?» → honest and keep helping; no time promises."""

    def test_conflict(self):
        out = hard_routes.apply({'reply': 'Пришлите номер заказа.', 'handoff': False, 'evidence': {}},
                                {'burst': 'вы мошенники, открою спор в банке'})
        self.assertEqual(out['reply'], hard_routes.CONFLICT_REPLY)
        self.assertTrue(out['evidence']['urgent'])

    def test_bot_question_is_honest(self):
        out = hard_routes.apply({'reply': 'Да, это Александр 🙂', 'handoff': False, 'evidence': {}}, {'burst': 'вы бот?'})
        self.assertIn('ИИ-помощник Александра', out['reply'])
        self.assertNotIn('это Александр', out['reply'])

    def test_human_request_keeps_the_answer(self):
        out = hard_routes.apply({'reply': 'ChatGPT Plus — 1990 ₽. Оплатить можно в боте.', 'handoff': False, 'evidence': {}},
                                {'burst': 'позовите александра, сколько стоит plus?'})
        self.assertIn('1990', out['reply'])
        self.assertIn('при первой возможности', out['reply'])

    def test_no_time_promise_in_handoff(self):
        from support_agent.handoff import handoff_text
        self.assertNotRegex(handoff_text(), r'получаса|в течение часа')


class OwnerAnswers21NovC(unittest.TestCase):
    """Owner 21.11.2026 (round 3): conflict auto-sent, calm refund asks reason+order, Claude Max consent → email."""

    def test_conflict_reply_is_auto(self):
        from support_agent.auto_policy import block_reason
        out = hard_routes.apply({'reply': 'x', 'handoff': True, 'evidence': {'conflict_hold': True}},
                                {'burst': 'вы мошенники'})
        self.assertFalse(out['evidence']['conflict_hold'])
        self.assertTrue(out['evidence']['urgent'])
        self.assertNotIn('конфликт', str(block_reason(out) or ''))

    def test_calm_refund_asks_reason_and_order(self):
        out = hard_routes.apply({'reply': 'Вернём деньги!', 'handoff': False, 'evidence': {}},
                                {'burst': 'хочу вернуть деньги'})
        self.assertEqual(out['reply'], hard_routes.REFUND_ASK)
        self.assertNotIn('conflict', out['evidence'].get('hard_routes'))

    def test_refund_with_reason_and_order_goes_to_owner(self):
        out = hard_routes.apply({'reply': 'x', 'handoff': False, 'evidence': {}},
                                {'burst': 'хочу вернуть деньги за заказ 10502, не подошёл тариф для работы'})
        self.assertEqual(out['reply'], hard_routes.REFUND_TAKEN)

    def test_claude_max_consent_asks_email(self):
        ctx = {'burst': 'да, согласен', 'history': [
            {'role': 'assistant', 'text': 'Заказ #1 вижу: Claude, тариф Max. ... Напишите, пожалуйста, что согласны с этими рисками'}]}
        out = hard_routes.apply({'reply': 'x', 'handoff': False, 'evidence': {}}, ctx)
        self.assertEqual(out['reply'], hard_routes.CLAUDE_EMAIL_ASK)

    def test_repeat_activation_claim_is_the_window(self):
        import support_shop_actions
        self.assertLessEqual(support_shop_actions.COOLDOWN_SECONDS, 2 * 3600)


class OwnerAnswers21NovD(unittest.TestCase):
    def test_wrong_plan(self):
        out = hard_routes.apply({'reply': 'x', 'handoff': False, 'evidence': {}}, {'burst': 'я оплатил не тот тариф'})
        self.assertEqual(out['reply'], hard_routes.WRONG_PLAN_ASK)
        out = hard_routes.apply({'reply': 'x', 'handoff': False, 'evidence': {}},
                                {'burst': 'оплатил не тот тариф, заказ 10502, нужно поменять на Pro'})
        self.assertEqual(out['reply'], hard_routes.WRONG_PLAN_TAKEN)

    def test_chatgpt_already_subscribed_faq(self):
        from support_agent.faq import load_faq
        answer = next(t['answer'] for t in load_faq()['topics'] if t['id'] == 'chatgpt_already_subscribed')
        self.assertIn('дождаться окончания', answer)
        self.assertIn('другой аккаунт', answer)


class Bench10Fixes(unittest.TestCase):
    """bench 10 / live 5 (21.11.2026)."""

    def _apply(self, burst, **evidence):
        return hard_routes.apply({'reply': 'x', 'handoff': False, 'evidence': dict(evidence)}, {'burst': burst})

    def test_phone_number_is_not_a_payment_failure(self):
        self.assertIsNone(self._apply('кодекс просит номер телефона, а российский не принимает')['evidence'].get('hard_routes'))

    def test_qr_not_accepted_is_a_payment_failure(self):
        out = self._apply('QR-код СБП не принимается ни одним банковским приложением')
        self.assertEqual(out['evidence']['hard_routes'], ['payment_failure'])

    def test_joke_about_not_being_scammers_is_not_a_conflict(self):
        out = self._apply('теперь надо доказать банку, что вы не мошенники)))')
        self.assertNotIn('conflict', out['evidence'].get('hard_routes') or [])

    def test_calm_refund_beats_the_model_conflict_verdict(self):
        out = self._apply('верните деньги, подписка не работает уже неделю', conflict_hold=True)
        self.assertEqual(out['reply'], hard_routes.REFUND_ASK)
        self.assertFalse(out['evidence']['conflict_hold'])

    def test_complaint_threat_is_a_conflict(self):
        self.assertEqual(self._apply('верните всё немедленно, я буду жаловаться')['reply'], hard_routes.CONFLICT_REPLY)


class Bench10Tidy(unittest.TestCase):
    def test_repeated_payment_blocks_are_dropped(self):
        from support_agent.playbook import dedupe
        reply = ('Оплата в @neirosetkaa_bot.\n\n1. Откройте бота и нажмите «Каталог».\n2. Оплата: «СБП» или «Картой».\n\n'
                 'Оплатить можно в боте @neirosetkaa_bot: «Каталог» → сервис → тариф, по СБП или картой «Мир».\n\n'
                 'Есть такие варианты:\n• A — 1 ₽\n\nЕсть такие варианты:\n• A — 1 ₽')
        out = dedupe(reply)
        self.assertEqual(out.count('Оплатить можно в боте'), 0)
        self.assertEqual(out.count('Есть такие варианты'), 1)

    def test_single_payment_line_stays(self):
        from support_agent.playbook import dedupe
        text = 'Есть такие варианты:\n• A — 1 ₽\n\nНапишите, какой тариф оформить.\nОплатить можно в боте @neirosetkaa_bot: «Каталог» → сервис → тариф, по СБП или картой «Мир».'
        self.assertEqual(dedupe(text), text)

    def test_relative_clause_is_not_an_outcome(self):
        from support_agent.reply_assertion import asserts_outcome
        self.assertFalse(asserts_outcome('После оплаты пришлите почту, на которую зарегистрирован аккаунт Perplexity.'))
        self.assertTrue(asserts_outcome('Аккаунт уже зарегистрирован на вашу почту.'))

    def test_mcp_setup_is_not_an_app_download(self):
        from support_agent.turn_understanding import _install_reply
        self.assertEqual(_install_reply('помогите поставить mcp сервер в клод десктоп'), '')
        self.assertIn('claude.com/download', _install_reply('Как скачать клод на компьютер?'))

    def test_hidden_data_reply_claims_nothing(self):
        src = open('support_agent/media_batch.py', encoding='utf-8').read()
        self.assertNotIn('данные получил', src)


class OwnerAnswers21NovRound5(unittest.TestCase):
    def test_paid_without_order_asks_order_number(self):
        out = hard_routes.apply({'reply': 'Кнопка «Активировать подписку» в боте…', 'handoff': False, 'evidence': {}},
                                {'burst': 'Здравствуйте, Александр! Я оплатила zoom', 'history': []})
        self.assertEqual(out['reply'], hard_routes.ORDER_FIRST)

    def test_paid_with_known_order_keeps_the_flow(self):
        out = hard_routes.apply({'reply': 'x', 'handoff': False, 'evidence': {}},
                                {'burst': 'я оплатила zoom', 'history': [{'role': 'client', 'text': 'Оплатил заказ #11314'}]})
        self.assertNotIn('paid_without_order', out['evidence'].get('hard_routes') or [])

    def test_question_before_paying_is_not_paid(self):
        out = hard_routes.apply({'reply': 'x', 'handoff': False, 'evidence': {}}, {'burst': 'если оплачу, когда придёт?'})
        self.assertNotIn('paid_without_order', out['evidence'].get('hard_routes') or [])

    def test_simple_question_inside_owner_step_goes_to_the_model(self):
        from support_agent.service import Service
        self.assertTrue(Service._step_question({'burst': 'куда вставить текст?'}))
        self.assertFalse(Service._step_question({'burst': '482913'}))
        self.assertFalse(Service._step_question({'burst': 'как там?'}))
        self.assertFalse(Service._step_question({'burst': 'вот'}, ['photo']))

    def test_verification_answer_has_no_phone_paragraph(self):
        from support_agent.faq import load_faq
        answer = next(t['answer'] for t in load_faq()['topics'] if t['answer'].startswith('Для верификации личности в Claude'))
        self.assertNotIn('Российские номера', answer)


class Bench11PaidTopics(unittest.TestCase):
    """bench 11: the order-first rule must not replace a service-specific owner answer."""

    def _go(self, burst, topic):
        return hard_routes.apply({'reply': 'x', 'handoff': False, 'evidence': {'turn_understanding': {'owner_topic': topic}}},
                                 {'burst': burst, 'history': []})['evidence'].get('hard_routes') or []

    def test_specific_owner_answer_stays(self):
        self.assertNotIn('paid_without_order', self._go('Оплатил Claude Max, что от меня нужно?', 'claude_max_login'))
        self.assertNotIn('paid_without_order', self._go('Оплатил Perplexity Pro, что дальше?', 'perplexity_how'))
        self.assertNotIn('paid_without_order', self._go('Оплатила ChatGPT Plus, помогите активировать', 'activate_for_me'))

    def test_where_is_the_button_keeps_its_answer(self):
        self.assertNotIn('paid_without_order', self._go('оплатил плюс, а где кнопка активировать?', 'none'))

    def test_generic_what_next_asks_the_order(self):
        self.assertIn('paid_without_order', self._go('Здравствуйте! Я оплатила zoom', 'what_next'))


class AndroidAppQuestion(unittest.TestCase):
    """@tatnua_z 21.11.2026: «Мне на андроид можно поставить чат джи пи ти / На ноут поставила»."""
    BURST = 'Александр, добрый вечер\nМне на андроид можно поставить чат джи пи ти\nНа ноут поставила'

    def test_done_device_is_not_the_question(self):
        from support_agent.turn_understanding import _install_reply
        text = _install_reply(self.BURST)
        self.assertIn('chatgpt.ru.uptodown.com/android', text)
        self.assertNotIn('Claude', text)

    def test_owner_app_answer_keeps_only_android_and_the_named_service(self):
        from support_agent.faq import load_faq
        from support_agent.playbook import only_asked_platform
        faq = next(t['answer'] for t in load_faq()['topics'] if t['id'] == 'app_download')
        out = only_asked_platform(faq, self.BURST)
        self.assertNotIn('iPhone', out)
        self.assertNotIn('Apple ID', out)
        self.assertNotIn('claude.ru.uptodown', out)
        self.assertIn('chatgpt.ru.uptodown.com/android', out)

    def test_iphone_question_drops_android_links(self):
        from support_agent.faq import load_faq
        from support_agent.playbook import only_asked_platform
        faq = next(t['answer'] for t in load_faq()['topics'] if t['id'] == 'app_download')
        out = only_asked_platform(faq, 'как скачать чат гпт на айфон?')
        self.assertIn('App Store', out)
        self.assertNotIn('uptodown', out)


class SystemicFit21Nov(unittest.TestCase):
    """«Реши системно» (21.11.2026): the reply fits the question — asked platform/service only, price when asked."""
    T = [{'service_id': 'chatgpt', 'service': 'ChatGPT', 'plan': p, 'price': c}
         for p, c in [('Go', 990), ('Plus', 1990), ('Pro 5×', 9990), ('Pro 20x', 19990), ('Pro50', 48990)]]

    def test_price_question_gets_the_price_not_only_payment_steps(self):
        from support_agent.payment_guide import render
        steps = render('', '', '', {'bot_navigation': {'status': 'available', 'username': 'neirosetkaa_bot', 'catalog_webapp': True}})
        out = hard_routes.apply({'reply': steps, 'handoff': False, 'evidence': {}},
                                {'burst': 'Здравствуйте сколько стоит у вас оформить (оплатить) подписку pro на чат gpt.', 'tariffs': self.T})
        self.assertIn('Pro 5× — 9990 ₽', out['reply'])
        self.assertNotIn('Plus — 1990', out['reply'])
        self.assertNotIn('Перейти к оплате', out['reply'])  # the long generic guide goes; the short steps stay (owner 22.11.2026)
        self.assertIn('1. Откройте бота @neirosetkaa_bot', out['reply'])

    def test_price_and_how_to_pay_keeps_both(self):
        out = hard_routes.apply({'reply': 'Оплата в @neirosetkaa_bot.\n\n1. Откройте бота и нажмите «Каталог».', 'handoff': False, 'evidence': {}},
                                {'burst': 'сколько стоит гпт плюс и как оплатить?', 'tariffs': self.T})
        self.assertIn('1990 ₽', out['reply'])
        self.assertIn('Откройте бота', out['reply'])

    def test_reply_with_a_price_is_left_alone(self):
        out = hard_routes.apply({'reply': 'ChatGPT Plus — 1990 ₽.', 'handoff': False, 'evidence': {}},
                                {'burst': 'сколько стоит гпт плюс', 'tariffs': self.T})
        self.assertEqual(out['reply'], 'ChatGPT Plus — 1990 ₽.')

    def test_unknown_service_payment_steps_promise_nothing_about_activation(self):
        from support_agent.payment_guide import render
        text = render('', '', '', {})
        self.assertNotIn('5-30 минут', text)
        self.assertNotIn('активация идёт в боте', text)

    def test_fit_keeps_list_lines_of_the_asked_service(self):
        from support_agent.fit_answer import fit_to_question
        reply = 'Claude Pro — 2090 ₽.\n\nЕсли это ChatGPT: кнопка в боте.\nЕсли это Claude: Александр вручную.'
        out = fit_to_question(reply, 'не могу активировать клод')
        self.assertIn('Если это Claude', out)
        self.assertNotIn('Если это ChatGPT', out)
        self.assertIn('2090', out)

    def test_fit_keeps_a_plain_heading(self):
        from support_agent.fit_answer import fit_to_question
        reply = 'Для региона США сейчас такие номиналы:\n\n5 USD: 450 ₽'
        self.assertEqual(fit_to_question(reply, 'Хочу пополнить айклауд на регион США'), reply)

    def test_games_answer_keeps_only_the_asked_console(self):
        from support_agent.faq import load_faq
        from support_agent.fit_answer import fit_to_question
        games = next(t['answer'] for t in load_faq()['topics'] if t['id'] == 'games_buy')
        out = fit_to_question(games, 'хочу купить игру на xbox')
        self.assertIn('Xbox', out)
        self.assertNotIn('PlayStation:', out)


class Completeness21Nov(unittest.TestCase):
    """Bench 12 (21.11.2026): «помощник должен больше подсказывать» — the minimum of a consultation."""
    T = ([{'service_id': 'midjourney', 'service': 'Midjourney', 'plan': p, 'price': c} for p, c in [('Basic', 1000), ('Standard', 3000), ('Pro', 5500)]]
         + [{'service_id': 'claude', 'service': 'Claude', 'plan': p, 'price': c} for p, c in [('Pro', 2090), ('Max', 9990), ('Ultra', 19990)]]
         + [{'service_id': 'perplexity', 'service': 'Perplexity', 'plan': 'Pro', 'price': 1990}])

    def _tidy(self, burst, reply):
        from support_agent.playbook import tidy
        return tidy({'reply': reply, 'handoff': False, 'evidence': {}}, {'burst': burst, 'tariffs': self.T})['reply']

    def test_vpn_question_gets_the_owner_recommendations(self):
        self.assertIn('DureVpnBot', self._tidy('VPN / Есть в каталоге ? / Подскажите', 'VPN в каталоге нет — мы его не продаём.'))

    def test_vpn_trouble_is_not_padded(self):
        self.assertNotIn('DureVpnBot', self._tidy('впн не работает', 'Попробуйте сменить страну.'))

    def test_passport_question_names_the_documents(self):
        out = self._tidy('а по российскому паспорту можно верификацию?', 'По внутреннему паспорту не стоит.')
        self.assertIn('загранпаспорт', out)
        self.assertIn('иностранные документы', out)

    def test_renewal_says_where(self):
        out = self._tidy('Хочу продлить подписку на тот же аккаунт', 'Продление — новая покупка.\n\nКакой сервис нужно продлить?')
        self.assertIn('@neirosetkaa_bot', out)
        self.assertTrue(out.rstrip().endswith('?'))  # the client's question stays last

    def test_games_fact_replaces_the_contrary_claim(self):
        out = self._tidy('робуксы есть?', 'Robux в каталоге нет.')
        self.assertIn('Гифт-карты и игры', out)
        self.assertNotIn('в каталоге нет', out)

    def test_buy_wish_gets_prices(self):
        out = self._tidy('мидджорни хочу купить', 'Уточните, пожалуйста: Midjourney Basic, Midjourney Standard или Midjourney Pro?')
        self.assertIn('Midjourney Basic — 1000 ₽', out)
        self.assertNotIn('Уточните', out)

    def test_every_named_service_gets_a_price(self):
        out = self._tidy('нужен клод про и перплексити, сколько за оба выйдет?', 'Claude Pro — 2090 ₽.')
        self.assertIn('Perplexity Pro — 1990 ₽', out)

    def test_what_do_you_need_names_the_after_payment_step(self):
        out = self._tidy('как оформить клод макс, что от меня нужно?', 'Оплата в @neirosetkaa_bot.\n\n1. Откройте бота и нажмите «Каталог».')
        self.assertIn('почт', out)

    def test_handover_is_not_padded(self):
        from support_agent.playbook import tidy
        out = tidy({'reply': 'x', 'handoff': True, 'evidence': {}}, {'burst': 'VPN есть?'})
        self.assertEqual(out['reply'], 'x')

    def test_how_many_days_is_not_a_price_question(self):
        self.assertEqual(self._tidy('сколько дней действует подписка на клод?', 'Месяц.'), 'Месяц.')


class OwnerAnswers21NovTwenty(unittest.TestCase):
    """Owner's 20 answers (21.11.2026, evening)."""
    T = [{'service_id': 'chatgpt', 'service': 'ChatGPT', 'plan': 'Plus', 'price': 1990},
         {'service_id': 'canva', 'service': 'Canva', 'plan': 'Pro', 'price': 990},
         {'service_id': 'canva', 'service': 'Canva', 'plan': 'Pro на год', 'price': 5990}]
    N = [{'region': 'Турция | TR', 'nominal': '25 TRY', 'price': 120}, {'region': 'Турция | TR', 'nominal': '50 TRY', 'price': 230}]

    def _go(self, burst, reply='x'):
        return hard_routes.apply({'reply': reply, 'handoff': False, 'evidence': {}},
                                 {'burst': burst, 'tariffs': self.T, 'nominals': self.N})

    def test_bot_is_always_named(self):
        from support_agent.playbook import tidy
        out = tidy({'reply': 'ChatGPT Plus стоит 1990 ₽. Оформить можно в боте: «Каталог» → ChatGPT → Plus.', 'handoff': False,
                    'evidence': {}}, {'burst': 'Хотела бы подписку оформить, плюс)'})
        self.assertIn('в боте @neirosetkaa_bot', out['reply'])

    def test_yearly_without_a_yearly_plan_is_honest(self):
        out = self._go('можно чат гпт сразу на год?')
        self.assertIn('Годового тарифа на ChatGPT', out['reply'])

    def test_yearly_plan_from_the_catalogue(self):
        self.assertIn('Canva Pro на год — 5990 ₽', self._go('канву на год есть?')['reply'])

    def test_region_present_is_never_denied(self):
        out = self._go('есть пополнение турецкого эпл айди?', 'К сожалению, для Турции пополнения нет.')
        self.assertIn('25 TRY: 120 ₽', out['reply'])
        self.assertNotIn('нет', out['reply'].split('\n')[0])

    def test_company_documents_go_to_the_owner(self):
        out = self._go('нужен счёт для ИП')
        self.assertTrue(out['evidence']['owner_alert'])
        self.assertIn('Александр', out['reply'])

    def test_friendly_owner_texts(self):
        from support_agent.faq import load_faq
        faq = {t['id']: t['answer'] for t in load_faq()['topics']}
        self.assertIn('восстановит', faq['subscription_ended_early'])
        self.assertIn('Другой сервис', faq['off_catalog'])
        self.assertIn('реферальной ссылкой', faq['for_other_person'])
        self.assertIn('после оплаты', faq['gemini_google'].casefold())
        self.assertTrue(faq['gemini_google'].index('Каталог') < faq['gemini_google'].index('пароль'))
        self.assertIn('Plus', faq['chatgpt_which'])
        self.assertIn('Автосписаний у нас нет', faq['autopay_cancel'])
        self.assertIn('при первой возможности', faq['delivery_time'])


class Live6Fixes(unittest.TestCase):
    """live 6 (22.11.2026): the five bad auto-sends."""
    T = [{'service_id': 'zoom', 'service': 'Zoom', 'plan': 'Pro', 'price': 1500},
         {'service_id': 'chatgpt', 'service': 'ChatGPT', 'plan': 'Go', 'price': 990}]

    def _go(self, burst):
        return hard_routes.apply({'reply': 'x', 'handoff': False, 'evidence': {'turn_understanding': {'owner_topic': 'what_next'}}},
                                 {'burst': burst, 'tariffs': self.T})

    def test_notice_never_claims_a_check(self):
        from support_agent.paid_notice import compose
        kind, text = compose({'order': '11332', 'service': 'ChatGPT', 'plan': 'Plus'}, [])
        self.assertNotIn('вижу', text)

    def test_zoom_paid_gets_the_zoom_step(self):
        out = self._go('Здравствуйте, Александр!\nЯ оплатила zoom\n[клиент прислал скриншот]')
        self.assertIn('почту и пароль от аккаунта Zoom', out['reply'])

    def test_a_wish_is_not_a_payment(self):
        out = self._go('Что нужно, чтобы оплатить ГПТ? хочется всё же сейчас оплаченный текущий профиль')
        self.assertNotIn('paid_without_order', out['evidence'].get('hard_routes') or [])

    def test_a_problem_is_not_a_purchase(self):
        out = self._go('Я оформила подписку на чат Go, но не отражается. Здесь просит оформить подписку')
        self.assertNotIn('price_without_price', out['evidence'].get('hard_routes') or [])

    def test_success_inside_owner_step_is_a_happy_end(self):
        from support_agent.service import Service
        out = Service._owner_step_ack(None, {'burst': 'Нет ничего\nЕсть подписка👌'})
        self.assertIn('получилось', out['reply'])
        self.assertFalse(out['evidence']['owner_alert'])

    def test_discount_gets_both_codes(self):
        from support_agent.playbook import tidy
        out = tidy({'reply': 'Подскажите, о каком сервисе речь?', 'handoff': False, 'evidence': {}}, {'burst': 'Скидку сделаете?'})
        self.assertIn('NAVSEX', out['reply'])
        self.assertIn('@neirosetkaa_bot', out['reply'])

    def test_grok_price_never_offers_supergrok(self):
        rows = [{'service_id': 'supergrok', 'service': 'SuperGrok', 'plan': 'SuperGrok', 'price': 3090}]
        out = hard_routes.apply({'reply': 'Cursor Plus — 990 ₽', 'handoff': False, 'evidence': {}}, {'burst': 'грок сколько стоит?', 'tariffs': rows})
        self.assertNotIn('SuperGrok', out['reply'])


class FreshSet22Nov(unittest.TestCase):
    def test_english_client_gets_no_russian_template(self):
        from support_agent.playbook import tidy
        rows = [{'service_id': 'chatgpt', 'service': 'ChatGPT', 'plan': 'Plus', 'price': 1990}]
        out = tidy({'reply': 'ChatGPT Plus costs 1990 RUB, pay in @neirosetkaa_bot.', 'handoff': False, 'evidence': {}},
                   {'burst': 'Hi! How much is ChatGPT Plus? Any discount?', 'tariffs': rows})
        self.assertNotRegex(out['reply'], r'[а-яё]')

    def test_ooo_documents_go_to_the_owner(self):
        out = hard_routes.apply({'reply': 'x', 'handoff': False, 'evidence': {}}, {'burst': 'нужны закрывающие документы для ООО'})
        self.assertIn('invoice_legal', out['evidence']['hard_routes'])

    def test_fresh_cases_are_valid(self):
        import re
        import yaml
        cases = yaml.safe_load(open('support_agent/data/benchmark.yaml', encoding='utf-8'))['cases']
        fresh = [c for c in cases if c['id'].startswith('fresh-')]
        self.assertGreaterEqual(len(fresh), 60)
        for case in fresh:
            for pattern in (case.get('need') or []) + (case.get('forbid') or []):
                re.compile('(?im)' + pattern)


class Bench14Fixes(unittest.TestCase):
    T = [{'service_id': 'suno_ai', 'service': 'Suno AI', 'plan': 'Pro', 'price': 990},
         {'service_id': 'kimi', 'service': 'Kimi', 'plan': 'Plus', 'price': 1990}]

    def _go(self, burst, **ev):
        ev.setdefault('turn_understanding', {'owner_topic': 'what_next'})
        return hard_routes.apply({'reply': 'x', 'handoff': False, 'evidence': ev}, {'burst': burst, 'tariffs': self.T})

    def test_catalogue_name_with_suffix_is_found(self):
        self.assertIn('почту и пароль от аккаунта Suno', self._go('оплатил суно, что дальше?')['reply'])

    def test_no_payment_link_gets_email_and_password(self):
        out = self._go('кими оплатил, а ссылку на оплату не могу достать')
        self.assertIn('почту и пароль от аккаунта Kimi', out['reply'])

    def test_receipt_without_order_goes_to_the_owner(self):
        out = self._go('отправил чек, а в боте заказа нет')
        self.assertEqual(out['reply'], hard_routes.RECEIPT_TAKEN)
        self.assertTrue(out['evidence']['owner_alert'])

    def test_old_owner_line_about_licences_is_not_a_clash(self):
        out = hard_routes.apply({'reply': 'Из российских документов подходит только загранпаспорт РФ.', 'handoff': True,
                                 'evidence': {'owner_alignment_hold': True}},
                                {'burst': 'а по паспорту можно?', 'history': [{'role': 'owner', 'text': 'можно по водительским правам'}]})
        self.assertFalse(out['handoff'])
        self.assertFalse(out['evidence']['owner_alignment_hold'])

    def test_checks_never_spend_the_clients_limit(self):
        for path in ('support_agent/bench.py', 'support_agent/dialog_check.py'):
            self.assertNotIn('service.charge_call()', open(path, encoding='utf-8').read())

    def test_english_price_names_the_bot(self):
        from support_agent.playbook import tidy
        out = tidy({'reply': 'Hi! ChatGPT Plus is 1,990 ₽.', 'handoff': False, 'evidence': {}}, {'burst': 'Hi! How much is ChatGPT Plus?'})
        self.assertIn('@neirosetkaa_bot', out['reply'])


class OwnerAnswers22Nov(unittest.TestCase):
    def _go(self, burst, tariffs=()):
        return hard_routes.apply({'reply': 'x', 'handoff': False, 'evidence': {}}, {'burst': burst, 'tariffs': list(tariffs)})

    def test_fkwallet(self):
        self.assertEqual(self._go('просит скачать какой-то FKwallet')['reply'], hard_routes.FKWALLET_REPLY)

    def test_claude_blocked_money(self):
        out = self._go('клод заблокировал аккаунт, верните деньги')
        self.assertEqual(out['reply'], hard_routes.CLAUDE_BLOCK_REPLY)
        self.assertTrue(out['evidence']['owner_alert'])

    def test_team_plan_from_catalogue_or_owner(self):
        rows = [{'service_id': 'chatgpt', 'service': 'ChatGPT', 'plan': 'Business', 'price': 3990}]
        self.assertIn('Business — 3990 ₽', self._go('нужен чатгпт на команду из 5 человек', rows)['reply'])
        self.assertIn('Александр', self._go('нужен чатгпт на команду из 5 человек')['reply'])

    def test_where_is_my_order(self):
        self.assertIn('номер заказа', self._go('оплатил час назад, где мой заказ?')['reply'])
        self.assertNotIn('where_is_my_order', self._go('Заказ #12001 где мой заказ')['evidence'].get('hard_routes') or [])

    def test_kazakh_and_ukrainian_get_no_russian_templates(self):
        self.assertTrue(hard_routes.latin_only('Сәлеметсіз бе, ChatGPT Plus қанша тұрады?'))
        self.assertTrue(hard_routes.latin_only('Скільки коштує ChatGPT Plus?'))
        self.assertFalse(hard_routes.latin_only('Сколько стоит?'))

    def test_owner_texts(self):
        from support_agent.faq import load_faq
        faq = {t['id']: t['answer'] for t in load_faq()['topics']}
        self.assertIn('не работает', faq['sora'])
        self.assertIn('нельзя', faq['transfer'])
        self.assertIn('целиком', faq['installments'])
        self.assertIn('в любое время', faq['working_hours'])


class Bench15Fixes(unittest.TestCase):
    def test_generic_what_next_does_not_block_a_known_service_step(self):
        rows = [{'service_id': 'suno', 'service': 'Suno', 'plan': 'Pro', 'price': 990}]
        what_next = 'Если вы уже оплатили… напишите, какой сервис оформляете, и пришлите номер заказа — подскажу следующий шаг.'
        out = hard_routes.apply({'reply': what_next, 'handoff': False, 'evidence': {'turn_understanding': {'owner_topic': 'what_next'}}},
                                {'burst': 'оплатил суно, что дальше?', 'tariffs': rows})
        self.assertIn('почту и пароль от аккаунта Suno', out['reply'])

    def test_unknown_service_gets_other_service(self):
        from support_agent.payment_guide import render
        guide = render('', '', '', {'bot_navigation': {'status': 'available', 'username': 'neirosetkaa_bot', 'catalog_webapp': True}})
        out = hard_routes.apply({'reply': guide, 'handoff': False, 'evidence': {}}, {'burst': 'нетфликс можете оплатить?', 'tariffs': []})
        self.assertIn('Другой сервис', out['reply'])
        same = hard_routes.apply({'reply': guide, 'handoff': False, 'evidence': {}}, {'burst': 'как оплатить подписку?', 'tariffs': []})
        self.assertEqual(same['reply'], guide)

    def test_slang_and_bare_plus(self):
        from support_agent.fit_answer import _asked, _services
        self.assertIn('ChatGPT', _asked('брат нужна гпшка на месяц', _services()))
        rows = [{'service_id': 'chatgpt', 'service': 'ChatGPT', 'plan': 'Plus', 'price': 1990}]
        self.assertEqual(hard_routes._asked_rows('сколько плюс стоит?', {'tariffs': rows}), rows)


class Bench17Fixes(unittest.TestCase):
    T = ([{'service_id': 'chatgpt', 'service': 'ChatGPT', 'plan': 'Plus', 'price': 1990},
          {'service_id': 'chatgpt_credits', 'service': 'пополнение кредитов ChatGPT', 'plan': '250 кредитов (10$)', 'price': 1090}]
         + [{'service_id': 'google_gemini', 'service': 'Gemini', 'plan': p, 'price': c}
            for p, c in [('Pro', 2090), ('Pro на год', 19989), ('Ultra', 24990), ('Ultra на год', 199990)]])

    def _tidy(self, burst, reply):
        from support_agent.playbook import tidy
        return tidy({'reply': reply, 'handoff': False, 'evidence': {}}, {'burst': burst, 'tariffs': self.T})['reply']

    def test_no_credit_top_ups_unless_asked(self):
        out = self._tidy('брат нужна гпшка на месяц, че по цене', 'На месяц есть Plus за 1 990 ₽.')
        self.assertNotIn('кредитов', out)
        self.assertIn('@neirosetkaa_bot', out)

    def test_gemini_after_payment_names_the_email(self):
        out = self._tidy('джемини про можно на мой гугл акк оформить?',
                         'Да, можем.\n\nОплатить можно в боте @neirosetkaa_bot. После оплаты подскажу, что понадобится для активации.')
        self.assertIn('почту и пароль', out)


class ForwardedBotNoticeAndGreeting(unittest.TestCase):
    """Owner 24.11.2026: ElevenLabs reply greeted twice and stacked the catalog onto a forwarded «Оплата прошла»."""

    def test_forwarded_notice_is_not_a_purchase(self):
        from support_agent import hard_routes
        burst = ('Да в принципе прикинул - креатор подойдет\n🎉 Оплата прошла!\n\n📦 ElevenLabs - Creator\n\n'
                 'Чтобы оформить подписку, получи ссылку на оплату по инструкции и пришли её сюда\nу меня был тариф\nДобрый вечер!')
        self.assertTrue(hard_routes.BOT_NOTICE.search(burst))

    def test_one_greeting_per_message(self):
        from support_agent.conversation_style import decorate_reply
        out = decorate_reply('Есть такие варианты:\n• ElevenLabs Creator - 2290 ₽\n\nДобрый вечер! Creator вам подходит.',
                             {'current_client_greeting': True}, introduced=False)
        self.assertEqual(out.count('Добрый вечер'), 0)
        self.assertIn('\n\nCreator вам подходит.', out)
        self.assertTrue(out.startswith('Здравствуйте!'))


class NightReview24Nov(unittest.TestCase):
    """Night review 24.11.2026: systemic fixes found in the day's answers."""

    def test_interjection_is_not_a_company(self):
        from support_agent import hard_routes
        self.assertIsNone(hard_routes._INVOICE.search('Ооо, круть\nБлагодарю 🙏'))
        self.assertTrue(hard_routes._INVOICE.search('нужен счёт на ООО'))

    def test_balance_question_is_not_a_price_question(self):
        from support_agent import hard_routes
        burst = 'Напиши пожалуйста, как можно тратить кредиты и монетки?'
        self.assertFalse(hard_routes._price_detect(burst, {'tariffs': []}, {'reply': 'Есть варианты'}))

    def test_call_answer_only_when_call_asked(self):
        from support_agent import hard_routes
        answer = {'reply': 'Созвониться, к сожалению, не получится - можем текстом или голосовыми сообщениями.'}
        self.assertTrue(hard_routes._call_detect('Ждем живого Александра:-)', {}, answer))
        self.assertFalse(hard_routes._call_detect('а можно созвониться?', {}, answer))
        out = hard_routes._call_act(answer, 'Ждем живого Александра:-)', {})
        self.assertIn('Передал Александру', out['reply'])
