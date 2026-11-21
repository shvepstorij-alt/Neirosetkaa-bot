"""Owner 21.11.2026: in a chat Alexander is handling, the assistant waits 10 minutes for him."""
import tempfile
import time
import unittest

from support_agent.storage import Store


class OwnerGraceTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(suffix='.sqlite')
        self.store = Store(self.file.name, batch_seconds=60)
        self.store.connection('biz', True, True)

    def _chat(self, mode):
        self.store.put('mode', mode)
        cid = self.store.ingest('biz', 50, 1, 'client', 'Здравствуйте', {}, time.time() - 7200)
        self.store.db.execute('UPDATE chats SET mode=? WHERE id=?', (mode, cid))
        return cid

    def test_fresh_chat_answers_fast(self):
        cid = self._chat('auto')
        self.assertEqual(self.store.answer_wait(cid), 60)

    def test_owner_active_chat_waits_ten_minutes(self):
        cid = self._chat('auto')
        self.store.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                              (cid, 2, 'owner', 'Пришлите токен', '{}', time.time() - 3600, 'stored'))
        self.assertEqual(self.store.answer_wait(cid), 600)
        self.assertEqual(self.store.answer_wait(cid, now=time.time() + 2 * 86400), 60)  # owner silent for a day

    def test_draft_mode_keeps_fast_drafts(self):
        cid = self._chat('draft')
        self.store.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                              (cid, 2, 'owner', 'Пришлите токен', '{}', time.time() - 60, 'stored'))
        self.assertEqual(self.store.answer_wait(cid), 60)
