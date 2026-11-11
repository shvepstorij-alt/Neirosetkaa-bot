"""Renaming a ChatGPT/Claude plan in the admin panel must not change which activation codes are used."""
import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source(name):
    with open(os.path.join(ROOT, name), encoding='utf-8') as handle:
        return handle.read()


def _function(source, name, extra=None):
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(extra or {})
    exec(compile(ast.Module(body=[node], type_ignores=[]), name, 'exec'), namespace)
    return namespace[name]


class CodeKeyTests(unittest.TestCase):
    def test_chatgpt_renamed_plan_keeps_its_pool_key(self):
        keys = {'chatgpt': {}, 'claude': {}}
        to_key = _function(_source('config.py'), 'plan_name_to_key', {'PLAN_CODE_KEYS': keys})
        self.assertEqual(to_key('Pro 5×'), 'pro_5x')
        self.assertNotEqual(to_key('Pro 100'), 'pro_5x')
        keys['chatgpt']['Pro 100'] = to_key('Pro 5×')
        self.assertEqual(to_key('Pro 100'), 'pro_5x')
        self.assertEqual(to_key('Plus'), 'plus')

    def test_claude_ultra_keeps_max_20x_codes(self):
        import sys
        import types
        keys = {'chatgpt': {}, 'claude': {}}
        fake = types.ModuleType('config')
        fake.PLAN_CODE_KEYS = keys
        saved = sys.modules.get('config')
        sys.modules['config'] = fake
        try:
            claude_key = _function(_source('common.py'), '_claude_plan_key')
            self.assertEqual(claude_key('Ultra'), 'pro')  # without the saved key Ultra would get Pro codes
            keys['claude']['Ultra'] = claude_key('Max 20×')
            self.assertEqual(claude_key('Ultra'), 'max_20x')
            self.assertEqual(claude_key('Max 5×'), 'max_5x')
        finally:
            if saved is not None:
                sys.modules['config'] = saved
            else:
                sys.modules.pop('config', None)

    def test_admin_edit_saves_the_old_key_and_startup_loads_it(self):
        common = _source('common.py')
        self.assertIn("f'code_key:{key}:{plan[\"name\"]}',old_key", common)
        self.assertIn("p['renameEditable'] = True", common)
        self.assertNotIn('название используется маршрутом активации', common)
        self.assertIn("plan_key = _claude_plan_key(plan_name)", common)
        self.assertIsNotNone(re.search(r"LIKE 'code_key:%'", _source('db.py')))


if __name__ == '__main__':
    unittest.main()
