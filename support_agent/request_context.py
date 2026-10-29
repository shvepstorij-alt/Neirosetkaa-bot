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
    lines=[line.strip() for line in str(text).splitlines() if line.strip()]
    if len(lines)>1:
        return all(is_acknowledgement(line) for line in lines)
    text=re.sub(r'[\s!?.…,]+',' ',str(text).casefold()).strip()
    return text in ('спасибо','хорошо','понял','поняла','ок','окей','да','ладно')


def _dependent_followup(text):
    """Only explicit short references; never infer a topic from arbitrary prose."""
    clean=re.sub(r'[\s!?.…,]+',' ',str(text).casefold()).strip()
    return bool(re.fullmatch(
        r'(?:а |и )?(?:сколько (?:это |он |она )?стоит|какая цена|какие лимиты|'
        r'что входит|как (?:его |её |это )?оформить|как оплатить|какой (?:из них )?лучше|'
        r'что (?:нужно(?: будет)?|потребуется|нужно будет предоставить) для оформления|'
        r'какие данные (?:нужны|потребуются)(?: для оформления)?|'
        r'а у (?:первого|второго|третьего)|у (?:первого|второго|третьего))',clean))


def _apple_detail_kind(text):
    line=re.sub(r'\s+',' ',str(text).casefold()).strip(' !?.,…')
    line=re.sub(r'^(?:нет[, ]+|лучше |тогда |точнее |передумал[, ]+|передумала[, ]+)','',line)
    from .apple_matching import ALIASES, PAIR
    region=re.sub(r'^(?:у меня )?(?:регион(?: apple id)? )?','',line)
    aliases={alias.casefold() for names in ALIASES.values() for alias in names}
    aliases.update(code.lower() for code in ALIASES)
    if region in aliases:return 'region',line
    amount=re.sub(r'^(?:(?:пополнить(?: баланс)?(?: хочу)?|хочу пополнить(?: баланс)?|сумма)\s+)?(?:на\s+)?','',line)
    if PAIR.fullmatch(amount):return 'amount',line
    return None,line


def _current_apple_details(text):
    """Only standalone slot replies supersede older slot replies; never rewrite prose."""
    if re.search(r'\b(?:сравни[а-яё]*|сравнение|оба|обоих|варианты|или)\b',str(text),re.I):
        return text
    lines=str(text).splitlines()
    kinds=[_apple_detail_kind(line) for line in lines]
    last={kind:i for i,(kind,_) in enumerate(kinds) if kind}
    kept=[]
    for i,(line,(kind,_)) in enumerate(zip(lines,kinds)):
        if kind and last[kind]!=i:continue
        if kind:
            line=re.sub(r'^(?:нет[, ]+|лучше |тогда |точнее |передумал[, ]+|передумала[, ]+)','',line.strip(),flags=re.I)
        kept.append(line)
    return '\n'.join(kept)


def _apple_details_candidate(text):
    """Recognize complete short region/amount replies, not arbitrary new topics."""
    lines=[re.sub(r'\s+',' ',line.casefold()).strip(' !?.,…')
           for line in str(text).splitlines() if line.strip()]
    if not lines:return False
    if all(is_acknowledgement(line) or _apple_detail_kind(line)[0] for line in lines):return True
    return _regional_question(text)


def _regional_question(text):
    """Recognize a regional price question as a whole, not a particular phrase."""
    from .apple_matching import ALIASES,PAIR,region_codes,words
    if not region_codes(text):return False
    remainder=PAIR.sub(' ',str(text))
    tokens=set(words(remainder))
    allowed=set('а и или для в во на у меня регион региона регионе сколько стоит будет стоить пополнение пополнить баланс баланса цена цены хочу нужно пожалуйста подскажите'.split())
    allowed.update(w for code,names in ALIASES.items() for alias in (code,*names) for w in words(alias))
    return not tokens-allowed


def _apple_topic_only(text):
    from .catalog_context import ALIASES,_words
    return _words(text) in {_words(alias) for alias in ALIASES['appstore']}


def _link_apple_turn(previous,text):
    """Carry service identity, but not an earlier country's amount, into a new regional question."""
    from .apple_matching import region_codes
    if _apple_topic_only(text):
        if _has_apple_topic(previous):return previous
        if _apple_details_candidate(previous):return text+'\n'+previous
    if _apple_region_followup(text,previous):
        if re.search(r'\b(?:сравни[а-яё]*|сравнение|оба|обоих|варианты|или)\b',previous,re.I):
            return previous+'\n'+text
        if _regional_question(text) and region_codes(text):
            from .apple_matching import amounts
            anchors=[line for line in previous.splitlines() if _has_apple_topic(line)
                     and not region_codes(line) and not amounts(line)]
            return (anchors[0] if anchors else 'App Store')+'\n'+_current_apple_details(text)
        return _current_apple_details(previous+'\n'+text)
    return None


def _has_apple_topic(text):
    from .catalog_context import ALIASES, _contains, _words
    return any(_contains(_words(text),_words(alias)) for alias in ALIASES['appstore'])


def _apple_region_followup(text, previous):
    return _apple_details_candidate(text) and _has_apple_topic(previous)


def _service_names(catalog):
    from .catalog_context import ALIASES,_words
    names={_words(alias) for aliases in ALIASES.values() for alias in aliases}
    for row in catalog:
        for key in ('service','service_key'):
            value=_words(row.get(key,''))
            if value:names.add(value)
    return names


def _contextual_followup(text,catalog):
    """Short workflow/plan references may inherit client context, never account state."""
    from .catalog_context import _words,_contains
    tokens=_words(text)
    if not tokens or len(tokens)>24:return False
    if any(_contains(tokens,name) for name in _service_names(catalog)):return False
    allowed=set('а и это этот эта его ее её мне от меня вам вас у нужно нужны нужен потребуется требуется какие данные что как куда когда сколько времени ждать долго занимает оформить оформление оформления оплатить оплата оплаты оплатил оплатила оплачено после передать прислать отправить почта почту пароль аккаунт аккаунта вход активация активации активировать подписка подписку подписки тариф тарифа тарифы выбрать выбирать месяц месяца месяцев год года годовой месячный можно ли будет для на по с со без уже не прошло работает спасибо пожалуйста подскажите тогда хочу да нет'.split())
    for row in catalog:
        allowed.update(_words(row.get('plan','')))
    intent=set('нужно нужны нужен потребуется требуется данные оформить оформление оформления оплатить оплата оплаты оплатил оплатила оплачено активация активации активировать ждать долго месяц год годовой месячный пароль почту аккаунт тариф тарифа'.split())
    plans={word for row in catalog for word in _words(row.get('plan',''))}
    return bool(set(tokens)&(intent|plans)) and all(t in allowed or t.isdigit() for t in tokens)


def resolve_request(db, chat, first, last, text, catalog=()):
    """Persist only message references. Deleted/expired content is not resurrected.

    Older deployments have no pointer: use the most recent draft's client batch,
    bounded by the preceding draft. Never use generated draft text as evidence.
    """
    db.execute('CREATE TABLE IF NOT EXISTS request_context (chat INTEGER PRIMARY KEY, first_mid INTEGER, last_mid INTEGER)')
    if _has_apple_topic(text):
        text=_current_apple_details(text)
    if is_acknowledgement(text):return text
    reminder=is_reminder(text)
    followup=_dependent_followup(text) or _contextual_followup(text,catalog)
    from .catalog_context import _words,_contains
    service_only=_words(text) in _service_names(catalog)
    # Region-only replies are considered below only after examining prior client
    # content. A standalone region must never revive an unrelated service.
    region_candidate=_apple_details_candidate(text)
    if not reminder and not followup and not region_candidate and not service_only:
        db.execute('INSERT OR REPLACE INTO request_context VALUES (?,?,?)',(chat,first,last))
        return text
    pointer=db.execute('SELECT first_mid,last_mid FROM request_context WHERE chat=?',(chat,)).fetchone()
    if pointer:
        bounds=(pointer['first_mid'],pointer['last_mid'])
    else:
        drafts=db.execute('SELECT DISTINCT mid FROM drafts WHERE chat=? AND mid<? ORDER BY mid DESC LIMIT 2',(chat,first)).fetchall()
        if not drafts:
            if not reminder:
                db.execute('INSERT OR REPLACE INTO request_context VALUES (?,?,?)',(chat,first,last))
                return text
            return ''
        bounds=(drafts[1]['mid']+1 if len(drafts)>1 else drafts[0]['mid'],drafts[0]['mid'])
    rows=db.execute("SELECT text FROM messages WHERE chat=? AND role='client' AND mid BETWEEN ? AND ? ORDER BY mid LIMIT 61",(chat,*bounds)).fetchall()
    if len(rows)>60:return '' if reminder else text
    parts=[r['text'] for r in rows if r['text'] and not is_reminder(r['text'])
           and not is_acknowledgement(r['text'])]
    previous=''
    for part in parts:
        linked=_link_apple_turn(previous,part) if previous else None
        previous=linked if linked is not None else (previous+'\n'+part if previous else part)
    if _has_apple_topic(previous):
        previous=_current_apple_details(previous)
    if not previous or len(previous)>24000:return '' if reminder else text
    if not reminder:
        linked=_link_apple_turn(previous,text)
        if linked is None and service_only:
            previous_has_service=any(_contains(_words(previous),name) for name in _service_names(catalog))
            if not previous_has_service and (_dependent_followup(previous) or _contextual_followup(previous,catalog)):
                linked=previous+'\n'+text
        if followup or linked is not None:
            combined=linked if linked is not None else previous+'\n'+text
            if len(combined)>24000:return text
            db.execute('INSERT OR REPLACE INTO request_context VALUES (?,?,?)',(chat,bounds[0],last))
            return combined
        db.execute('INSERT OR REPLACE INTO request_context VALUES (?,?,?)',(chat,first,last))
        return text
    db.execute('INSERT OR REPLACE INTO request_context VALUES (?,?,?)',(chat,*bounds))
    return previous
