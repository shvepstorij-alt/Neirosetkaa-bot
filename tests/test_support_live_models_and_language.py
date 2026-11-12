"""Owner 12.11.2026: current models from official sites; foreign clients in their language."""
import asyncio
import types
import unittest

from support_agent.faq import usable_answer
from support_agent.turn_understanding import UNDERSTANDING_INSTRUCTIONS, foreign_language, research_request


class _Store:
    db = None

    def still_current(self, job):
        return True


def _service(ai):
    from support_agent.service import Service
    fake = types.SimpleNamespace(ai=ai, store=_Store(), charge_call=lambda reason='model': None)
    fake._in_client_language = types.MethodType(Service._in_client_language, fake)
    fake._attach_official_research = types.MethodType(Service._attach_official_research, fake)
    return fake


class ModelsTests(unittest.TestCase):
    def test_model_questions_trigger_an_official_lookup(self):
        self.assertEqual(research_request({'burst': 'какие модели сейчас в чате гпт и клоде?'}),
                         {'topic': 'models', 'products': ['chatgpt', 'claude']})
        self.assertEqual(research_request({'burst': 'а какая версия?', 'dialog_state': {'service_id': 'grok'}}),
                         {'topic': 'models', 'products': ['supergrok']})
        self.assertIsNone(research_request({'burst': 'как оплатить клод?'}))
        self.assertIn('official_research', UNDERSTANDING_INSTRUCTIONS)

    def test_lookup_result_and_failure_reach_the_context(self):
        import support_agent.research as research

        async def ok(ai, plan, **kwargs):
            return {'checked_at': 1794470400, 'digest': 'В Plus доступна модель X [1]',
                    'sources': [{'id': 1, 'url': 'https://openai.com/x'}]}

        async def broken(ai, plan, **kwargs):
            raise ValueError('search failed')

        original = research.lookup
        try:
            research.lookup = ok
            context = {'burst': 'какие модели в чате гпт?'}
            asyncio.run(_service(object())._attach_official_research(context))
            self.assertEqual(context['official_research']['status'], 'checked')
            self.assertIn('openai.com', context['official_research']['sources'][0]['url'])
            research.lookup = broken
            context = {'burst': 'какие модели в чате гпт?'}
            asyncio.run(_service(object())._attach_official_research(context))
            self.assertEqual(context['official_research']['status'], 'unavailable')
            context = {'burst': 'как оплатить?'}
            asyncio.run(_service(object())._attach_official_research(context))
            self.assertNotIn('official_research', context)
        finally:
            research.lookup = original


class LanguageTests(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(foreign_language('Hello, how can I buy ChatGPT Plus?'))
        self.assertTrue(foreign_language('gpt plus new acc sell ?'))
        self.assertFalse(foreign_language('ChatGPT Plus'))
        self.assertFalse(foreign_language('как оплатить plus'))

    def test_reply_is_translated_and_facts_are_kept(self):
        class AI:
            async def translate_reply(self, reply, client_text):
                return 'Payment in @neirosetkaa_bot: 1990 ₽, promo NAVSEX.'

        answer = {'reply': 'Оплата в @neirosetkaa_bot: 1990 ₽, промокод NAVSEX.', 'handoff': False, 'evidence': {}}
        out = asyncio.run(_service(AI())._in_client_language({'chat': 1}, answer, {'burst': 'How do I pay for ChatGPT?'}))
        self.assertTrue(out['reply'].startswith('Payment'))
        self.assertFalse(out['handoff'])

    def test_lost_link_or_failure_goes_to_the_owner(self):
        class Lossy:
            async def translate_reply(self, reply, client_text):
                return 'Payment in the bot.'

        class Broken:
            async def translate_reply(self, reply, client_text):
                raise ValueError('down')

        answer = {'reply': 'Оплата в @neirosetkaa_bot.', 'handoff': False, 'evidence': {}}
        for ai in (Lossy(), Broken()):
            out = asyncio.run(_service(ai)._in_client_language({'chat': 1}, answer, {'burst': 'How do I pay for ChatGPT?'}))
            self.assertTrue(out['handoff'])

    def test_russian_client_is_untouched(self):
        answer = {'reply': 'Оплата в боте.', 'handoff': False, 'evidence': {}}
        out = asyncio.run(_service(object())._in_client_language({'chat': 1}, answer, {'burst': 'как оплатить?'}))
        self.assertIs(out, answer)


class OwnerRules12_11bTests(unittest.TestCase):
    def test_phone_vpn_abroad_app_update(self):
        self.assertIn('Александр', usable_answer('phone_number'))
        self.assertIn('не нужен', usable_answer('vpn_abroad'))
        self.assertIn('просит обновиться', usable_answer('app_download'))


if __name__ == '__main__':
    unittest.main()
