"""Owner 21.11.2026: «сразу для всех» — AUTO for every client once the global switch is on."""
import tempfile
import unittest
from pathlib import Path

from support_agent.storage import Store


class AutoForAll(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 's.db')
        self.store.connection('biz', True, True)
        self.store.ingest('biz', 77, 1, 'client', 'привет', {}, 1.0)
        self.cid = self.store.db.execute('SELECT id FROM chats').fetchone()[0]

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_default_chat_follows_the_global_switch(self):
        self.store.switch('auto')
        self.assertEqual(self.store.effective_mode(self.cid), 'draft')
        self.store.put('auto_all', '1')
        self.assertEqual(self.store.effective_mode(self.cid), 'auto')

    def test_answering_myself_stays_off(self):
        self.store.put('auto_all', '1')
        self.store.switch('auto')
        self.store.switch('off', self.cid)
        self.assertEqual(self.store.effective_mode(self.cid), 'off')

    def test_draft_global_mode_never_sends(self):
        self.store.put('auto_all', '1')
        self.store.switch('draft')
        self.assertEqual(self.store.effective_mode(self.cid), 'draft')


if __name__ == '__main__':
    unittest.main()
