"""Owner 21.11.2026: any order reference → check the client's own order → act on its real state."""
import asyncio
import unittest

from support_agent import order_action


class FakeShop:
    def __init__(self, row=None, status='available', resend=None):
        self.row, self.status, self.resend_result, self.resent = row, status, resend, []

    async def order_facts(self, uid, ref):
        return {'orders_status': self.status, 'orders': [self.row] if self.row else []}

    async def resend_activation(self, uid, order):
        self.resent.append((uid, order))
        return self.resend_result or {'ok': True, 'error': None, 'msg': 'ok'}


class Svc:
    def __init__(self, shop):
        self.shop = shop


def run(shop, number='9974'):
    return asyncio.run(order_action.decide(Svc(shop), {'uid': 50, 'chat': 1}, number))


class OrderActionTests(unittest.TestCase):
    def test_reference_in_own_words(self):
        text = 'оплатил подписку gpt plus ещё 9 ноября, но сразу не активировал. Есть ли смысл?\nЗаказ #9974\nfreekassa 346894753'
        self.assertEqual(order_action.find_reference(text), '9974')
        self.assertEqual(order_action.find_reference('freekassa 346894753 оплатил'), '346894753')
        self.assertEqual(order_action.find_reference('у меня 2 вопроса #123'), '')  # no payment context

    def test_expired_window_reactivates(self):
        shop = FakeShop({'num': 9974, 'status': 'paid', 'service_key': 'chatgpt', 'plan_name': 'Plus', 'activation_status': 'not_checked'})
        answer = run(shop)
        self.assertEqual(shop.resent, [(50, '9974')])
        self.assertIn('повторную активацию', answer['reply'])
        self.assertIn('Активировать подписку', answer['reply'])
        self.assertEqual(answer['evidence']['order_action'], 'reactivated')
        self.assertFalse(answer['handoff'])

    def test_shop_refuses_then_owner_issues(self):
        shop = FakeShop({'num': 9974, 'status': 'paid', 'service_key': 'claude', 'activation_status': 'not_checked'},
                        resend={'ok': False, 'error': 'not_connected', 'msg': ''})
        answer = run(shop)
        self.assertIn('Александр выдаст повторную активацию', answer['reply'])
        self.assertTrue(answer['evidence']['owner_alert'])

    def test_window_open_and_activated_and_unpaid(self):
        open_ = run(FakeShop({'num': 1, 'status': 'paid', 'service_key': 'chatgpt', 'activation_status': 'pending_window'}))
        self.assertIn('нажмите её', open_['reply'])
        done = run(FakeShop({'num': 1, 'status': 'paid', 'service_key': 'chatgpt', 'activation_status': 'code_consumed'}))
        self.assertIn('10–15 минут', done['reply'])
        # the shop marks «fulfilled» when it SENDS the button: the shop decides; an activated order is refused by it
        sent = FakeShop({'num': 1, 'status': 'paid', 'service_key': 'chatgpt', 'activation_status': 'fulfilled_recorded'},
                        resend={'ok': False, 'error': 'already_activated', 'msg': ''})
        self.assertIn('10–15 минут', run(sent)['reply'])
        self.assertEqual(len(sent.resent), 1)
        expired = FakeShop({'num': 1, 'status': 'paid', 'service_key': 'chatgpt', 'activation_status': 'fulfilled_recorded'})
        self.assertEqual(run(expired)['evidence']['order_action'], 'reactivated')
        unpaid = run(FakeShop(None, status='not_found'))
        self.assertTrue(unpaid['handoff'])
        self.assertIsNone(run(FakeShop(None, status='read_error')))

    def test_no_reactivation_for_manual_services(self):
        shop = FakeShop({'num': 5, 'status': 'paid', 'service_key': 'cursor', 'activation_status': 'not_checked'})
        answer = run(shop)
        self.assertEqual(shop.resent, [])
        self.assertIn('Александр проверит', answer['reply'])
