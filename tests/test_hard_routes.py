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
        self.assertTrue(out['reply'].startswith('Заказ #12345 вижу.'), out['reply'])
        self.assertNotIn('номер заказа', out['reply'].lower())
        self.assertNotIn('номера заказа', out['reply'].lower())
        self.assertIn('Пришлите, пожалуйста, скриншот', out['reply'])
        self.assertIn('Активировать подписку', out['reply'])

    def test_order_and_freekassa(self):
        burst = 'Заказ #9974 freekassa 346894753 / оплатил, есть ли смысл активировать'
        out = run(burst, 'Напишите, пожалуйста, номер заказа и уточните, какая подписка.')
        self.assertIn('Заказ #9974 вижу.', out['reply'])
        self.assertNotIn('номер заказа', out['reply'])
        self.assertIn('Уточните, пожалуйста, какая подписка.', out['reply'])

    def test_freekassa_only(self):
        out = run('freekassa 346894753 не пришло', 'Пришлите номер заказа и скриншот экрана, Александр проверит.')
        self.assertEqual(out['reply'], 'Номер FreeKassa 346894753 вижу. Пришлите скриншот экрана, Александр проверит.')

    def test_from_history_tail(self):
        history = [{'role': 'client', 'text': 'номер заказа 55123', 'at': 1}]
        out = run('не активируется', 'Здравствуйте! Подскажите, пожалуйста, номер заказа.', history=history)
        self.assertEqual(out['reply'], 'Здравствуйте! Заказ #55123 вижу.')

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
