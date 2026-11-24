"""Session-aware customer presentation, independent of reconstructed request text."""
import re

from .response_policy import natural_reply

SESSION_GAP_SECONDS=30*60
_GREETING=re.compile(
    r'^\s*(?:здравствуйте|здравствуй|приветствую|привет|добрый\s+(?:день|вечер)|доброе\s+утро)\b',
    re.I)
_OPENING=re.compile(
    r'^\s*(?:здравствуйте|здравствуй|приветствую|привет|добрый\s+(?:день|вечер)|доброе\s+утро)\b[!.,:)\s]*',
    re.I)


def session_info(db,chat,first,last):
    """Only delivered replies and the current raw client batch define greeting state.

    A half-hour gap starts a fresh greeting exchange only when the current client
    actually greets. It does not reset product, order, or account context.
    """
    rows=db.execute("""SELECT mid,text,at FROM messages
        WHERE chat=? AND role='client' AND mid BETWEEN ? AND ?
        ORDER BY mid LIMIT 61""",(chat,first,last)).fetchall()
    prior=db.execute("""SELECT mid,at FROM messages
        WHERE chat=? AND role IN ('assistant','owner') AND mid<?
        ORDER BY mid DESC LIMIT 1""",(chat,last)).fetchone()
    if prior:rows=[row for row in rows if row['mid']>prior['mid']]
    greeted=any(_GREETING.match(str(row['text'] or '')) for row in rows)
    continuing=prior is not None
    if greeted and prior and rows:
        first_at=rows[0]['at'];prior_at=prior['at']
        if (type(first_at) in (int,float) and type(prior_at) in (int,float)
                and first_at-prior_at>=SESSION_GAP_SECONDS):
            continuing=False
    return {'continuing':continuing,'current_client_greeting':bool(greeted)}


INTRO=('Здравствуйте! Я ИИ-помощник Александра, помогу с вопросами по нейросетям '
       'и оформлению подписок)')
INTRO_PLAIN=INTRO.removeprefix('Здравствуйте! ')


def address_mode(text):
    """Mirror the client's address. Formal вы wins if both appear."""
    value=str(text or '').casefold().replace('ё','е')
    formal=bool(re.search(r'\bвы\b|\bвас\b|\bвам\b|\bваш',value))
    informal=bool(re.search(r'\bты\b|\bтебя\b|\bтебе\b|\bтвой\b|\bтвоя\b|\bтвои\b',value))
    if informal and not formal:
        return 'ты'
    return 'вы'


_INNER_GREETING=re.compile(
    r'^(?:здравствуйте|здравствуй|приветствую|привет|добрый\s+(?:день|вечер)|доброе\s+утро)\s*[!.,)]+\s*',re.I)


def single_greeting(reply):
    """Owner 24.11.2026 (ElevenLabs: «Здравствуйте!… Добрый вечер! Creator вам подходит»): one message greets once.

    When rules put text before the model's own answer, the model's «Добрый вечер!» ended up in the middle.
    A greeting is kept only at the very start; later paragraphs lose their greeting opening.
    """
    parts=re.split(r'(\n\s*\n)',str(reply or ''))
    seen_text=False
    for i in range(0,len(parts),2):
        piece=parts[i]
        if not piece.strip():continue
        if seen_text:
            stripped=_INNER_GREETING.sub('',piece.lstrip(),count=1)
            if stripped!=piece.lstrip():
                parts[i]=stripped[:1].upper()+stripped[1:] if stripped else ''
        seen_text=True
    text=''.join(parts)
    return re.sub(r'\n\s*\n(?:\s*\n)+','\n\n',text).strip()


def decorate_reply(reply, info=None, introduced=False):
    """First contact gets the agreed introduction once, then the answer itself.

    `introduced` is persisted by the store, so a restart does not greet again.
    Later messages are not given a second hello.
    """
    reply=single_greeting(natural_reply(reply).strip())
    if reply.startswith(INTRO):
        body=reply[len(INTRO):].strip()
    elif reply.startswith(INTRO_PLAIN):
        body=reply[len(INTRO_PLAIN):].strip()
    else:
        body=_OPENING.sub('',reply,count=1).strip()
    if not body:
        body='Чем могу помочь?'
    greeted=bool((info or {}).get('current_client_greeting'))
    if introduced:
        return ('Здравствуйте!\n\n' if greeted else '')+body
    return (INTRO if greeted else INTRO_PLAIN)+'\n\n'+body
