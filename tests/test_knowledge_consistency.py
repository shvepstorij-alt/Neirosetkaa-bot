"""Owner 20.11.2026: one truth across all knowledge files.

Every rule the owner has given is checked against faq, playbook, payment guide, service guide
and the business profile, so an old sentence in one file cannot contradict a newer rule.
"""
import pathlib
import re
import unittest

DATA = pathlib.Path('support_agent/data')
FILES = ['faq.yaml', 'playbook.yaml', 'payment_guide.yaml', 'service_guide.yaml', 'business_profile.md']

# (pattern that must NOT appear, owner rule it would break)
FORBIDDEN = [
    (r'часто не проходит', 'оплата: СБП или карта «Мир» (19.11)'),
    (r'(?i)вставьте[^.\n]{0,60}organization id|organization id[^.\n]{0,40}(?:на|во) втором шаге', 'Claude Pro — sessionKey, не Organization ID (19.11)'),
    (r'(?i)автоактивац\w*[^.;\n]{0,40}claude', 'Claude активируется вручную (19.11)'),
    (r'(?i)промокод\w*\s+(?!NAVSEX|CHATIK10)[A-Z0-9]{4,}', 'только промокоды NAVSEX и CHATIK10'),
    (r'(?i)(?:уточните|подскажите)[^\n]{0,160}на какой срок|\\n\d\.\s*на какой срок', 'срок не спрашивать — по умолчанию месяц (20.11)'),
    (r'(?i)напишите[^.\n]{0,40}@neirosetkaalex', 'этот чат и есть @Neirosetkaalex (20.11)'),
    (r'(?i)официальн\w+ данн', 'без подписей «официальные данные»'),
    (r'(?i)водительск\w*\s+прав', 'Claude: только загранпаспорт, права убраны (20.11)'),
    (r'(?i)тарифы\s+SuperGrok[^.\n]{0,40}не\s+работают', 'Grok → тарифы Cursor (20.11)'),
    (r'(?i)выдаст\s+номер|номер\s+(?:может\s+)?выдаст\s+Александр|Александр\s+может\s+выдать', 'номер клиент покупает сам в «Виртуальных номерах» (20.11)'),
]


class KnowledgeConsistencyTests(unittest.TestCase):
    def test_no_old_rule_survives(self):
        problems = []
        for name in FILES:
            text = (DATA / name).read_text(encoding='utf-8')
            for pattern, rule in FORBIDDEN:
                for match in re.finditer(pattern, text):
                    line = text.count('\n', 0, match.start()) + 1
                    problems.append('%s:%d «%s» — нарушает: %s' % (name, line, match.group(0)[:60], rule))
        self.assertEqual(problems, [])

    def test_every_topic_has_title_and_answer(self):
        import yaml
        topics = yaml.safe_load((DATA / 'faq.yaml').read_text(encoding='utf-8'))['topics']
        ids = [t['id'] for t in topics]
        self.assertEqual(len(ids), len(set(ids)), 'повторяющиеся темы')
        owner_only = {'refund_terms', 'region_policy', 'linkpay_no_vpn', 'reseller', 'claude_refuses_account'}  # отвечает сам владелец
        for topic in topics:
            self.assertTrue(str(topic.get('title') or '').strip(), topic['id'])
            if topic['id'] not in owner_only:
                self.assertTrue(str(topic.get('answer') or '').strip(), topic['id'])

    def test_catalog_lists_agree(self):
        from support_agent.turn_understanding import OWNER_CATALOG
        profile = (DATA / 'business_profile.md').read_text(encoding='utf-8').casefold()
        names = {'chatgpt': 'chatgpt', 'claude': 'claude', 'supergrok': 'supergrok', 'zoom': 'zoom', 'perplexity': 'perplexity',
                 'kimi': 'kimi', 'capcut': 'capcut', 'cursor': 'cursor', 'gemini': 'gemini', 'midjourney': 'midjourney'}
        for key, word in names.items():
            self.assertIn(key, OWNER_CATALOG)
            self.assertIn(word, profile)
