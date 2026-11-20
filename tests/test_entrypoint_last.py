"""Owner 21.11.2026 (release 68 crash): code below `if __name__ == '__main__'` does not exist when the bot runs.

Tests import modules whole, so such code passes tests but fails on Railway. The main guard must be last.
"""
import ast
import pathlib
import unittest


class EntrypointLastTests(unittest.TestCase):
    def test_main_guard_is_the_last_statement(self):
        problems = []
        for path in sorted(pathlib.Path('support_agent').glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for index, node in enumerate(tree.body):
                if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                        and getattr(node.test.left, 'id', '') == '__name__' and index != len(tree.body) - 1):
                    problems.append('%s:%d — после блока запуска есть код' % (path.name, node.lineno))
        self.assertEqual(problems, [])
