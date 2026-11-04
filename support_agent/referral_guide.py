"""Ordinary referral policy verified against local shop code, not model memory.

No personal link/statistics are fabricated. Premium partner programmes are separate.
"""
import re


def referral_facts(facts):
    # Snapshot verified and owner confirmed 04.11.2026: config
    # REF_WELCOME_CREDITS and common.process_referral_bonus.
    return {
        'source':'verified_shop_code_2026_11_04',
        'programme':'ordinary_referral',
        'link_path':['Мой профиль','Пригласить друга'],
        'welcome_credits':150,
        'inviter_credit_range':[200,350],
        'credit_reward_condition':'first_paid_purchase_of_invited_friend',
        'coins_percent_first_purchase':10,
        'credits_valid_days':30,
        'coins_use':'purchases_in_shop',
        'personal_link_status':'not_read',
        'personal_statistics_status':'not_read',
        'premium_partner_terms':'separate_not_inferred',
        'owner_diagnostic':'Этап: сверка реферальных условий. Приветственный бонус подтверждён владельцем: 150 кредитов; config.REF_WELCOME_CREDITS=150. Условия начисления не изменены. Персональная ссылка, факт начисления и статистика не читались.',
    }


def referral_request(text):
    return bool(re.search(r'реферал|рефераль|пригласить\s+друга|приглаш[а-я]*\s+друз|'
                          r'ссылк[а-я]*\s+для\s+друз|бонус[а-я]*\s+за\s+друга|'
                          r'к[эе]шб[эе]к[^.!?\n]{0,35}(?:друг|приглаш)',str(text),re.I))


def referral_reply(facts,text):
    """Answer pure referral consultation; mixed service requests stay full pipeline."""
    text=str(text or '')
    if not referral_request(text):return None
    if re.search(r'премиум|партн[её]р|возврат|ошиб|не\s+начисл|не\s+приш|не\s+получ|где\s+мои|'
                 r'chat\s*gpt|чат\s*гпт|клод|claude|krea|zoom|подписк|'
                 r'себестоим|закуп|18\s*\+|порно|эрот',text,re.I):return None
    # Prevent a direct shortcut from swallowing additional service/payment goals.
    if re.search(r'также|заодно|ещ[её].*(?:тариф|оформ|оплат)|сколько.*(?:стоит|цен)',text,re.I):return None
    policy=referral_facts(facts)
    nav=facts.get('bot_navigation',{})
    username=nav.get('username','')
    if nav.get('status')=='available' and isinstance(username,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',username):
        where=f'В @{username} откройте «Мой профиль», затем «Пригласить друга». '
    else:
        where='В главном меню бота откройте «Мой профиль», затем «Пригласить друга». '
    body=(where+'Там находится ваша личная реферальная ссылка)\n\n'
          'Поделитесь ею с другом. При первом приходе по ссылке он получает 150 кредитов. '
          'После его первой покупки вам начисляются кредиты по уровню, от 200 до 350, '
          'и 10% от суммы этой покупки монетками.\n\n'
          'Кредиты используются для работы внутри бота, бонусные кредиты действуют 30 дней. '
          'Монетки можно тратить на покупки в боте. Это разные балансы. '
          'Для обычной реферальной программы бонус за покупку разовый, а не с каждой оплаты друга.')
    if re.search(r'(?:моя|мою|мне|пришли|дайте).{0,20}ссылк|сколько.{0,20}(?:заработал|пригласил)',text,re.I):
        body+='\n\nСвою ссылку и статистику посмотрите в этом разделе; личные данные рефералов здесь не проверялись.'
    return {'reply':body,
            'summary':'Объяснена обычная реферальная программа по коду магазина. Персональная ссылка, начисление и статистика не утверждались.',
            'handoff':False,'source_ids':[],
            'evidence':{'status':'catalog_direct','sources':[],
                        'diagnostic':policy['owner_diagnostic']}}
