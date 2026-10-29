"""Bounded client-reference context for summaries of several active services.

Integration: call after resolve_request(), before annotate(). The original current
client batch is read from messages so generated context cannot become evidence.
No account data, model summaries, prices or currencies are cached here.
"""
import json
import re
import time

from .catalog_context import ALIASES, _contains, _families, _words
from .apple_matching import region_codes

MAX_MESSAGES=60
MAX_CHARACTERS=24000
MAX_AGE=86400
SESSION_GAP=4*3600
MAX_SERVICES=8


def _all_intent(text):
    return bool(re.search(r'\b(?:всех(?:\s+этих)?\s+(?:сервисов|подписок|товаров)|'
                          r'всем(?:\s+этим)?\s+(?:сервисам|подпискам)|по\s+итогу|итого)\b',text,re.I))


def _mentions(text,catalog):
    tokens=_words(text);found={}
    for family,aliases in ALIASES.items():
        if any(_contains(tokens,_words(alias)) for alias in aliases):
            found[family]=aliases[0]
    from .catalog_context import annotate
    for row in annotate({'catalog':catalog},text)['requested_catalog']:
        if not isinstance(row,dict):continue
        name=str(row.get('service',''));key=str(row.get('service_key',''))
        if not name:continue
        families=_families(_words(name))|_families(_words(key))
        identity=next(iter(families)) if len(families)==1 else 'catalog:'+key.casefold()+':'+name.casefold()
        found[identity]=name
    return found


def _only_names(text,catalog):
    tokens=list(_words(text));names=[]
    for aliases in ALIASES.values():names.extend(_words(a) for a in aliases)
    for row in catalog:
        if isinstance(row,dict):names.extend(_words(row.get(k,'')) for k in ('service','service_key'))
    matched=False
    for name in sorted(set(names),key=len,reverse=True):
        if not name:continue
        i=0
        while i<=len(tokens)-len(name):
            if tuple(tokens[i:i+len(name)])==name:
                del tokens[i:i+len(name)];matched=True
            else:i+=1
    return matched and all(t in ('а','и','это','сервис','сервисы','сервиса','именно','про','подписка','подписки') for t in tokens)


def _region_only(text):
    # Do not interpret an unrelated account/card question as an Apple region.
    value=re.sub(r'^(?:а\s+)?(?:у меня\s+)?(?:регион(?:\s+apple\s+id)?\s+)?','',str(text).strip(),flags=re.I)
    codes=region_codes(value)
    if not codes:return False
    from .apple_matching import ALIASES as regions
    choices={_words(alias) for code,names in regions.items() for alias in (code,*names)}
    return _words(value) in choices


def _boundary(text):
    return bool(re.search(r'предыдущий\s+вопрос\s+отменяю|(?<!не )\bзабудь(?:те)?\s+вс[её]|'
                          r'отменяю\s+вс[её]|начн[её]м\s+заново|до\s+свидания|до\s+завтра',text,re.I))


def _new_inquiry(text):
    return bool(re.match(r'^\s*(?:привет|приветствую|здравствуйте|добрый\s+(?:день|вечер)|доброе\s+утро)\b',text,re.I))


def _session_rows(db,chat,last,now):
    rows=db.execute("SELECT mid,text,at FROM messages WHERE chat=? AND role='client' AND mid<=? ORDER BY mid DESC LIMIT ?",
                    (chat,last,MAX_MESSAGES+1)).fetchall()
    kept=[];chars=0;later=None
    for row in rows:
        at=row['at']
        if not isinstance(at,(int,float)) or not 0<=now-at<=MAX_AGE:break
        if later is not None and later-at>SESSION_GAP:break
        later=at
        if len(kept)>=MAX_MESSAGES:break
        text=row['text'] or '';chars+=len(text)
        if chars>MAX_CHARACTERS:break
        if _boundary(text):break
        kept.append(dict(row))
        if _new_inquiry(text):break
    return list(reversed(kept))


def _active(rows,catalog):
    services={};region=None
    for row in rows:
        text=row['text'] or '';mentions=_mentions(text,catalog)
        if re.search(r'теперь\s+только|пока\s+только|вместо\s+этого',text,re.I):services={};region=None
        cancelled=bool(re.search(r'\bне\s+нуж[а-яё]*|\bне\s+оформля[а-яё]*|\bне\s+обсуждаем',text,re.I))
        for identity,label in mentions.items():
            if cancelled:services.pop(identity,None)
            else:services[identity]={'identity':identity,'label':label,'mid':row['mid']}
        codes=region_codes(text)
        if len(codes)==1 and ('appstore' in mentions or ('appstore' in services and _region_only(text))):
            region={'code':next(iter(codes)),'mid':row['mid']}
    return list(services.values()),region


def _valid(snapshot,rows,catalog):
    by_mid={r['mid']:r for r in rows}
    for service in snapshot['services']:
        source=by_mid.get(service['mid'])
        if not source or service['identity'] not in _mentions(source['text'],catalog):return False
    for mid in snapshot['intents']:
        if mid not in by_mid or not _all_intent(by_mid[mid]['text']):return False
    region=snapshot.get('region')
    if region:
        source=by_mid.get(region['mid'])
        if not source or region_codes(source['text'])!={region['code']}:return False
    return True


def _uncertain(raw):
    return 'Контекст: исходный список сервисов не подтверждён. Уточните его, не подставляйте старые темы.\n'+raw


def _render(snapshot,rows,raw,followup):
    labels=[s['label'] for s in snapshot['services']]
    context='Сервисы текущего обращения: '+', '.join(labels)+'.'
    if snapshot.get('region') and any(s['identity']=='appstore' for s in snapshot['services']):
        context+='\nРегион Apple ID: '+snapshot['region']['code']+'.'
    if followup:
        # Restore only an actual all-services question, not earlier prices or replies.
        by_mid={r['mid']:r for r in rows}
        intent='\n'.join(by_mid[mid]['text'] for mid in snapshot['intents'])
        return context+'\n'+intent+'\n'+raw
    return context+'\n'+raw


def enrich_request(db,chat,first,last,text,catalog):
    """Enrich explicit all-service intent and its short clarifications only.

    Snapshots hold canonical service labels and source message IDs, not source
    text. A removed/expired source causes uncertainty, never rebinding to a third
    old brand. Ordinary new-focus questions are returned unchanged.
    """
    db.execute('CREATE TABLE IF NOT EXISTS session_context_refs (chat INTEGER, mid INTEGER, data TEXT, PRIMARY KEY(chat,mid))')
    current=db.execute("SELECT mid,text FROM messages WHERE chat=? AND role='client' AND mid BETWEEN ? AND ? ORDER BY mid",
                       (chat,first,last)).fetchall()
    raw='\n'.join(r['text'] or '' for r in current) if current else text
    all_intent=_all_intent(raw)
    if not all_intent and not (_only_names(raw,catalog) or _region_only(raw)):return text
    rows=_session_rows(db,chat,last,time.time())
    saved=db.execute('SELECT data FROM session_context_refs WHERE chat=? AND mid=?',(chat,last)).fetchone()
    if saved:
        snapshot=json.loads(saved['data'])
        return _render(snapshot,rows,raw,not all_intent) if _valid(snapshot,rows,catalog) else _uncertain(raw)
    previous=db.execute('SELECT mid,data FROM session_context_refs WHERE chat=? AND mid<? ORDER BY mid DESC LIMIT 1',(chat,first)).fetchone()
    prior=json.loads(previous['data']) if previous else None
    if prior and not all_intent:
        intervening=[r for r in rows if previous['mid']<r['mid']<first]
        if any(_mentions(r['text'],catalog) and not _only_names(r['text'],catalog)
               and not _all_intent(r['text']) and not _region_only(r['text']) for r in intervening):
            return text
    mids={r['mid'] for r in rows}
    if prior and previous['mid'] not in mids:
        old=db.execute("SELECT at FROM messages WHERE chat=? AND mid=? AND role='client'",(chat,previous['mid'])).fetchone()
        between=db.execute("SELECT text FROM messages WHERE chat=? AND role='client' AND mid>? AND mid<? ORDER BY mid DESC LIMIT ?",
                           (chat,previous['mid'],first,MAX_MESSAGES)).fetchall()
        gap=bool(old and isinstance(old['at'],(int,float)) and rows and rows[0]['at']-old['at']>SESSION_GAP)
        if gap or any(_boundary(r['text'] or '') or _new_inquiry(r['text'] or '') for r in between):prior=None
    if prior and (previous['mid'] not in mids or not _valid(prior,rows,catalog)):
        # Explicit names can repair missing source references. A bare region or
        # another all-intent must not silently substitute a deleted service.
        if not _only_names(raw,catalog):return _uncertain(raw)
        prior=None
    if not all_intent and prior is None:return text
    if all_intent:
        services,region=_active(rows,catalog)
        intents=[r['mid'] for r in current if _all_intent(r['text'] or '')]
        snapshot={'services':services,'region':region,'intents':intents}
    else:
        snapshot=dict(prior)
        if _only_names(raw,catalog):
            # The customer's explicit list is authoritative for this summary.
            services,_=_active([dict(mid=r['mid'],text=r['text'],at=time.time()) for r in current],catalog)
            snapshot['services']=services
        if _region_only(raw) and any(s['identity']=='appstore' for s in snapshot['services']):
            codes=region_codes(raw)
            if len(codes)==1:snapshot['region']={'code':next(iter(codes)),'mid':last}
    if not snapshot['services'] or len(snapshot['services'])>MAX_SERVICES or not snapshot['intents']:
        return _uncertain(raw)
    if not _valid(snapshot,rows,catalog):return _uncertain(raw)
    db.execute('INSERT INTO session_context_refs VALUES (?,?,?)',(chat,last,json.dumps(snapshot,ensure_ascii=False)))
    return _render(snapshot,rows,raw,not all_intent)
