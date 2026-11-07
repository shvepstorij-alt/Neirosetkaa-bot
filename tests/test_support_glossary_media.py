"""Glossary, photos, voice, several questions and the VPN rule through the understanding path."""
import asyncio
import io
import json
import tempfile
import time
import unittest

from PIL import Image

from support_agent.clients import OpenAI
from support_agent.dialog_probe import NoTelegram
from support_agent.glossary import for_context, load_glossary
from support_agent.handoff import HANDOFF
from support_agent.service import Service
from support_agent.storage import Store
from support_agent.turn_understanding import UNDERSTANDING_INSTRUCTIONS, VPN_SENTENCE, package


SHOP_KEYS = (
    'chatgpt', 'claude', 'grok', 'perplexity', 'cursor', 'lovable', 'midjourney', 'canva',
    'kling', 'runway', 'heygen', 'elevenlabs', 'suno', 'gamma', 'appstore', 'zoom', 'spotify',
    'capcut', 'freepik', 'genspark', 'github', 'higgsfield', 'kimi', 'krea', 'make', 'manus',
    'openrouter', 'telegram', 'youtube', 'z.ai', 'lovable',
)


def jpeg_bytes():
    buffer = io.BytesIO()
    Image.new('RGB', (16, 16), (250, 250, 250)).save(buffer, 'JPEG')
    return buffer.getvalue()


def shop_facts():
    catalog = [{'service': row['name'], 'service_key': row['id'], 'plan': 'Pro',
                'catalog_price_rub': 1000, 'fulfillment': 'account_details'}
               for row in load_glossary()['services']]
    return {'catalog': catalog, 'catalog_status': 'available',
            'appstore': {'status': 'available', 'regions': []},
            'bot_navigation': {'status': 'available', 'username': 'neirosetkaa_bot', 'catalog_webapp': True}}


def _fold(value):
    return ' '.join(str(value or '').split()).casefold().replace('ё', 'е')


def _mentioned(text, name):
    folded = _fold(text)
    token = _fold(name)
    return bool(token) and (folded == token or (' ' + token + ' ') in (' ' + folded + ' '))


class GlossaryModel:
    """Uses only the glossary the service put in the payload."""

    def __init__(self):
        self.payloads = []
        self.images = []
        self.transcript = 'сколько стоит суно'
        self.calls = 0

    async def transcribe(self, data):
        return self.transcript

    async def understand_turn(self, context, images=None):
        self.calls += 1
        self.payloads.append(context)
        self.images.append(images)
        burst = context.get('burst') or ''
        glossary = context.get('glossary') or {}
        if images:
            return self._pack('other', '', 'На скриншоте ошибка входа. Откройте сервис ещё раз.',
                              'Разбор скрина. Оплата не подтверждена.', questions=['Что на скриншоте?'])
        if 'работает ли' in _fold(burst):
            return self._pack('other', 'chatgpt', 'В России сервис открывается не всегда.',
                              'Вопрос про доступ.', vpn=True, questions=['Работает ли сервис в России?'])
        if 'и как' in _fold(burst):
            return self._pack('how_to', 'zoom', '1. Zoom Pro есть в каталоге.\n2. Оплата Zoom Pro идёт в боте.',
                              'Два вопроса.', questions=['Сколько стоит Zoom Pro?', 'Как оплатить Zoom Pro?'])
        if 'только цена' in _fold(burst):
            return self._pack('price', 'zoom', 'Zoom Pro есть в каталоге.',
                              'Второй вопрос без своего пункта.',
                              questions=['Сколько стоит Zoom Pro?', 'Как оплатить Zoom Pro?'])
        exact = []
        partial = []
        for row in glossary.get('services') or []:
            names = [row.get('name'), row.get('id'), *(row.get('aliases') or [])]
            if any(_fold(burst) == _fold(name) for name in names):
                exact.append(row)
            elif any(_mentioned(burst, name) for name in names):
                partial.append(row)
        for phrase in glossary.get('phrases') or []:
            if _fold(burst) == _fold(phrase.get('phrase')):
                service = next((row for row in glossary.get('services') or [] if row['id'] == phrase['service']), None)
                if service and service not in exact:
                    exact.append(service)
        matches = exact or partial
        if len(matches) == 1:
            row = matches[0]
            return self._pack('price', row['id'], 'Речь о ' + row['name'] + '. Могу подсказать цену и оформление.',
                              'Сервис понятен по глоссарию.', questions=[burst])
        options = [row['id'] for row in (glossary.get('services') or [])[:3]]
        return self._pack('other', '', 'Похоже на один из сервисов.', 'Нужно уточнение.',
                          confidence='low', clarify=options, questions=[burst])

    def _pack(self, intent, service_id, reply, summary, questions=None, vpn=False, confidence='high', clarify=None):
        return {'intent': intent, 'service_id': service_id, 'tariff_id': '',
                'confidence': confidence, 'clarify_ids': clarify or [],
                'topic_changed': False, 'known_facts': [], 'missing_facts': [],
                'questions': questions or [], 'vpn_advice': vpn, 'risk': 'instruct',
                'order_number': '', 'receipt_amount_unverified': '',
                'reply': reply, 'summary': summary}


class GlossaryMediaTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(suffix='.sqlite')
        self.store = Store(self.file.name, batch_seconds=0)
        self.store.put('mode', 'auto')
        self.store.connection('biz', True, True)
        self.model = GlossaryModel()
        self.picture = jpeg_bytes()
        self.tg = NoTelegram({'screen-1': self.picture, 'voice-1': b'OggS' + b'\x00' * 32})
        self.service = Service(self.store, self.tg, self.model, _Shop(), owner=1, allowed={1, 50}, daily_limit=500)
        self.mid = 0

    def tearDown(self):
        self.file.close()

    def ask(self, text, media=None):
        self.mid += 1
        now = time.time()
        cid = self.store.ingest('biz', 50, self.mid, 'client', text, media or {}, now)
        self.store.db.execute("UPDATE chats SET mode='auto' WHERE id=?", (cid,))
        self.store.prepare_now(cid, now)
        job = self.store.claim(now + 1)
        answer = asyncio.run(self.service.generate(job))
        self.store.db.execute("UPDATE messages SET state='done' WHERE chat=? AND mid=?", (cid, job['mid']))
        self.mid += 1
        self.store.ingest('biz', 50, self.mid, 'assistant', 'ок', {}, now)
        return answer

    def test_every_alias_is_context_for_the_model(self):
        loaded = load_glossary()
        covered = set()
        for row in loaded['services']:
            covered.add(row['id'].casefold())
            covered.update(_fold(alias) for alias in row['aliases'])
        for key in SHOP_KEYS:
            self.assertIn(key.casefold(), covered, key)
        seen = []
        for row in loaded['services']:
            for alias in row['aliases']:
                answer = self.ask(alias)
                seen.append(alias)
                self.assertIn('glossary', self.model.payloads[-1])
                self.assertEqual(answer['evidence']['turn_understanding']['service_id'], row['id'], alias)
                self.assertIn(row['name'], answer['reply'])
                self.assertFalse(answer['handoff'], alias)
        phrase = loaded['phrases'][0]
        named = self.ask(phrase['phrase'])
        self.assertEqual(named['evidence']['turn_understanding']['service_id'], phrase['service'])
        self.assertGreater(len(seen), 40)

    def test_glossary_is_not_a_router(self):
        context = {'burst': 'гпт', 'glossary': for_context(shop_facts()),
                   'tariffs': [{'id': 'chatgpt|Plus', 'service_id': 'chatgpt', 'service': 'ChatGPT',
                                'plan': 'Plus', 'price': 1990}],
                   'nominals': []}
        answer = package({'intent': 'price', 'service_id': '', 'tariff_id': '', 'confidence': 'high',
                          'reply': 'Уточните, пожалуйста, какой сервис смотрим?', 'summary': 'Сервис не назван.',
                          'questions': [], 'vpn_advice': False}, context)
        self.assertEqual(answer['evidence']['turn_understanding']['service_id'], '')

    def test_low_confidence_asks_for_concrete_options(self):
        answer = self.ask('что-то похожее')
        self.assertFalse(answer['handoff'])
        self.assertEqual(answer['evidence']['turn_understanding']['service_id'], '')
        self.assertIn('уточните', answer['reply'].casefold())
        self.assertIn('ChatGPT', answer['reply'])
        self.assertIn('Claude', answer['reply'])
        self.assertIn('SuperGrok', answer['reply'])
        self.assertIn('?', answer['reply'])

    def test_two_questions_need_two_points(self):
        answered = self.ask('сколько стоит зум и как оплатить')
        self.assertFalse(answered['handoff'])
        self.assertIn('1.', answered['reply'])
        self.assertIn('2.', answered['reply'])
        self.assertIn('Zoom', answered['reply'])
        skipped = self.ask('только цена зума')
        self.assertTrue(skipped['handoff'])

    def test_error_photo_is_part_of_understanding(self):
        before = self.store.calls_today()
        answer = self.ask('вот ошибка', {'kind': 'photo', 'file_id': 'screen-1', 'file_size': len(self.picture)})
        self.assertEqual(len(self.model.images[-1]), 1)
        self.assertIn('скриншоте', answer['reply'])
        self.assertFalse(answer['handoff'])
        self.assertEqual(self.store.calls_today(), before + 1)

    def test_voice_is_transcribed_and_filtered(self):
        before = self.store.calls_today()
        answer = self.ask('', {'kind': 'voice', 'file_id': 'voice-1', 'file_size': 36, 'duration': 4})
        self.assertEqual(answer['evidence']['turn_understanding']['service_id'], 'suno')
        self.assertIn('суно', self.model.payloads[-1]['burst'])
        self.assertEqual(self.store.calls_today(), before + 2)
        self.model.transcript = '4111111111111111'
        self.ask('', {'kind': 'voice', 'file_id': 'voice-1', 'file_size': 36, 'duration': 4})
        self.assertNotIn('4111111111111111', self.model.payloads[-1]['burst'])
        stored = self.store.db.execute('SELECT text, media FROM messages WHERE role=?', ('client',)).fetchall()
        blob = '\n'.join((row['text'] or '') + (row['media'] or '') for row in stored)
        self.assertNotIn('4111111111111111', blob)
        calls = self.model.calls
        self.model.transcript = 'пароль: qwerty12'
        hidden = self.ask('', {'kind': 'voice', 'file_id': 'voice-1', 'file_size': 36, 'duration': 4})
        self.assertEqual(self.model.calls, calls)
        self.assertTrue(hidden['handoff'])
        self.assertNotIn('qwerty12', hidden['reply'])

    def test_country_access_adds_the_vpn_rule(self):
        self.assertIn('VPN', UNDERSTANDING_INSTRUCTIONS)
        self.assertIn('одна и та же локация', UNDERSTANDING_INSTRUCTIONS)
        answer = self.ask('работает ли в РФ')
        self.assertIn(VPN_SENTENCE, answer['reply'])
        self.assertFalse(answer['handoff'])
        self.assertFalse(answer['reply'].startswith(HANDOFF))

    def test_daily_limit_counts_transcription_before_the_reply(self):
        self.service.daily_limit = 1
        answer = self.ask('', {'kind': 'voice', 'file_id': 'voice-1', 'file_size': 36, 'duration': 4})
        self.assertTrue(answer['handoff'])
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.store.calls_today(), 1)


class _Shop:
    async def facts(self, uid):
        return shop_facts()


class VisionRequestTests(unittest.TestCase):
    def test_understanding_call_carries_the_image(self):
        client = OpenAI(None, 'test-key', 'gpt-4.1-mini', 'gpt-4o-mini-transcribe')
        seen = {}

        async def request(path, **kwargs):
            seen['path'] = path
            seen['body'] = kwargs.get('json')
            payload = {'intent': 'other', 'service_id': '', 'tariff_id': '', 'confidence': 'high',
                       'clarify_ids': [], 'topic_changed': False, 'known_facts': [], 'missing_facts': [],
                       'questions': ['Что на скрине?'], 'vpn_advice': False, 'risk': 'instruct',
                       'order_number': '', 'receipt_amount_unverified': '',
                       'reply': 'На скриншоте ошибка входа.', 'summary': 'Скрин.'}
            return {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(payload)}]}]}

        client.request = request
        asyncio.run(client.understand_turn({'burst': 'ошибка'}, images=[{'mid': 3, 'caption': '', 'data': jpeg_bytes()}]))
        content = seen['body']['input'][0]['content']
        self.assertEqual(seen['body']['model'], 'gpt-4.1-mini')
        self.assertTrue(any(part.get('type') == 'input_image' and part['image_url'].startswith('data:image/jpeg;base64,')
                            for part in content))
        self.assertNotIn('test-key', json.dumps(seen['body']))


if __name__ == '__main__':
    unittest.main()
