"""Owner 24.11.2026: the evening review grades every answer of the day and proposes lessons."""
import asyncio
import json
import logging
import time
import unittest

from support_agent import stress_suite as ss
from support_agent import nightly_review as nr
from support_agent.service import Service
from support_agent.storage import Store


class _TG(ss._Telegram):
    async def send_text_file(self, chat_id, filename, text, caption=''):
        self.sent.append(('file', {'name': filename, 'text': text}))


class _AI(ss.StressModel):
    model = 'test'

    async def request(self, path, **kwargs):
        payload = json.loads(kwargs['json']['input'][0]['content'][0]['text'])
        self.instructions = kwargs['json']['instructions']
        items = []
        for item in payload:
            bad = 'сервис' in item['assistant']
            items.append({'id': item['id'], 'verdict': 'should_clarify' if bad else 'good',
                          'category': 'needless_question' if bad else 'none',
                          'problem': 'Клиент уже назвал ChatGPT' if bad else '',
                          'better_reply': 'Оформить ChatGPT можно в боте @neirosetkaa_bot: «Каталог» → ChatGPT → тариф.' if bad else '',
                          'lesson': 'Когда клиент уже назвал сервис, не переспрашивай его, а сразу давай шаги оформления.' if bad else ''})
        return {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({'items': items}, ensure_ascii=False)}]}]}


class NightlyReviewTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.store = Store(':memory:', batch_seconds=0)
        self.tg = _TG({})
        self.service = Service(self.store, self.tg, _AI(), ss._Shop(ss.shop_catalog_from_config()),
                               owner=1, allowed={1, 50, 51}, daily_limit=500, auto_for_all=True)
        self.store.connection('c1', True, True)
        now = time.time() - 3600
        for uid, mid, client, reply in ((50, 5, 'как оплатить гпт', 'Уточните, пожалуйста, о каком сервисе речь.'),
                                        (51, 7, 'сколько стоит суно', 'Suno Pro — 1000 ₽. Оплатить можно в боте @neirosetkaa_bot.')):
            cid = self.store.ingest('c1', uid, mid, 'client', client, {}, now)
            self.store.db.execute("INSERT INTO drafts(chat,mid,revision,global_revision,text,summary,handoff,created,state) VALUES (?,?,?,?,?,?,?,?,?)",
                                  (cid, mid, 0, 0, reply, '', 0, now + 5, 'sent'))

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_review_reports_and_proposes_lessons(self):
        asyncio.run(self.service.night_review(manual=True))
        texts = [kw.get('text', '') if isinstance(kw, dict) else kw for m, kw in self.tg.sent]
        summary = next(t for t in texts if 'Вечерний разбор' in str(t))
        self.assertIn('Проверено ответов: 2 из 2', summary)
        self.assertIn('нужно было уточнить — 1', summary)
        report = next(kw['text'] for m, kw in self.tg.sent if m == 'file')
        self.assertIn('Как надо было: Оформить ChatGPT', report)
        self.assertNotIn('Suno Pro', report)  # good answers are not in the file
        lessons = self.store.db.execute("SELECT text,state FROM lessons").fetchall()
        self.assertEqual([r['state'] for r in lessons], ['proposed'])
        self.assertIn('КАТАЛОГ', self.service.ai.instructions)

    def test_runs_once_in_the_evening(self):
        evening = __import__('datetime').datetime(2026, 11, 24, 23, 40, tzinfo=nr.LOCAL).timestamp()
        morning = __import__('datetime').datetime(2026, 11, 24, 9, 0, tzinfo=nr.LOCAL).timestamp()
        self.assertTrue(nr.due(evening, '2026-11-23'))
        self.assertFalse(nr.due(evening, '2026-11-24'))
        self.assertFalse(nr.due(morning, '2026-11-23'))


if __name__ == '__main__':
    unittest.main()
