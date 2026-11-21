"""Support assistant repeat-activation action: auth, parsing, eligibility, flow."""
import asyncio
import json
import os
import unittest
from unittest import mock

import support_shop_actions as ssa

TOKEN = 'x' * 40
PAID_GPT = {'order_id': 'shop_5_1700000000abc', 'num': 42, 'status': 'paid',
            'pack': 'shop:chatgpt:0:Plus', 'label': 'ChatGPT Plus'}
CLEAN = {'lookup': 'ok', 'fulfilled': True, 'auto_plan': True}


def sig(**kw):
    return dict(CLEAN, **kw)


class _Content:
    def __init__(self, raw):
        self.raw = raw

    async def iter_chunked(self, n):
        for i in range(0, len(self.raw), n):
            yield self.raw[i:i + n]


class _Req:
    def __init__(self, body, auth='Bearer ' + TOKEN):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = {'Authorization': auth} if auth is not None else {}
        self.content = _Content(raw)


def _resp(payload, status=200, headers=None):
    return (status, payload)


def run(coro):
    return asyncio.run(coro)


class Auth(unittest.TestCase):
    def test_disabled_without_or_short_token(self):
        self.assertEqual(ssa.check_auth('Bearer ' + TOKEN, ''), (503, {'ok': False, 'error': 'disabled'}))
        self.assertEqual(ssa.check_auth('Bearer short', 'short')[0], 503)

    def test_wrong_missing_long_header(self):
        self.assertEqual(ssa.check_auth('Bearer ' + 'y' * 40, TOKEN)[0], 401)
        self.assertEqual(ssa.check_auth('', TOKEN)[0], 401)
        self.assertEqual(ssa.check_auth(None, TOKEN)[0], 401)
        self.assertEqual(ssa.check_auth('Bearer ' + TOKEN + 'z' * 600, TOKEN)[0], 401)
        self.assertEqual(ssa.check_auth(TOKEN, TOKEN)[0], 401)

    def test_ok(self):
        self.assertIsNone(ssa.check_auth('Bearer ' + TOKEN, TOKEN))


class Body(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(ssa.parse_body(b'{"user_id": 5, "order": "42"}'), (5, '42'))
        self.assertEqual(ssa.parse_body(b'{"user_id": 5, "order": "#42"}'), (5, '42'))
        self.assertEqual(ssa.parse_body(b'{"user_id": 5, "order": 42}'), (5, '42'))
        self.assertEqual(ssa.parse_body(b'{"user_id": 5, "order": "shop_5_1700000000abc"}'),
                         (5, 'shop_5_1700000000abc'))

    def test_invalid(self):
        for raw, code in ((b'not json', 'body'), (b'[]', 'body'),
                          (b'{"user_id": "5", "order": "1"}', 'user_id'),
                          (b'{"user_id": true, "order": "1"}', 'user_id'),
                          (b'{"user_id": 0, "order": "1"}', 'user_id'),
                          (b'{"user_id": 5, "order": "12a"}', 'order'),
                          (b'{"user_id": 5, "order": "1 OR 1=1"}', 'order'),
                          (b'{"user_id": 5, "order": "0"}', 'order'),
                          (b'{"user_id": 5, "order": "' + b'1' * 21 + b'"}', 'order'),
                          (b'{"user_id": 5}', 'order')):
            with self.assertRaises(ValueError) as cm:
                ssa.parse_body(raw)
            self.assertEqual(str(cm.exception), code, raw)

    def test_clause(self):
        self.assertEqual(ssa.order_clause('42'), '(num::text=$2 OR fk_intid=$2)')
        self.assertEqual(ssa.order_clause('shop_5_1abc'), 'order_id=$2')


class Eligibility(unittest.TestCase):
    def err(self, order, signals):
        out = ssa.eligibility(order, signals)
        return None if out['ok'] else out['error']

    def test_eligible_all_services(self):
        for pack in ('shop:chatgpt:0:Plus', 'shop:claude:1:Pro', 'shop:perplexity:0'):
            out = ssa.eligibility(dict(PAID_GPT, pack=pack), sig(auto_plan=None))
            self.assertTrue(out['ok'], pack)
            self.assertEqual(out['service'], pack.split(':')[1])

    def test_fulfilled_alone_does_not_block(self):
        self.assertIsNone(self.err(PAID_GPT, sig(fulfilled=True)))

    def test_branches(self):
        cases = [
            (None, {}, 'not_found'),
            (None, {'ambiguous': True}, 'ambiguous'),
            (dict(PAID_GPT, status='pending'), CLEAN, 'not_paid'),
            (dict(PAID_GPT, pack='shop:midjourney:0'), CLEAN, 'not_supported'),
            (dict(PAID_GPT, pack='credits_100'), CLEAN, 'not_supported'),
            (dict(PAID_GPT, pack='shop:chatgpt'), CLEAN, 'not_supported'),
            (PAID_GPT, sig(lookup='unavailable'), 'unavailable'),
            (PAID_GPT, {}, 'unavailable'),
            (PAID_GPT, sig(recently_sent=True, pending_open=True), 'already_sent'),
            (PAID_GPT, sig(code_used=True), 'already_activated'),
            (PAID_GPT, sig(marked_done=True), 'already_activated'),
            (PAID_GPT, sig(activation_started=True, pending_open=True), 'in_progress'),
            (PAID_GPT, sig(acthelp_phase='pending'), 'in_progress'),
            (PAID_GPT, sig(acthelp_phase='review'), 'in_progress'),
            (PAID_GPT, sig(acthelp_phase='success'), 'in_progress'),
            (PAID_GPT, sig(pending_open=True), 'window_open'),
            (PAID_GPT, sig(linkpay_status='awaiting_link'), 'manual_order'),
            (PAID_GPT, sig(nsgifts_status='pending'), 'manual_order'),
            (PAID_GPT, sig(auto_plan=False), 'manual_plan'),
            (PAID_GPT, sig(too_old=True), 'too_old'),
        ]
        for order, signals, code in cases:
            out = ssa.eligibility(order, signals)
            self.assertFalse(out['ok'], code)
            self.assertEqual(out['error'], code)
            self.assertTrue(out['msg'])

    def test_retryable_acthelp_and_closed_window(self):
        self.assertIsNone(self.err(PAID_GPT, sig(acthelp_phase='input', pending_open=False)))
        self.assertIsNone(self.err(PAID_GPT, sig(acthelp_phase='confirm')))

    def test_manual_plan_only_for_chatgpt(self):
        self.assertIsNone(self.err(dict(PAID_GPT, pack='shop:claude:0'), sig(auto_plan=False)))


class Redact(unittest.TestCase):
    def test_no_codes_links_emails(self):
        for text in ('Кнопка отправлена клиенту.\n🎟 Код: <code>ABCD-1234-EFGH</code> · PH',
                     'Заказ уже активирован (код XYZ12345QW) — повторно не выдаю.',
                     'По коду QWERTYUIOP неясный исход — сначала реши по нему',
                     'Напиши на a.b@mail.ru или https://t.me/x?start=abc123def'):
            out = ssa.redact(text)
            for bad in ('ABCD', '1234', 'XYZ12345QW', 'QWERTYUIOP', '@mail', 'http', 't.me', '<'):
                self.assertNotIn(bad, out, text)

    def test_keeps_plain_reason(self):
        msg = 'Для этого товара самостоятельная активация не предусмотрена — заказ выполняется вручную.'
        self.assertEqual(ssa.redact(msg), msg)


class Flow(unittest.TestCase):
    def make(self, order=PAID_GPT, signals=CLEAN, claim_left=0, resend_result=(True, 'Код: <code>AAAA1111BBBB</code>'),
             notify_exc=None, resend_exc=None):
        calls = {'claim': [], 'resend': [], 'notify': []}

        async def fetch(uid, ref):
            calls['fetch'] = (uid, ref)
            return (dict(order) if order else None), dict(signals)

        async def claim(key, seconds):
            calls['claim'].append((key, seconds))
            return claim_left

        async def resend(oid):
            calls['resend'].append(oid)
            if resend_exc:
                raise resend_exc
            return resend_result

        async def notify(text):
            calls['notify'].append(text)
            if notify_exc:
                raise notify_exc

        h = ssa.make_support_resend_handler(fetch=fetch, claim=claim, resend=resend, notify=notify,
                                            json_response=_resp)
        return h, calls

    def call(self, h, req):
        with mock.patch.dict(os.environ, {ssa.TOKEN_ENV: TOKEN}):
            return run(h(req))

    def test_auth_and_body_errors(self):
        h, calls = self.make()
        with mock.patch.dict(os.environ, {ssa.TOKEN_ENV: ''}):
            self.assertEqual(run(h(_Req({'user_id': 5, 'order': '42'})))[0], 503)
        self.assertEqual(self.call(h, _Req({'user_id': 5, 'order': '42'}, auth='Bearer bad'))[0], 401)
        self.assertEqual(self.call(h, _Req({'user_id': 5, 'order': '42'}, auth=None))[0], 401)
        self.assertEqual(self.call(h, _Req(b'{"user_id":5,"order":"' + b'1' * 3000 + b'"}'))[0], 413)
        self.assertEqual(self.call(h, _Req({'user_id': '5', 'order': '42'})), (400, {'ok': False, 'error': 'user_id'}))
        self.assertEqual(self.call(h, _Req({'user_id': 5, 'order': 'abc'})), (400, {'ok': False, 'error': 'order'}))
        self.assertEqual(calls['resend'], [])

    def test_success(self):
        h, calls = self.make()
        status, body = self.call(h, _Req({'user_id': 5, 'order': '42'}))
        self.assertEqual(status, 200)
        self.assertEqual(body, {'ok': True, 'error': None, 'msg': ssa.OK_MSG})
        self.assertEqual(calls['fetch'], (5, '42'))
        self.assertEqual(calls['claim'], [('support_resend:shop_5_1700000000abc', 86400)])
        self.assertEqual(calls['resend'], ['shop_5_1700000000abc'])
        self.assertIn('Помощник выдал повторную активацию · заказ #42 · ChatGPT Plus · ok', calls['notify'][0])
        self.assertNotIn('AAAA1111BBBB', json.dumps(body))

    def test_ineligible_does_not_claim_or_resend(self):
        h, calls = self.make(signals=sig(pending_open=True))
        status, body = self.call(h, _Req({'user_id': 5, 'order': '42'}))
        self.assertEqual((status, body['error']), (200, 'window_open'))
        self.assertEqual((calls['claim'], calls['resend'], calls['notify']), ([], [], []))

    def test_claim_race_already_sent(self):
        h, calls = self.make(claim_left=3600)
        body = self.call(h, _Req({'user_id': 5, 'order': '42'}))[1]
        self.assertEqual(body['error'], 'already_sent')
        self.assertEqual(calls['resend'], [])

    def test_resend_failure_redacted_and_notified(self):
        h, calls = self.make(resend_result=(False, 'По коду QWERTY1234 неясный исход'))
        body = self.call(h, _Req({'user_id': 5, 'order': '42'}))[1]
        self.assertEqual(body['error'], 'resend_failed')
        self.assertNotIn('QWERTY1234', body['msg'])
        self.assertIn('ошибка', calls['notify'][0])

    def test_manual_plan_passthrough(self):
        h, _ = self.make(resend_result=(False, 'Для этого товара самостоятельная активация не предусмотрена — заказ выполняется вручную.'))
        body = self.call(h, _Req({'user_id': 5, 'order': '42'}))[1]
        self.assertEqual(body['error'], 'manual_plan')
        self.assertIn('вручную', body['msg'])

    def test_notify_and_resend_exceptions_never_fail(self):
        h, _ = self.make(notify_exc=RuntimeError('tg down'))
        self.assertTrue(self.call(h, _Req({'user_id': 5, 'order': '42'}))[1]['ok'])
        h, calls = self.make(resend_exc=RuntimeError('boom'))
        status, body = self.call(h, _Req({'user_id': 5, 'order': '42'}))
        self.assertEqual((status, body['ok'], body['error']), (200, False, 'resend_failed'))
        self.assertEqual(len(calls['notify']), 1)

    def test_fetch_error(self):
        async def bad(uid, ref):
            raise RuntimeError('db')
        h = ssa.make_support_resend_handler(fetch=bad, claim=None, resend=None, notify=None, json_response=_resp)
        body = self.call(h, _Req({'user_id': 5, 'order': '42'}))[1]
        self.assertEqual(body['error'], 'read_error')


class FetchFacts(unittest.TestCase):
    """SQL shape: ownership in WHERE, one row, signal mapping."""

    class Conn:
        def __init__(self, rows, values):
            self.rows, self.values, self.sql = rows, values, []

        async def fetch(self, sql, *args):
            self.sql.append((sql, args))
            return self.rows

        async def fetchrow(self, sql, *args):
            self.sql.append((sql, args))
            if 'pending_activations' in sql:
                return self.values.get('pending')
            return None

        async def fetchval(self, sql, *args):
            self.sql.append((sql, args))
            if '_codes' in sql:
                return self.values.get('used')
            if 'activation_claims' in sql:
                return self.values.get('claim')
            if args and str(args[0]).startswith('order_done:'):
                return self.values.get('done')
            if args and str(args[0]).startswith('acthelp:'):
                return self.values.get('acthelp')
            return None

    class Pool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            conn = self.conn

            class _Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *a):
                    return False
            return _Ctx()

    def row(self, **kw):
        base = {'order_id': 'shop_5_1abc', 'num': 42, 'fk_intid': '999', 'status': 'paid',
                'pack': 'shop:chatgpt:0:Plus', 'paid_at': None, 'fulfilled_at': 1, 'too_old': False}
        base.update(kw)
        return base

    def test_owned_query_and_signals(self):
        conn = self.Conn([self.row()], {'pending': {'open': False, 'started': False},
                                        'acthelp': '{"phase": "input"}', 'done': '0'})
        order, signals = run(ssa.fetch_facts(self.Pool(conn), 5, '42',
                                             plan_info=lambda p: {'label': 'ChatGPT Plus', 'auto': True}))
        sql, args = conn.sql[0]
        self.assertIn('WHERE user_id=$1 AND (num::text=$2 OR fk_intid=$2)', sql)
        self.assertEqual(args, (5, '42'))
        self.assertEqual(signals['pending_open'], False)
        self.assertEqual(signals['acthelp_phase'], 'input')
        self.assertFalse(signals['code_used'] or signals['marked_done'] or signals['recently_sent'])
        self.assertTrue(ssa.eligibility(order, signals)['ok'])

    def test_ambiguous_and_missing(self):
        conn = self.Conn([self.row(), self.row(order_id='shop_5_2abc')], {})
        self.assertEqual(run(ssa.fetch_facts(self.Pool(conn), 5, '42'))[1], {'ambiguous': True})
        conn = self.Conn([], {})
        self.assertEqual(run(ssa.fetch_facts(self.Pool(conn), 5, '42')), (None, {}))

    def test_used_code_blocks(self):
        conn = self.Conn([self.row(pack='shop:claude:0:Pro')], {'used': 1})
        order, signals = run(ssa.fetch_facts(self.Pool(conn), 5, '42'))
        self.assertEqual(ssa.eligibility(order, signals)['error'], 'already_activated')

    def test_signal_read_error_is_unavailable(self):
        conn = self.Conn([self.row()], {})

        async def boom(*a):
            raise RuntimeError('x')
        conn.fetchrow = boom
        order, signals = run(ssa.fetch_facts(self.Pool(conn), 5, '42'))
        self.assertEqual(ssa.eligibility(order, signals)['error'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
