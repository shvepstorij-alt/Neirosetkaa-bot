"""Stage 4: the shop bot no longer calls the old consultant."""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class ConsultantRemovalTests(unittest.TestCase):
    def test_shop_entry_does_not_call_claude_consultant(self):
        bot = (ROOT / 'bot.py').read_text(encoding='utf-8')
        chat = (ROOT / 'handlers_chat.py').read_text(encoding='utf-8')
        self.assertNotIn('claude_with_search', bot)
        self.assertNotIn('claude_with_search', chat)
        self.assertIn('CONSULT_MOVED_TEXT', bot)
        self.assertIn('process_linkpay_link', bot)
        self.assertIn('import handlers_claude', bot)

    def test_archived_files_are_not_the_live_modules(self):
        self.assertFalse((ROOT / 'catalog_context.py').exists())
        self.assertTrue((ROOT / 'archive' / 'root' / 'catalog_context.py').exists())
        self.assertTrue((ROOT / 'support_agent' / 'catalog_context.py').exists())
        self.assertFalse((ROOT / 'support_agent' / 'core.py').exists())
        self.assertTrue((ROOT / 'archive' / 'support_agent' / 'core.py').exists())
        self.assertFalse((ROOT / 'knowledge.json').exists())


if __name__ == '__main__':
    unittest.main()
