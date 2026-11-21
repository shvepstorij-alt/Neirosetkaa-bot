"""/live fairness (21.11.2026): first-reply reference, real gaps, media markers, auto-send verdict."""
import asyncio
import json
import time
import unittest
from unittest import mock

from support_agent import dialog_check
from support_agent.draft_stats import live_cases
from support_agent.storage import Store


def _msg(db, chat, mid, role, text, at, media=None):
    db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
               (chat, mid, role, text, json.dumps(media or {}), at, 'stored'))


def _draft(db, did, chat, mid, created, state='stale'):
    db.execute("INSERT INTO drafts(id,chat,mid,revision,global_revision,text,summary,handoff,created,state) "
               "VALUES (?,?,?,0,0,'черновик','',0,?,?)", (did, chat, mid, created, state))
    db.execute('INSERT INTO draft_evidence VALUES (?,?)', (did, json.dumps({})))


class LiveCasesFairTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.db = self.store.db
        self.now = time.time()

    def test_reference_is_first_owner_reply_only(self):
        db, now = self.db, self.now
        _msg(db, 1, 1, 'owner', 'Здравствуйте!', now - 3 * 86400)
        _msg(db, 1, 2, 'client', 'Не работает подписка', now - 3600)
        _draft(db, 1, 1, 2, now - 3500)
        _msg(db, 1, 3, 'owner', 'Пришлите скриншот, пожалуйста', now - 3000)
        _msg(db, 1, 4, 'client', '', now - 2900, {'kind': 'photo', 'file_id': 'x'})
        _msg(db, 1, 5, 'owner', 'Переактивировал, проверьте', now - 2000)
        _msg(db, 1, 6, 'owner', 'Всё должно работать', now - 1900)
        cases = live_cases(db, 7, now=now)
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(case['owner'], 'Пришлите скриншот, пожалуйста')
        self.assertNotIn('Переактивировал', case['owner'])
        self.assertIn('Переактивировал', case['later'])
        self.assertEqual(case['later_count'], 2)
        self.assertEqual(case['client'], 'Не работает подписка')
        # Real times are kept, not synthetic ones.
        self.assertEqual(case['times'], [now - 3 * 86400])
        self.assertEqual(case['burst'], [['Не работает подписка', now - 3600]])

    def test_history_up_to_15_long_messages(self):
        db, now = self.db, self.now
        for mid in range(1, 21):
            _msg(db, 2, mid, 'owner' if mid % 2 else 'assistant', 'x' * 2000, now - 10000 + mid)
        _msg(db, 2, 21, 'client', 'Ну что?', now - 100)
        _draft(db, 2, 2, 21, now - 90)
        _msg(db, 2, 22, 'owner', 'Готово', now - 50)
        case = live_cases(db, 7, now=now)[0]
        self.assertEqual(len(case['history']), 15)
        self.assertEqual(len(case['history'][-1][1]), 1500)
        self.assertIn('assistant', {role for role, _ in case['history']})

    def test_media_marker_and_media_only_skip(self):
        db, now = self.db, self.now
        _msg(db, 3, 1, 'owner', 'Как дела?', now - 500)
        _msg(db, 3, 2, 'client', 'вот ошибка', now - 400, {'kind': 'photo', 'file_id': 'a'})
        _msg(db, 3, 3, 'client', '', now - 390, {'kind': 'document', 'file_id': 'b'})
        _draft(db, 3, 3, 3, now - 380)
        _msg(db, 3, 4, 'owner', 'Вижу, сейчас исправлю', now - 300)
        # Media-only burst with no marker possible: skipped.
        _msg(db, 4, 1, 'owner', 'Ок', now - 500)
        _msg(db, 4, 2, 'client', '', now - 400, {'kind': 'none'})
        _draft(db, 4, 4, 2, now - 380)
        _msg(db, 4, 3, 'owner', 'Что-то не дошло', now - 300)
        cases = live_cases(db, 7, now=now)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]['client'], '[клиент прислал скриншот] вот ошибка\n[клиент прислал файл]')

    def test_two_drafts_for_one_owner_reply_are_one_case(self):
        db, now = self.db, self.now
        _msg(db, 5, 1, 'client', 'Привет', now - 500)
        _draft(db, 5, 5, 1, now - 490)
        _msg(db, 5, 2, 'client', 'Сколько стоит GPT?', now - 480)
        _draft(db, 6, 5, 2, now - 470)
        _msg(db, 5, 3, 'owner', 'Plus — 1990 ₽', now - 400)
        cases = live_cases(db, 7, now=now)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]['client'], 'Привет\nСколько стоит GPT?')


class TimelineTests(unittest.TestCase):
    def test_real_gaps_preserved(self):
        case = {'history': [['owner', 'a'], ['client', 'b']], 'times': [1000.0, 1000.0 + 86400],
                'burst': [['c', 1000.0 + 2 * 86400], ['d', 1000.0 + 2 * 86400 + 30]], 'client': 'c\nd'}
        line = dialog_check._timeline(case, 50000000.0)
        self.assertEqual(line['burst'][-1][1], 50000000.0)
        self.assertAlmostEqual(line['burst'][-1][1] - line['burst'][0][1], 30)
        self.assertAlmostEqual(line['burst'][0][1] - line['history'][1], 86400)
        self.assertAlmostEqual(line['history'][1] - line['history'][0], 86400)

    def test_archived_cases_keep_synthetic_times(self):
        line = dialog_check._timeline({'history': [['client', 'a']], 'client': 'b'}, 1000.0)
        self.assertEqual(line['burst'], [('b', 1000.0)])
        self.assertEqual(line['history'], [401.0])

    def test_grader_gets_note_and_gaps(self):
        case = {'history': [['owner', 'a'], ['client', 'b']], 'times': [1000.0, 1000.0 + 7200],
                'burst': [['c', 1000.0 + 7200 + 86400]], 'client': 'c', 'owner': 'Ответ'}
        graded = dialog_check.grade_case(case, 'r')
        self.assertTrue(graded['owner'].startswith(dialog_check.GRADE_NOTE))
        self.assertIn('Ответ', graded['owner'])
        self.assertEqual(graded['history'][1][1], '[через 2 ч] b')
        self.assertTrue(graded['client'].startswith('[через 1 дн.]'))
        archived = {'client': 'x', 'owner': 'y'}
        self.assertIs(dialog_check.grade_case(archived), archived)
        long = dict(case, history=[['client', 'z' * 1500]] * 15, times=[1000.0] * 15)
        payload = json.dumps({'history': dialog_check.grade_case(long, 'r')['history']}, ensure_ascii=False)
        self.assertLess(len(payload), 6000)


class AutoVerdictTests(unittest.TestCase):
    def test_verdicts(self):
        self.assertEqual(dialog_check.auto_verdict({}, 'x', True, 'нужен Александр', 'q'), (False, 'стоп: нужен Александр'))
        self.assertFalse(dialog_check.auto_verdict({'handoff': False}, 'x', False, '', '[клиент прислал вложение]')[0])
        with mock.patch('support_agent.auto_policy.block_reason', return_value=None):
            self.assertEqual(dialog_check.auto_verdict({'handoff': False}, 'x', False, '', 'q'), (True, 'все проверки пройдены'))
        with mock.patch('support_agent.auto_policy.block_reason', return_value='нет доказательства'):
            self.assertEqual(dialog_check.auto_verdict({'handoff': False}, 'x', False, '', 'q'), (False, 'нет доказательства'))

    def test_report_line_and_launch_metric_on_top(self):
        from support_agent.service import Service

        class Shop:
            async def facts(self, uid):
                return {'catalog': [], 'catalog_status': 'available'}

        class AI:
            model = 'test'
            graded = []

            async def grade_reply(self, case, reply, stopped, tariffs=()):
                self.graded.append(case)
                return {'verdict': 'wrong', 'category': 'other', 'problem': 'x', 'fix': ''}

        class TG:
            def __init__(self):
                self.files = []

            async def send_text_file(self, chat, name, text, caption=''):
                self.files.append((name, text))

        class Owner:
            def __init__(self):
                self.store = Store(':memory:'); self.sent = []; self.owner = 5; self.shop = Shop()
                self.ai = AI(); self.tg = TG()

            def charge_call(self, reason='model'):
                pass

            async def owner_message(self, text, *a, **k):
                self.sent.append(text)

        seen = {}

        async def generate(self, job):
            rows = self.store.db.execute('SELECT mid, role, text, at FROM messages WHERE chat=? ORDER BY mid', (job['chat'],)).fetchall()
            seen['rows'] = [tuple(r) for r in rows]
            return {'reply': 'Plus стоит 1990 ₽', 'handoff': False, 'evidence': {}}

        base = 1_000_000.0
        case = {'history': [['owner', 'Здравствуйте'], ['assistant', 'Чем помочь?']], 'times': [base, base + 60],
                'burst': [['Сколько GPT?', base + 3 * 86400], ['[клиент прислал скриншот]', base + 3 * 86400 + 20]],
                'client': 'Сколько GPT?\n[клиент прислал скриншот]', 'owner': '1990 ₽', 'later': 'Оплатили?', 'later_count': 1}
        owner = Owner()
        with mock.patch.object(Service, 'generate', generate), \
                mock.patch('support_agent.auto_policy.block_reason', return_value=None):
            asyncio.run(dialog_check.run_dialogs(owner, 0, [case], 'Проверка на ваших исправлениях'))
        rows = seen['rows']
        self.assertEqual([r[1] for r in rows], ['owner', 'assistant', 'client', 'client'])
        self.assertAlmostEqual(rows[1][3] - rows[0][3], 60, places=3)
        self.assertAlmostEqual(rows[2][3] - rows[1][3], 3 * 86400 - 60, places=3)
        self.assertAlmostEqual(rows[3][3] - rows[2][3], 20, places=3)
        self.assertTrue(owner.ai.graded[0]['owner'].startswith(dialog_check.GRADE_NOTE))
        body = owner.tg.files[0][1]
        self.assertIn('Автоотправка: да (все проверки пройдены)', body)
        self.assertIn('Позже Александр писал (1 сообщ., не эталон): Оплатили?', body)
        self.assertTrue(owner.sent[-1].startswith('Плохих ответов, которые ушли бы сами'))
        self.assertIn(': 1 из 1', owner.sent[-1].splitlines()[0])


if __name__ == '__main__':
    unittest.main()
