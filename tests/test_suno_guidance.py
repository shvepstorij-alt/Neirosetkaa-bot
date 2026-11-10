import unittest

from support_agent.suno_guidance import reply


class SunoGuidanceTests(unittest.TestCase):
    def test_audio_and_lyrics_question_gets_safe_workflow(self):
        answer=reply('Можно в Suno загрузить свою мелодию, голос и написать текст?')
        self.assertIn('Custom',answer['reply'])
        self.assertIn('есть права',answer['reply'])
        self.assertNotIn('стоит',answer['reply'].casefold())

    def test_price_question_is_not_claimed(self):
        self.assertIsNone(reply('Сколько стоит Suno?'))

    def test_mixed_price_and_audio_question_keeps_catalog_priority(self):
        self.assertIsNone(reply('Сколько стоит Suno и можно ли загрузить мелодию?'))


if __name__=='__main__':
    unittest.main()
