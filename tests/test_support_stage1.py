"""Stage-1 checks: questions vs secrets, manual pause, orders, service words."""
import pathlib
import re
import time
import unittest

from support_agent.catalog_context import annotate
from support_agent.order_activation import activation_from_signals, parse_shop_pack, project_order
from support_agent.order_status import status_reply
from support_agent.privacy import scrub, sensitive
from support_agent.storage import MANUAL_PAUSE_SECONDS, Store
from support_agent.workflow_policy import ambiguity_reply, policies_for


ROOT = pathlib.Path(__file__).resolve().parents[1]


class PrivacyTests(unittest.TestCase):
    def test_consultation_questions_stay_visible(self):
        questions = (
            'забыл пароль',
            'забыл пароль от chatgpt',
            'истёк токен',
            'истек токен',
            'не приходит код',
            'не приходит код на почту',
            'не вставляется сессия',
            'session expired',
        )
        for question in questions:
            self.assertFalse(sensitive(question), question)
            self.assertNotIn('Сообщение скрыто', scrub(question))
            self.assertIn(question.split()[0].casefold().replace('ё', 'е')[:4], scrub(question).casefold().replace('ё', 'е'))

    def test_real_secrets_stay_hidden(self):
        secrets = (
            'пароль: SecretValue99',
            'мой токен eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig',
            'код 123456',
            'забыл пароль\nSecretValue99',
            'session: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature',
        )
        for value in secrets:
            self.assertTrue(sensitive(value), value)


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:', batch_seconds=0)
        self.store.connection('biz', True, True)
        self.store.switch('draft')

    def test_manual_reply_pauses_and_skips_answered_messages(self):
        now = time.time()
        cid = self.store.ingest('biz', 7, 10, 'client', 'вопрос', {'kind': 'none'}, now)
        self.assertEqual(self.store.claim(time.time())['mid'], 10)
        before = time.time()
        self.store.ingest('biz', 7, 11, 'owner', 'отвечаю сам', {'kind': 'none'}, time.time())
        chat = self.store.chat(cid)
        self.assertGreaterEqual(chat['manual_pause_until'], before + MANUAL_PAUSE_SECONDS - 1)
        self.assertEqual(chat['owner_answered_mid'], 10)
        self.assertIsNone(self.store.claim(time.time()))
        self.assertFalse(self.store.prepare_now(cid, time.time()))
        self.store.ingest('biz', 7, 12, 'client', 'ещё вопрос', {'kind': 'none'}, time.time())
        self.assertIsNone(self.store.claim(time.time()))
        self.store.db.execute('UPDATE chats SET manual_pause_until=0 WHERE id=?', (cid,))
        job = self.store.claim(time.time())
        self.assertEqual(job['mid'], 12)

    def test_assistant_outgoing_is_not_a_manual_reply(self):
        now = time.time()
        cid = self.store.ingest('biz', 7, 10, 'client', 'вопрос', {'kind': 'none'}, now)
        self.store.db.execute(
            'INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
            (cid, 11, 'assistant', 'автоответ', '{}', now, 'done'))
        self.store.ingest('biz', 7, 11, 'owner', 'автоответ', {'kind': 'none'}, time.time())
        chat = self.store.chat(cid)
        self.assertEqual(chat['manual_pause_until'], 0)
        role = self.store.db.execute(
            'SELECT role FROM messages WHERE chat=? AND mid=11', (cid,)).fetchone()['role']
        self.assertEqual(role, 'assistant')

    def test_pause_blocks_auto_send(self):
        now = time.time()
        self.store.switch('auto')
        cid = self.store.ingest('biz', 7, 10, 'client', 'вопрос', {'kind': 'none'}, now)
        self.store.switch('auto', cid)
        self.store.db.execute("UPDATE messages SET state='queued' WHERE chat=? AND mid=10", (cid,))
        self.store.db.execute('UPDATE chats SET batch_due=? WHERE id=?', (time.time(), cid))
        job = self.store.claim(time.time())
        did = self.store.finish(job, 'черновик', 'кратко', False, {'status': 'catalog_direct', 'sources': []})
        self.store.db.execute(
            'UPDATE chats SET manual_pause_until=? WHERE id=?', (time.time() + MANUAL_PAUSE_SECONDS, cid))
        reason = self.store.auto_send_block_reason(did, time.time())
        self.assertIn('пауза', reason)
        self.assertIsNone(self.store.reserve_auto_send(did, time.time()))
        self.assertIsNone(self.store.reserve_send(did, time.time()))


class OrderTests(unittest.TestCase):
    def _lookup(self, status, orders, reference=None):
        reference = reference or {'kind': 'shop_num', 'value': '5'}
        return status_reply(
            {'order_lookup': {'status': status, 'ownership_scope': 'authenticated_user',
                              'reference': reference, 'orders': orders}},
            'проверь заказ', reference)

    def test_unavailable_source_is_not_called_unpaid(self):
        for status in ('not_connected', 'read_error', 'timeout'):
            reply = self._lookup(status, [])['reply']
            self.assertIn('не означает, что заказ не оплачен', reply)
            self.assertNotIn('оплата не прошла', reply)
            self.assertNotIn('статус: не оплачен', reply)

    def test_paid_order_keeps_service_and_does_not_invent_activation(self):
        self.assertEqual(parse_shop_pack('shop:chatgpt:0:Plus')['plan_name'], 'Plus')
        row = project_order(
            {'num': 5, 'fk_intid': '9', 'status': 'paid', 'amount_rub': 1990,
             'pack': 'shop:chatgpt:0:Plus', 'fulfilled_at': None, 'order_id': 'order-1'},
            {'lookup': 'ok'})
        self.assertEqual(row['service_key'], 'chatgpt')
        self.assertEqual(row['activation_status'], 'not_checked')
        self.assertNotIn('order_id', row)
        reply = self._lookup('available', [row])['reply']
        self.assertIn('оплата отмечена', reply)
        self.assertIn('chatgpt', reply)
        self.assertIn('Plus', reply)
        self.assertNotIn('не оплачен', reply)
        unavailable = activation_from_signals('paid', {'lookup': 'unavailable'})
        self.assertEqual(unavailable['activation_status'], 'unavailable')
        self.assertIn('не значит, что оплата не прошла', unavailable['activation_note'])

    def test_foreign_order_is_not_disclosed(self):
        reference = {'kind': 'shop_num', 'value': '5'}
        reply = status_reply(
            {'order_lookup': {'status': 'available', 'ownership_scope': 'not_confirmed',
                              'reference': reference, 'orders': [{'num': 5, 'status': 'paid', 'amount_rub': 1990}]}},
            'проверь заказ', reference)['reply']
        self.assertNotIn('1990', reply)
        self.assertNotIn('оплата отмечена', reply)


class ServiceDetectionTests(unittest.TestCase):
    def test_pointer_and_video_do_not_request_credentials(self):
        for text, service in (('курсор пропал', 'cursor'), ('видео для ютуб', 'youtube')):
            reply = ambiguity_reply(text)
            self.assertIsNotNone(reply, text)
            lowered = reply['reply'].casefold()
            self.assertNotIn('почт', lowered)
            self.assertNotIn('парол', lowered)
            policies = policies_for({
                'conversation': {'latest_client_message': text},
                'requested_catalog': [{'service': 'Cursor' if service == 'cursor' else 'YouTube',
                                       'service_key': service}]})
            self.assertFalse(any(item['service'] == service for item in policies), text)

    def test_explicit_purchase_still_matches(self):
        text = 'хочу оформить подписку cursor'
        self.assertIsNone(ambiguity_reply(text))
        policies = policies_for({'conversation': {'latest_client_message': text}, 'requested_catalog': []})
        self.assertTrue(any(item['service'] == 'cursor' for item in policies))
        youtube = policies_for({
            'conversation': {'latest_client_message': 'сколько стоит youtube premium'},
            'requested_catalog': []})
        self.assertTrue(any(item['service'] == 'youtube' for item in youtube))

    def test_catalog_match_ignores_ambiguous_word(self):
        catalog = [{'service_key': 'cursor', 'service': 'Cursor', 'plan': 'Pro',
                    'catalog_price_rub': 1000, 'plan_index': 0},
                   {'service_key': 'chatgpt', 'service': 'ChatGPT', 'plan': 'Plus',
                    'catalog_price_rub': 2000, 'plan_index': 0}]
        missed = annotate({'catalog': catalog}, 'курсор пропал')['requested_catalog']
        self.assertEqual(missed, [])
        found = annotate({'catalog': catalog}, 'сколько стоит подписка cursor')['requested_catalog']
        self.assertEqual(found[0]['service_key'], 'cursor')


class SecretPlaceholderTests(unittest.TestCase):
    def test_example_env_has_no_live_credentials(self):
        text = (ROOT / 'env.example.txt').read_text(encoding='utf-8')
        self.assertNotRegex(text, r'\d{8,12}:[A-Za-z0-9_-]{30,}')
        self.assertNotRegex(text, r'sk-ant-api\d+-')
        self.assertIn('REPLACE_WITH_SHOP_BOT_TOKEN', text)
        self.assertIn('REPLACE_WITH_MODEL_API_KEY', text)

    def test_readonly_sql_is_present_and_avoids_secret_columns(self):
        raw = (ROOT / 'support_agent' / 'sql' / 'support_readonly.sql').read_text(encoding='utf-8')
        sql = '\n'.join(line for line in raw.lower().splitlines() if not line.strip().startswith('--'))
        self.assertIn('support_catalog_readonly', sql)
        self.assertIn('support_price_context_readonly', sql)
        self.assertNotIn('session_raw', sql)
        self.assertNotIn('account_pass', sql)
        self.assertNotIn('account_email', sql)


if __name__ == '__main__':
    unittest.main()
