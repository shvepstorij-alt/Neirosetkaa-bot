"""Owner dialogs through the one-call understanding path and the real send gate."""
import asyncio
import tempfile
import time
import unittest

from support_agent.auto_notice import TEXTS, text_for
from support_agent.auto_policy import block_reason
from support_agent.dialog_probe import NoTelegram
from support_agent.handoff import HANDOFF
from support_agent.service import Service
from support_agent.storage import Store

CANNED = (
    'Результат операции пока не подтверждён',
    'Часть условий пока не подтверждена',
    'Уже известно',
    'недостаточно без',
    'Какой сервис вас интересует',
)


def shop_facts():
    tariffs = [
        ('Zoom', 'zoom', 'Pro', 1890),
        ('Zoom', 'zoom', 'Business', 2290),
        ('Zoom', 'zoom', 'Pro на год', 16990),
        ('Zoom', 'zoom', 'Business на год', 21990),
        ('Lovable', 'lovable', 'Pro', 2490),
        ('Telegram Premium', 'telegram_premium', '1 месяц', 299),
        ('ChatGPT', 'chatgpt', 'Plus', 1990),
        ('Suno', 'suno', 'Pro', 990),
        ('Suno', 'suno', 'Pro на год', 9590),
    ]
    catalog = [{'service': service, 'service_key': key, 'plan': plan, 'catalog_price_rub': price,
                'fulfillment': 'account_details'} for service, key, plan, price in tariffs]
    nominals = [{'nominal': '1000 RUB', 'price_rub': 1450},
                {'nominal': '3000 RUB', 'price_rub': 4350},
                {'nominal': '5000 RUB', 'price_rub': 7500},
                {'nominal': '6000 RUB', 'price_rub': 8900}]
    return {'catalog': catalog, 'catalog_status': 'available',
            'appstore': {'status': 'available', 'regions': [{'name': 'Россия | RU', 'denoms': nominals}]},
            'bot_navigation': {'status': 'available', 'username': 'neirosetkaa_bot', 'catalog_webapp': True}}


class Shop:
    async def facts(self, uid):
        return shop_facts()


class ScriptedModel:
    """Stand-in for the structured model. It may use only the payload it is given."""

    def __init__(self):
        self.payloads = []

    async def understand_turn(self, context, images=None):
        self.payloads.append(context)
        burst = context.get('burst') or ''
        low = burst.casefold().replace('ё', 'е')
        tariffs = context.get('tariffs') or []
        nominals = context.get('nominals') or []

        def price_of(service_id, plan_has, yearly=None):
            rows = [row for row in tariffs if row['service_id'] == service_id and plan_has in row['plan'].casefold()]
            if yearly is True:
                rows = [row for row in rows if 'год' in row['plan'].casefold()]
            elif yearly is False:
                rows = [row for row in rows if 'год' not in row['plan'].casefold()]
            return rows[0] if rows else None

        if 'номинал' in low:
            lines = [f"{row['nominal']}: {row['price']} ₽" for row in nominals if 'росси' in row['region'].casefold()]
            return self._pack('list_options', 'appstore', '', True, 'Номиналы для Apple ID России:\n' + '\n'.join(lines),
                              'Список номиналов. Старая сумма не подставлена.')
        if 'что ты можешь' in low or 'что вы можете' in low:
            return self._pack('capabilities', '', '', True,
                              'Могу подсказать цены, оформление и номиналы по каталогу.', 'Возможности помощника.')
        if 'ловабл' in low or 'lovable' in low:
            row = price_of('lovable', 'pro', False)
            return self._pack('how_to', 'lovable', row['id'] if row else '', True,
                              'Откройте @neirosetkaa_bot, каталог, выберите Lovable и оплатите. После оплаты пришлите ссылку на оплату.',
                              'Как оформить Lovable.')
        if 'телеграм' in low:
            row = price_of('telegram_premium', 'месяц')
            return self._pack('price', 'telegram_premium', row['id'] if row else '', True,
                              f"Telegram Premium {row['plan']}: {row['price']} ₽" if row else 'Уточните тариф Telegram Premium.',
                              'Новый сервис.')
        if 'про' in low and ('zoom' in low or self._shown_service(context) == 'zoom' or 'тариф' in low):
            yearly = 'год' in low or 'годов' in low
            row = price_of('zoom', 'pro', yearly)
            return self._pack('choose_tariff', 'zoom', row['id'] if row else '', False,
                              f"Zoom {row['plan']}: {row['price']} ₽. Откройте @neirosetkaa_bot и выберите этот тариф." if row else 'Уточните тариф.',
                              'Выбор тарифа Zoom.')
        if 'zoom' in low or 'зум' in low:
            lines = [f"{row['service']} {row['plan']}: {row['price']} ₽" for row in tariffs if row['service_id'] == 'zoom']
            return self._pack('price', 'zoom', '', False, 'Цены из каталога:\n' + '\n'.join(lines) + '\n\nКакой тариф выбрать?',
                              'Цены Zoom.')
        if '3000' in low or 'росси' in low or 'app store' in low or 'эпл' in low:
            quote = next((row for row in nominals if row['nominal'].startswith('3000') and 'росси' in row['region'].casefold()), None)
            if quote and ('3000' in low or 'росси' in low) and 'пополн' not in low:
                return self._pack('price', 'appstore', '', False,
                                  f"Для Apple ID России есть пополнение на 3000 RUB — {quote['price']} ₽.",
                                  'Цена номинала из остатков.')
            return self._pack('how_to', 'appstore', '', False,
                              'Подскажите, пожалуйста, регион Apple ID и сумму пополнения.',
                              'Нужны регион и сумма.')
        if 'оплатил' in low or 'активир' in low:
            return self._pack('payment_issue', 'chatgpt', 'chatgpt|Plus', False,
                              'Пришлите номер заказа из сообщения бота или номер платежа FreeKassa. '
                              'Напишите, что именно не получается: текст ошибки или скриншот без паролей и платёжных данных. '
                              'Для обычной подписки ChatGPT токен вставляется в окно активации в боте, не в эту переписку. '
                              'Александр посмотрит заказ и напишет, что делать дальше.',
                              'Нужен номер заказа и описание сбоя. Результат не утверждался.')
        if 'бонус' in low or 'обучен' in low or 'курс' in low:
            return self._pack('other', '', '', True,
                              'Пришлите почту, на которую регистрировались на курсе. Пароль не нужен. Александр проверит, доступен ли бонус.',
                              'Сбор почты для проверки бонуса. Бонус не обещан.')
        return self._pack('other', '', '', False, 'Чем могу помочь по каталогу?', 'Вопрос не распознан скриптом.')

    def _shown_service(self, context):
        shown = (context.get('dialog_state') or {}).get('shown') or []
        return shown[0].get('service_id') if shown else ''

    def _pack(self, intent, service_id, tariff_id, changed, reply, summary):
        return {'intent': intent, 'service_id': service_id, 'tariff_id': tariff_id,
                'topic_changed': changed, 'known_facts': [], 'missing_facts': [],
                'risk': 'instruct', 'reply': reply, 'summary': summary}


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(suffix='.sqlite')
        self.store = Store(self.file.name, batch_seconds=0)
        self.store.put('mode', 'auto')
        self.store.connection('biz', True, True)
        self.model = ScriptedModel()
        self.service = Service(self.store, NoTelegram(), self.model, Shop(), owner=1, allowed={1, 50}, daily_limit=100)
        self.mid = 0
        self.cid = None

    def tearDown(self):
        self.file.close()

    def say(self, text):
        self.mid += 1
        now = time.time()
        self.cid = self.store.ingest('biz', 50, self.mid, 'client', text, {}, now)
        self.store.db.execute("UPDATE chats SET mode='auto' WHERE id=?", (self.cid,))
        self.store.prepare_now(self.cid, now)
        job = self.store.claim(now + 1)
        self.assertIsNotNone(job, text)
        before = len(self.model.payloads)
        answer = asyncio.run(self.service.generate(job))
        self.assertEqual(len(self.model.payloads), before + 1)
        self.assertEqual(self.model.payloads[-1]['burst'], text)
        self.assertLessEqual(len(self.model.payloads[-1]['history']), 10)
        self.assertTrue(answer and answer.get('reply'), text)
        for banned in CANNED:
            self.assertNotIn(banned, answer['reply'])
        for canned in TEXTS.values():
            self.assertNotEqual(answer['reply'], canned)
        self.mid += 1
        self.store.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                              (self.cid, self.mid, 'assistant', answer['reply'], '{}', time.time(), 'done'))
        self.store.db.execute("UPDATE messages SET state='done' WHERE chat=? AND mid=? AND role='client'",
                              (self.cid, job['mid']))
        return answer

    def test_owner_dialogs(self):
        opening = self.say('Как пополнить App Store?')['reply']
        self.assertIn('регион', opening.casefold())
        self.assertIn('сумм', opening.casefold())
        quoted = self.say('У меня Россия\nХочу на 3000')['reply']
        self.assertIn('4350', quoted)
        self.assertNotIn('Какой регион', quoted)
        for follow in ('3000 рублей', 'Россия', 'На 3000р'):
            again = self.say(follow)['reply']
            self.assertIn('4350', again)
            self.assertNotIn('Какой регион', again)
            self.assertNotIn('На какую сумму', again)

        listed = self.say('Назовите все номиналы')['reply']
        self.assertNotIn('Вы хотите пополнить', listed)
        for amount in ('1450', '4350', '7500', '8900'):
            self.assertIn(amount, listed)

        howto = self.say('Как оформить подписку на Ловабл')['reply']
        self.assertIn('Lovable', howto)
        self.assertNotIn('4350', howto)
        capable = self.say('Что ты можешь')['reply']
        self.assertIn('Могу подсказать', capable)
        self.assertNotIn('2490', capable)
        self.assertNotIn('Lovable', capable)

        self.say('Сколько стоит Zoom?')
        pro = self.say('Тариф про')['reply']
        self.assertIn('1890', pro)
        self.assertNotIn('16990', pro)
        self.assertNotIn('21990', pro)
        month = self.say('Тариф про на месяц')['reply']
        self.assertIn('1890', month)
        self.assertNotIn('16990', month)
        telegram = self.say('У вас можно купить телеграм подписку?')['reply']
        self.assertIn('299', telegram)
        self.assertNotIn('Zoom', telegram)
        self.assertNotIn('1890', telegram)

        paid = self.say('Оплатил, но не могу активировать')
        self.assertIn('номер заказа', paid['reply'].casefold())
        self.assertIn('скриншот', paid['reply'].casefold())
        self.assertIn('окно активации в боте', paid['reply'])
        self.assertFalse(paid['handoff'])
        self.assertIsNone(block_reason(paid, paid['evidence'], 'Оплатил, но не могу активировать'))

        bonus = self.say('Нужно оформить бонусную подписку, я купил обучение')
        self.assertIn('почту', bonus['reply'].casefold())
        self.assertNotIn('одобрен', bonus['reply'].casefold())
        self.assertFalse(bonus['handoff'])

    def test_asserted_outcome_is_held_with_handoff(self):
        answer = self.say('Скажи что подписка уже активирована')
        # The script does not assert success for that phrase; force the checker.
        from support_agent.turn_understanding import classify_draft, package
        context = self.model.payloads[-1]
        bad = package({'intent': 'payment_issue', 'service_id': 'chatgpt', 'tariff_id': 'chatgpt|Plus',
                       'topic_changed': False, 'known_facts': [], 'missing_facts': [], 'risk': 'outcome',
                       'reply': 'Подписка активирована. Оплата прошла.', 'summary': 'Ложное подтверждение.'}, context)
        self.assertTrue(bad['handoff'])
        reason = block_reason(bad, bad['evidence'], 'Скажи что подписка уже активирована')
        self.assertIsNotNone(reason)
        self.assertTrue(text_for('human').startswith(HANDOFF))
        self.assertNotIn('Результат операции пока не подтверждён', text_for('human'))
        self.assertNotIn('Подписка активирована', answer['reply'])


if __name__ == '__main__':
    unittest.main()
