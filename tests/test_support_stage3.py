"""Stage 3: call accounting, rule scope, batch deadline, coins and weekends."""
import time
import unittest

from support_agent.handoff import handoff_text
from support_agent.owner_scenarios import reply as scenario
from support_agent.storage import Store
from support_agent.turn_stages import call_estimate, full_rules, next_batch_due, relevant_history, select_rules
from support_agent.usage import CallMeter, reset_meter, use_meter
import datetime as dt


class WeekendTests(unittest.TestCase):
    def test_saturday_inside_hours_has_no_delay_warning(self):
        zone = dt.timezone(dt.timedelta(hours=3))
        saturday = dt.datetime(2026, 11, 7, 15, 0, tzinfo=zone)
        text = handoff_text(saturday)
        self.assertNotIn('задержк', text)
        self.assertNotIn('выходн', text)
        self.assertIn('получаса-часа', text)


class CoinTests(unittest.TestCase):
    def test_only_friend_purchases(self):
        text = scenario('как получить и потратить монетки')['reply']
        self.assertIn('только за приглашённых друзей', text)
        self.assertIn('10% с первой покупки каждого друга', text)
        self.assertIn('оплату подписки', text)
        self.assertIn('Других способов получить или потратить монетки нет', text)


class RuleScopeTests(unittest.TestCase):
    def test_price_question_keeps_secrets_and_drops_course(self):
        selected = select_rules('сколько стоит подписка')
        self.assertIn('Не раскрывать', selected)
        self.assertIn('Деньги и исключения', selected)
        self.assertNotIn('Клиенты с курса', selected)
        self.assertLess(len(selected), len(full_rules()))

    def test_course_question_keeps_course_rules(self):
        self.assertIn('Клиенты с курса', select_rules('бонус за курс'))


class HistoryTests(unittest.TestCase):
    def test_recent_history_unless_the_client_points_backward(self):
        rows = [{'text': str(i)} for i in range(20)]
        self.assertEqual(len(relevant_history(rows, 'сколько стоит')), 12)
        self.assertEqual(len(relevant_history(rows, 'как я писал раньше')), 20)


class BatchTests(unittest.TestCase):
    def test_fragments_do_not_postpone_the_deadline(self):
        store = Store(':memory:', batch_seconds=180)
        store.connection('biz', True, True)
        store.switch('draft')
        start = time.time()
        cid = store.ingest('biz', 7, 1, 'client', 'раз', {'kind': 'none'}, start)
        due = store.chat(cid)['batch_due']
        store.ingest('biz', 7, 2, 'client', 'два', {'kind': 'none'}, start + 60)
        store.ingest('biz', 7, 3, 'client', 'три', {'kind': 'none'}, start + 170)
        self.assertEqual(store.chat(cid)['batch_due'], due)
        self.assertLessEqual(due, store.chat(cid)['batch_start'] + 180)
        texts = [row['text'] for row in store.db.execute(
            'SELECT text FROM messages WHERE chat=? ORDER BY mid', (cid,))]
        self.assertEqual(texts, ['раз', 'два', 'три'])

    def test_pause_keeps_later_fragments(self):
        store = Store(':memory:', batch_seconds=0)
        store.connection('biz', True, True)
        store.switch('draft')
        now = time.time()
        cid = store.ingest('biz', 7, 1, 'client', 'первый', {'kind': 'none'}, now)
        store.ingest('biz', 7, 2, 'owner', 'ответил', {'kind': 'none'}, now)
        store.ingest('biz', 7, 3, 'client', 'ещё', {'kind': 'none'}, now)
        store.ingest('biz', 7, 4, 'client', 'и это', {'kind': 'none'}, now)
        store.db.execute('UPDATE chats SET manual_pause_until=0 WHERE id=?', (cid,))
        job = store.claim(time.time())
        self.assertEqual(job['mid'], 4)
        kept = [row['text'] for row in store.db.execute(
            "SELECT text FROM messages WHERE chat=? AND role='client' AND mid>=? AND mid<=? ORDER BY mid",
            (cid, store.chat(cid)['batch_first'], job['mid']))]
        self.assertEqual(kept, ['ещё', 'и это'])

    def test_deadline_helper_never_extends_an_open_batch(self):
        self.assertEqual(next_batch_due(100, 150, 180, 280, True), 280)
        self.assertEqual(next_batch_due(100, 400, 180, 500, True), 280)


class UsageTests(unittest.TestCase):
    def test_quota_counts_requests_not_intent(self):
        store = Store(':memory:', batch_seconds=0)
        meter = CallMeter(store, 100, 7)
        token = use_meter(meter)
        try:
            meter.arm('draft')
            self.assertEqual(store.calls_today(), 0)
            meter.consume('responses')
            meter.consume('responses')
        finally:
            reset_meter(token)
        self.assertEqual(store.calls_today(), 2)
        self.assertEqual([event['reason'] for event in meter.events], ['draft', 'follow_up'])
        self.assertNotIn('text', meter.events[0])

    def test_static_call_estimate(self):
        self.assertEqual(call_estimate('deterministic'), 0)
        self.assertEqual(call_estimate('consultation'), 2)
        self.assertEqual(call_estimate('public_features'), 4)
        self.assertEqual(call_estimate('public_features_cached'), 3)


if __name__ == '__main__':
    unittest.main()
