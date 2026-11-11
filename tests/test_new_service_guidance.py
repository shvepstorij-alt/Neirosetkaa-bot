import unittest

from support_agent.gamma_guidance import reply as gamma
from support_agent.capcut_guidance import reply as capcut
from support_agent.chatgpt_image_guidance import reply as chatgpt_images


class NewServiceGuidanceTests(unittest.TestCase):
    def test_gamma_presentation_draft(self):
        answer=gamma('Какая нейросеть поможет собрать информацию в презентацию и оформить текст?')
        self.assertIn('Gamma',answer['reply'])
        self.assertIn('проверьте факты',answer['reply'])

    def test_gamma_price_does_not_bypass_catalogue(self):
        self.assertIsNone(gamma('Сколько стоит Gamma для презентаций?'))
        self.assertIsNone(gamma('Как войти в Gamma для презентации?'))

    def test_capcut_captions_for_existing_reel(self):
        answer=capcut('В уже снятом рилс можно добавить субтитры?')
        self.assertIn('CapCut',answer['reply'])
        self.assertIn('вручную проверьте',answer['reply'])

    def test_capcut_subscription_is_not_claimed(self):
        self.assertIsNone(capcut('Какой тариф CapCut выбрать для субтитров?'))
        self.assertIsNone(capcut('Можно в CapCut полностью ИИ сделать ролик с нуля?'))
        self.assertIsNone(capcut('Не приходит код входа в CapCut для субтитров'))

    def test_chatgpt_character_from_reference(self):
        answer=chatgpt_images('Можно в ChatGPT создать персонажа из своего фото в стиле комикса?')
        self.assertIn('ChatGPT Images',answer['reply'])
        self.assertIn('разрешением',answer['reply'])

    def test_chatgpt_images_price_is_not_claimed(self):
        self.assertIsNone(chatgpt_images('Сколько стоит ChatGPT для создания персонажа из фото?'))
        self.assertIsNone(chatgpt_images('Можно в вашем боте создать персонажа из фото?'))
        self.assertIsNone(chatgpt_images('Можно в ChatGPT создать персонажа из чужого фото?'))
        self.assertIsNone(chatgpt_images('Можно в ChatGPT сделать персонажа из фото друга?'))


if __name__=='__main__':
    unittest.main()
