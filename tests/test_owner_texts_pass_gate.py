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

    def test_every_paid_notice_passes(self):
        """Owner 26.11.2026: the after-payment step of every service goes out by itself."""
        services = [('ChatGPT', 'Plus'), ('ChatGPT', 'Pro 5×'), ('ChatGPT', 'Новый аккаунт с подпиской Plus'),
                    ('Пополнение кредитов ChatGPT', '500'), ('Claude', 'Pro'), ('Claude', 'Ultra'), ('Perplexity Pro', 'Pro'),
                    ('Gemini', 'Pro'), ('SuperGrok', 'Lite'), ('Midjourney', 'Basic'), ('Suno', 'Pro'), ('Gamma', 'Pro'),
                    ('Zoom', 'Pro'), ('Spotify', 'Premium'), ('YouTube', 'Premium'), ('Notion', 'Plus'), ('Freepik', 'Premium'),
                    ('Krea', 'Pro'), ('GitHub', 'Pro'), ('OpenRouter', '10'), ('Cursor', 'Plus'), ('Canva Pro', 'Pro'),
                    ('Kimi', 'Plus'), ('Make', 'Core'), ('Kling AI', 'Pro'), ('Runway', 'Standard'), ('HeyGen', 'Creator'),
                    ('ElevenLabs', 'Creator'), ('Lovable Pro', 'Pro'), ('Higgsfield AI', 'Plus'), ('Genspark', 'Plus'),
                    ('Manus', 'Pro'), ('Z.ai', 'Lite'), ('Telegram Premium', '12'), ('CapCut', 'Pro'),
                    ('App Store / iCloud', 'Россия 1000'), ('Другой сервис', '$20')]
        for name, plan in services:
            with self.subTest(service=name):
                kind, text = paid_notice.compose({'order': '11900', 'service': name, 'plan': plan, 'fk': '350902411'},
                                                 [{'service_id': name.split()[0].lower(), 'service': name, 'plan': plan, 'price': 1000}])
                self.assertIsNone(owner_gate({'reply': pipe(text), 'handoff': False}, {}))

    def test_receipts_after_full_pipeline(self):
        """Owner 28.11.2026: «данные получил» replies pass after greeting and dash restyling."""
        from support_agent.media_batch import SAFE_RECEIPTS
        from support_agent.turn_understanding import ACCOUNT_SETUP_RECEIVED
        for text in (ACCOUNT_SETUP_RECEIVED, DATA_RECEIVED) + tuple(SAFE_RECEIPTS):
            with self.subTest(text=text[:30]):
                self.assertIsNone(owner_gate({'reply': pipe(text), 'handoff': True}, {'manual_secret_intake': True}))


if __name__ == '__main__':
    unittest.main()
