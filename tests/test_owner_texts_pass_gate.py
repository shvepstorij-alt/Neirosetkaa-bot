"""Owner 25.11.2026: every answer the owner wrote must pass the AUTO gate after the full outgoing pipeline."""
import unittest
import yaml

from support_agent.auto_policy import owner_gate
from support_agent.service import present_outgoing
from support_agent.catalog_order import sort_price_lines
from support_agent.voice import masculine, trim_filler
from support_agent.repeat_guard import dedupe, DATA_RECEIVED, CLAUDE_PICK
from support_agent import course_sheets, media_batch, paid_notice


def pipe(text):
    answer, _ = present_outgoing({'reply': text, 'handoff': False, 'summary': '', 'evidence': {}}, {}, introduced=False, facts={})
    return dedupe(trim_filler(masculine(sort_price_lines(answer['reply']))), [])


class OwnerTextsPassGate(unittest.TestCase):
    def test_all(self):
        cases = {'eligible': course_sheets.ELIGIBLE, 'not_eligible': course_sheets.NOT_ELIGIBLE,
                 'not_found': course_sheets.NOT_FOUND, 'data': DATA_RECEIVED, 'pick': CLAUDE_PICK,
                 'token_file': media_batch.TOKEN_AS_FILE, 'token_manual': paid_notice.TOKEN_STEPS_MANUAL.format(),
                 'token_auto': paid_notice.TOKEN_STEPS_AUTO.format()}
        for row in yaml.safe_load(open('support_agent/data/faq.yaml', encoding='utf-8'))['topics']:
            if row.get('answer'):
                cases['faq:' + row['id']] = row['answer']
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assertIsNone(owner_gate({'reply': pipe(text), 'handoff': False}, {}))

    def test_real_secret_still_stops(self):
        self.assertIsNotNone(owner_gate({'reply': 'Логин: user@mail.ru пароль: Qwerty12345!', 'handoff': False}, {}))


if __name__ == '__main__':
    unittest.main()
