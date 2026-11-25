"""Owner 25.11.2026: course bonus — check the client's course e-mail in the owner's Google Sheets.

The sheets are read by the owner's Google Apps Script web app (see course_sheets_apps_script.gs):
the bot sends the e-mail and a secret token, the script answers with the row it found.
A tick in the «Подписка» / «Подписка Codex» column = the bonus month is due.
Nothing is stored: the e-mail lives only in memory for the time of the check.
"""
import json
import logging
import os
import re
import time

EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
_MEMORY = {}  # chat id → (e-mail, time); never written to the database
_TTL = 3600


def remember(chat, raw):
    match = EMAIL.search(str(raw or ''))
    if match:
        _MEMORY[chat] = (match.group(0).strip('.').casefold(), time.time())


def recall(chat):
    value = _MEMORY.get(chat)
    if value and time.time() - value[1] < _TTL:
        return value[0]
    return ''


def configured():
    return bool(os.getenv('COURSE_SHEETS_URL') and os.getenv('COURSE_SHEETS_TOKEN'))


async def check(email):
    """{'status': eligible|not_eligible|not_found|error|off, 'sheet_url', 'sheet_name', 'tab', 'row', 'tariff', 'curator', 'name'}"""
    if not configured():
        return {'status': 'off'}
    if not EMAIL.fullmatch(str(email or '')):
        return {'status': 'error', 'reason': 'нет почты'}
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            async with session.get(os.environ['COURSE_SHEETS_URL'],
                                   params={'email': email, 'token': os.environ['COURSE_SHEETS_TOKEN']}) as response:
                data = json.loads(await response.text())
    except Exception as exc:
        logging.warning('Course sheet check failed: %s', type(exc).__name__)
        return {'status': 'error', 'reason': type(exc).__name__}
    if not isinstance(data, dict) or data.get('error'):
        return {'status': 'error', 'reason': str((data or {}).get('error') or 'bad answer')[:100]}
    if not data.get('found'):
        return {'status': 'not_found'}
    keep = {key: str(data.get(key) or '')[:200] for key in ('sheet_url', 'sheet_name', 'tab', 'row', 'tariff', 'curator', 'name')}
    return dict(keep, status='eligible' if data.get('eligible') is True else 'not_eligible')


ELIGIBLE = (
    'Проверил — бонусная подписка на месяц вам положена 🎉\n\n'
    'Можно выбрать один сервис:\n\n'
    '• **ChatGPT Plus** — подписка на ваш аккаунт ChatGPT. Для оформления понадобится токен аккаунта:\n'
    '1. Войдите на chatgpt.com в браузере.\n'
    '2. В том же браузере откройте chatgpt.com/api/auth/session\n'
    '3. Выделите весь текст (Ctrl+A) и скопируйте (Ctrl+C).\n'
    '4. Вставьте его в Блокнот, сохраните как .txt и пришлите сюда одним файлом.\n\n'
    '• **Claude Pro** — *важно знать заранее:* нужна верификация личности (подходят иностранные документы, из российских — '
    'только загранпаспорт РФ, проходит примерно в половине случаев); Claude может заблокировать аккаунт, '
    'гарантировать это не можем, и при блокировке бонус не восстанавливается. Если выбираете Claude, напишите '
    '«Claude, согласен(на) с рисками» — Александр продолжит оформление.\n\n'
    'Напишите, что выбираете 🙂')

NOT_ELIGIBLE = (
    'Проверил по почте: в списке курса бонусная подписка для вас пока не отмечена, поэтому оформить бонусный месяц '
    'сейчас не получится.\n\n'
    'Пожалуйста, уточните этот момент у поддержки курса. Если подписку вам одобрят, напишите сюда ещё раз — '
    'проверю снова.')

NOT_FOUND = (
    'Эту почту не нашёл в списках курса. Проверьте, пожалуйста, что это именно почта, на которую вы регистрировались '
    'на курсе, и пришлите её ещё раз. Если почта верная, уточните у поддержки курса, внесли ли вас в список.')


def owner_card(identity, email, result):
    import html
    esc = lambda v: html.escape(str(v or ''), quote=False)
    status = result.get('status')
    head = {'eligible': '✅ бонус ПОЛОЖЕН — клиенту отправлен выбор ChatGPT / Claude',
            'not_eligible': '❌ бонус НЕ положен — клиенту отказано, отправлен к поддержке курса',
            'not_found': '❓ почта не найдена в таблицах — клиента попросили проверить почту',
            'error': '⚠️ таблицы не прочитались — проверьте вручную',
            'off': '⚠️ проверка по таблицам не подключена — проверьте вручную'}.get(status, '⚠️ проверьте вручную')
    lines = [esc(identity), '🎓 <b>КЛИЕНТ С КУРСА</b>', '━━━━━━━━━━━━', head,
             '📧 Почта: <code>' + esc(email or 'не распознана, см. переписку') + '</code>']
    if result.get('name'):
        lines.append('👤 Участник: ' + esc(result['name']))
    if result.get('tariff'):
        lines.append('🏷 Тариф курса: <b>' + esc(result['tariff']) + '</b>')
    if result.get('curator'):
        lines.append('🧑‍🏫 Куратор: ' + esc(result['curator']))
    if result.get('sheet_url'):
        lines.append('📊 Таблица: <a href="' + esc(result['sheet_url']) + '">' + esc(result.get('sheet_name') or 'открыть') + '</a>'
                     + (' · лист «' + esc(result['tab']) + '»' if result.get('tab') else '')
                     + (' · строка ' + esc(result['row']) if result.get('row') else ''))
    if result.get('reason'):
        lines.append('<i>' + esc(result['reason']) + '</i>')
    return '\n'.join(lines)
