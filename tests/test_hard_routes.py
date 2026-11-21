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
