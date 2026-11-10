import unittest

from support_agent.app_installation import MIDJOURNEY, reply


class MidjourneyInstallationTests(unittest.TestCase):
    def test_app_question_uses_official_web_and_discord_without_claiming_no_app(self):
        answer=reply({'catalog':[]},'Есть приложение Midjourney?')
        self.assertIn(MIDJOURNEY,answer['reply'])
        self.assertIn('Discord',answer['reply'])
        self.assertIn('неофициального источника',answer['reply'])
        self.assertNotIn('нет приложения',answer['reply'].casefold())

    def test_install_wording_uses_same_official_route(self):
        answer=reply({'catalog':[]},'Как установить Midjourney?')
        self.assertIn(MIDJOURNEY,answer['reply'])

    def test_tariff_question_is_not_claimed_as_installation(self):
        self.assertIsNone(reply({'catalog':[]},'Сколько стоит Midjourney?'))


if __name__=='__main__':
    unittest.main()
