import time
import unittest

from support_agent.app_installation import reply as installation_reply
from support_agent.claude_policy import (
    PURCHASE_DISCLOSURE_MARKER,
    claude_purchase_disclosure,
    public_region_issue,
)


class ClaudePurchaseDisclosureTests(unittest.TestCase):
    def setUp(self):
        self.facts={'requested_catalog':[{'service':'Claude','service_key':'claude'}]}

    def test_purchase_is_gated_before_price_or_payment_help(self):
        answer=claude_purchase_disclosure(self.facts,'Хочу оформить Claude Pro',[])
        self.assertIn(PURCHASE_DISCLOSURE_MARKER,answer['reply'])
        self.assertIn('не можем гарантировать',answer['reply'])
        self.assertNotIn('1990',answer['reply'])
        self.assertIsNone(public_region_issue(answer['reply'],self.facts))

    def test_price_question_is_also_gated(self):
        answer=claude_purchase_disclosure(self.facts,'Сколько стоит Claude Pro?',[])
        self.assertIn(PURCHASE_DISCLOSURE_MARKER,answer['reply'])

    def test_acceptance_releases_the_next_purchase_step(self):
        history=[{'role':'assistant','text':PURCHASE_DISCLOSURE_MARKER}]
        self.assertIsNone(claude_purchase_disclosure(self.facts,'Согласен, хочу Pro',history))

    def test_app_download_is_not_replaced_with_purchase_warning(self):
        self.assertIsNone(claude_purchase_disclosure(self.facts,'Как скачать Claude на Android?',[]))


class MultipleApplicationInstallTests(unittest.TestCase):
    def setUp(self):
        now=time.time()
        self.history=[
            {'mid':1,'role':'client','text':'А через вас можно скачать приложение?','at':now-10},
            {'mid':2,'role':'assistant','text':'Какое приложение хотите установить?','at':now-5},
        ]

    def test_two_apps_continue_installation_instead_of_tariff_selection(self):
        answer=installation_reply({'catalog':[]},'Чата Гпт и Клода',self.history)
        self.assertIn('ChatGPT и Claude',answer['reply'])
        self.assertIn('Android',answer['reply'])
        self.assertNotIn('тариф',answer['reply'].casefold())

    def test_generic_repeat_keeps_multiple_app_platform_question(self):
        history=self.history+[{'mid':3,'role':'assistant',
                               'text':'Понял, нужна установка ChatGPT и Claude) На чём хотите установить: Android, iPhone или компьютер?',
                               'at':time.time()-1}]
        answer=installation_reply({'catalog':[]},'Мне нужно скачать приложение',history)
        self.assertIn('ChatGPT и Claude',answer['reply'])
        self.assertNotIn('Какое приложение хотите установить?',answer['reply'])

    def test_platform_after_multiple_app_question_keeps_both_apps(self):
        history=self.history+[{'mid':3,'role':'assistant',
                               'text':'Понял, нужна установка ChatGPT и Claude) На чём хотите установить: Android, iPhone или компьютер?',
                               'at':time.time()-1}]
        answer=installation_reply({'catalog':[]},'Айфон',history)
        self.assertIn('App Store',answer['reply'])
        self.assertIn('Claude',answer['reply'])
        self.assertNotIn('тариф',answer['reply'].casefold())

    def test_explicit_tariff_question_is_not_claimed_by_installation(self):
        self.assertIsNone(installation_reply({'catalog':[]},'Какие тарифы у ChatGPT и Claude?',self.history))


if __name__=='__main__':
    unittest.main()
