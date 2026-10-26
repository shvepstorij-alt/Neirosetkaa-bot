"""Keep reminder messages attached to the last client question, never a model answer."""
import re


def is_reminder(text):
    lines=[line.strip() for line in str(text).splitlines() if line.strip()]
    if len(lines)>1:
        return (any(is_reminder(line) for line in lines)
                and all(is_reminder(line) or is_acknowledgement(line) for line in lines))
    text=re.sub(r'[\s!?.…,]+',' ',str(text).casefold()).strip()
    return bool(re.fullmatch(
        r'(?:ну |хорошо |да |спасибо )?(?:я )?(?:жду(?: вашего| твоего| ваш| твой)?(?: ответа| ответ)?|'
        r'ожидаю(?: ответа| ответ)?|когда (?:будет )?ответ|вы тут|вы здесь|есть ответ|'
        r'ответьте(?: пожалуйста)?|жду ответ пожалуйста)',text))


def is_acknowledgement(text):
    text=re.sub(r'[\s!?.…,]+',' ',str(text).casefold()).strip()
    return text in ('спасибо','хорошо','понял','поняла','ок','окей','да','ладно')


def resolve_request(db, chat, first, last, text):
    """Persist only message references. Deleted/expired content is not resurrected.

    Older deployments have no pointer: use the most recent draft's client batch,
    bounded by the preceding draft. Never use generated draft text as evidence.
    """
    db.execute('CREATE TABLE IF NOT EXISTS request_context (chat INTEGER PRIMARY KEY, first_mid INTEGER, last_mid INTEGER)')
    if is_acknowledgement(text):return text
    if not is_reminder(text):
        db.execute('INSERT OR REPLACE INTO request_context VALUES (?,?,?)',(chat,first,last))
        return text
    pointer=db.execute('SELECT first_mid,last_mid FROM request_context WHERE chat=?',(chat,)).fetchone()
    if pointer:
        bounds=(pointer['first_mid'],pointer['last_mid'])
    else:
        drafts=db.execute('SELECT DISTINCT mid FROM drafts WHERE chat=? AND mid<? ORDER BY mid DESC LIMIT 2',(chat,first)).fetchall()
        if not drafts:return ''
        bounds=(drafts[1]['mid']+1 if len(drafts)>1 else drafts[0]['mid'],drafts[0]['mid'])
    rows=db.execute("SELECT text FROM messages WHERE chat=? AND role='client' AND mid BETWEEN ? AND ? ORDER BY mid LIMIT 61",(chat,*bounds)).fetchall()
    if len(rows)>60:return ''
    parts=[r['text'] for r in rows if r['text'] and not is_reminder(r['text'])]
    previous='\n'.join(parts)
    if not previous or len(previous)>24000:return ''
    db.execute('INSERT OR REPLACE INTO request_context VALUES (?,?,?)',(chat,*bounds))
    return previous
