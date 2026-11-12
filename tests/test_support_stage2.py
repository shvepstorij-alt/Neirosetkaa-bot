"""Stage-2 behaviour from the agreed specification."""
import datetime as dt
import unittest

from support_agent.activation_guidance import reply as activation_reply
from support_agent.conversation_style import INTRO, address_mode, decorate_reply
from support_agent.external_quote import estimate
from support_agent.handoff import handoff_text, outside_working_hours
from support_agent import owner_scenarios
from support_agent.owner_scenarios import reply as scenario
from support_agent.rollout import auto_recipients
from support_agent.storage import Store
from support_agent.app_installation import reply as install_reply
from install_guides import DESKTOP_PLAIN, load_guides, setting_key


class GreetingTests(unittest.TestCase):
    def test_first_contact_then_not_again(self):
        first = decorate_reply('Тариф Plus стоит 1990 ₽.', {'current_client_greeting': True}, introduced=False)
        self.assertTrue(first.startswith(INTRO))
        self.assertIn('1990', first)
        self.assertEqual(first.count('Здравствуйте'), 1)
        later = decorate_reply(first, introduced=True)
        plain = decorate_reply('Тариф Plus стоит 1990 ₽.', introduced=False)
        self.assertTrue(plain.startswith('Я ИИ-помощник Александра'))
        self.assertFalse(later.startswith('Здравствуйте'))
        self.assertIn('1990', later)

    def test_introduction_survives_restart(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'state.sqlite3')
            store = Store(path, batch_seconds=0)
            store.connection('biz', True, True)
            cid = store.ingest('biz', 7, 1, 'client', 'сколько стоит', {'kind': 'none'}, 1)
            self.assertFalse(store.introduced(cid))
            store.mark_introduced(cid)
            store.db.close()
            reopened = Store(path, batch_seconds=0)
            self.assertTrue(reopened.introduced(cid))
            reopened.db.close()

    def test_address_mirrors_client(self):
        self.assertEqual(address_mode('подскажите, пожалуйста, как вам оплатить'), 'вы')
        self.assertEqual(address_mode('скажи мне как тебе оплатить'), 'ты')


class HandoffTests(unittest.TestCase):
    def test_hours_and_phrase(self):
        zone = dt.timezone(dt.timedelta(hours=3))
        day = dt.datetime(2026, 11, 7, 15, 0, tzinfo=zone)
        night = dt.datetime(2026, 11, 7, 23, 30, tzinfo=zone)
        edge = dt.datetime(2026, 11, 7, 22, 0, tzinfo=zone)
        self.assertFalse(outside_working_hours(day))
        self.assertTrue(outside_working_hours(night))
        self.assertTrue(outside_working_hours(edge))
        self.assertEqual(handoff_text(day).split('.')[0], 'Александр будет на связи в течение получаса-часа')
        self.assertIn('другими вопросами', handoff_text(day))
        self.assertNotIn('задержк', handoff_text(day))
        self.assertIn('задержк', handoff_text(night))
        asked = scenario('позовите человека', now=day)
        self.assertIn('получаса-часа', asked['reply'])
        self.assertIn('другими вопросами', asked['reply'])


class PriceTests(unittest.TestCase):
    def test_owner_formula(self):
        self.assertEqual(estimate('25'), 2490)
        self.assertEqual(estimate('40'), 3990)
        self.assertEqual(estimate('100'), 9990)
        self.assertEqual(estimate('22.50'), 2290)
        self.assertEqual(estimate('22.90'), 2290)

    def test_promo_only_on_request(self):
        asked = scenario('есть промокод или скидка?')
        self.assertIn('NAVSEX', asked['reply'])
        self.assertIn('10%', asked['reply'])
        self.assertIn('Других скидок нет', asked['reply'])
        self.assertIsNone(scenario('сколько стоит подписка'))


class ScenarioTests(unittest.TestCase):
    def test_refund_coins_giveaway_generation(self):
        refund = scenario('хочу возврат')
        self.assertIn('FreeKassa', refund['reply'])
        self.assertIn('не обещаю', refund['reply'])
        self.assertNotIn('верну деньги', refund['reply'].casefold())
        coins = scenario('как потратить монетки')
        self.assertIn('только за приглашённых друзей', coins['reply'])
        self.assertIn('10%', coins['reply'])
        self.assertIn('оплату подписки', coins['reply'])
        self.assertIn('Других способов', coins['reply'])
        self.assertEqual(scenario('есть розыгрыш?')['reply'], owner_scenarios.GIVEAWAY)
        generated = scenario('как сгенерировать видео в боте')
        self.assertIn('Видео', generated['reply'])
        self.assertIn('Анимировать', generated['reply'])


class ActivationTests(unittest.TestCase):
    def test_expired_token_and_routes(self):
        facts = {'catalog_selection': {'mentioned_known_services': ['chatgpt']}, 'requested_catalog': []}
        expired = activation_reply(facts, 'истёк токен chatgpt')
        self.assertIn('выйдите', expired['reply'].casefold())
        self.assertIn('том же браузере', expired['reply'])
        self.assertIn('Opera', expired['reply'])
        self.assertIn('2–5 минут', expired['reply'])
        paste = activation_reply(facts, 'не вставляется сессия')
        self.assertIn('Ctrl+V', paste['reply'])
        new_account = activation_reply(facts, 'новый аккаунт plus')
        self.assertIn('эту переписку', new_account['reply'])
        self.assertNotIn('окно активации в боте, не в эту', new_account['reply'])
        overlay = activation_reply(facts, 'можно активировать поверх')
        self.assertIn('сгорит', overlay['reply'])


class InstallTests(unittest.TestCase):
    def test_desktop_text_and_legacy_guides(self):
        seen = {}

        async def get_setting(key, default=''):
            seen[key] = default
            return ''

        import asyncio
        guides = asyncio.run(load_guides(get_setting))
        self.assertIn('Android', guides['android'])
        self.assertIn('iPhone', guides['iphone'])
        self.assertIn('17763.0', guides['desktop'])
        self.assertIn('Microsoft Store', guides['desktop'])
        self.assertIn('Apple Silicon', guides['desktop'])
        self.assertEqual(setting_key('gpt_install_android'), 'gpt_install_android_text')
        self.assertEqual(setting_key('gpt_install_iphone'), 'gpt_install_iphone_text')
        self.assertEqual(setting_key('gpt_install_desktop'), 'gpt_install_desktop_text')
        answer = install_reply({'catalog': []}, 'как установить chatgpt на компьютер')
        self.assertEqual(answer['reply'], DESKTOP_PLAIN)


class RolloutTests(unittest.TestCase):
    def test_group_then_everyone(self):
        ids, everyone = auto_recipients('8939620159', '', '', allowed={8939620159}, owner=1)
        self.assertEqual(ids, frozenset({8939620159}))
        self.assertFalse(everyone)
        ids, everyone = auto_recipients('', '8939620159', 'yes', allowed={8939620159, 2}, owner=1)
        self.assertTrue(everyone)
        self.assertEqual(ids, frozenset({8939620159}))
        with self.assertRaises(ValueError):
            auto_recipients('1', '', '', allowed={8939620159}, owner=1)


if __name__ == '__main__':
    unittest.main()
