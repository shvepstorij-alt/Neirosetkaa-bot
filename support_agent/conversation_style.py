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


def decorate_reply(reply,info):
    """One natural greeting per new greeting exchange; factual text is untouched."""
    reply=natural_reply(reply)
    # Discard model/template salutations, then apply the independently computed
    # raw-batch decision. Old questions reconstructed by context cannot greet again.
    body=_OPENING.sub('',reply,count=1).strip()
    if not body:
        body='Чем могу помочь?'
    if info.get('current_client_greeting') and not info.get('continuing'):
        return 'Здравствуйте!)\n\n'+body
    return body
