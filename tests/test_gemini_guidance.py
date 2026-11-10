import unittest

from support_agent.gemini_guidance import reply


class GeminiGuidanceTests(unittest.TestCase):
    def test_image_question_gets_only_safe_navigation(self):
        answer=reply('В Gemini можно сделать и отредактировать фото?')
        self.assertIn('создавать и редактировать изображения',answer['reply'])
        self.assertIn('загрузить изображение',answer['reply'])
        self.assertIn('зависит от страны',answer['reply'])

    def test_comparison_is_not_overridden(self):
        self.assertIsNone(reply('Gemini лучше ChatGPT для фотографий?'))

    def test_plan_and_limits_are_not_claimed(self):
        self.assertIsNone(reply('Сколько стоит Gemini и какой там лимит на фото?'))

    def test_model_and_other_service_comparisons_are_not_claimed(self):
        self.assertIsNone(reply('Какая модель Gemini лучше для фото?'))
        self.assertIsNone(reply('Gemini vs Claude для генерации картинок?'))


if __name__=='__main__':
    unittest.main()
