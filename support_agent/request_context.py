"""Keep reminder messages attached to the last client question, never a model answer."""
import re
import time
import json


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
    details=re.sub(r'^(?:(?:а|и|можно|расскажите|подскажите|пожалуйста)\s+){0,5}','',clean)
    if re.fullmatch(r'(?:подробнее|поподробнее|подробней|побольше\s+информации)'
                    r'(?:\s+(?:про|о|об)\s+(?:эти\s+|этих\s+)?'
                    r'(?:тариф[а-яё]*|план[а-яё]*|подписк[а-яё]*|возможност[а-яё]*|'
                    r'функци[а-яё]*|них|это|нём|нем))?',details):
        return True
    return bool(re.fullmatch(
        r'(?:а |и )?(?:сколько (?:это |он |она )?стоит|какая цена|какие (?:есть )?тарифы(?: есть)?|какие лимиты|'
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
    amount=re.sub(r'^(?:а\s+)?(?:сколько\s+(?:будет\s+)?стоить|сколько\s+стоит|какая\s+цена(?:\s+на)?)\s+','',line)
    amount=re.sub(r'^(?:(?:мне\s+)?(?:нужно|надо)\s+)?','',amount)
    amount=re.sub(r'^(?:(?:пополнить(?: баланс)?(?: хочу)?|хочу пополнить(?: баланс)?|сумма)\s+)?(?:на\s+)?','',amount)
    if PAIR.fullmatch(amount):return 'amount',line
    # A short answer can specify the desired amount without its currency.
    # Preserve it as client context; the renderer must ask for currency rather
    # than manufacture a price or infer RUB from the conversation language.
    if re.fullmatch(r'\d+(?:[.,]\d{1,2})?\s*(?:тысячи?|тысяч|тыс)',amount):return 'amount',line
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
        if kind=='amount':
            from .apple_matching import amounts
            shorthand=re.search(r'(\d+(?:[.,]\d{1,2})?)\s*(?:тысячи?|тысяч|тыс)(?!\w)',line,re.I)
            if shorthand and not amounts(line):
                from decimal import Decimal
                value=Decimal(shorthand[1].replace(',','.'))*1000
                # Restating «3000р» as «3 тысячи» keeps an explicitly given
                # currency only when the numeric amount is exactly the same.
                prior=[lines[j] for j in range(i) if kinds[j][0]=='amount' and
                       len(amounts(lines[j]))==1 and next(iter(amounts(lines[j])))[0]==value]
                if prior:line=prior[-1]
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
    value=re.sub(r'\b(?:мне|нужен|нужна|нужно|именно|это)\b',' ',str(text),flags=re.I)
    return _words(value) in {_words(alias) for alias in ALIASES['appstore']}


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


def _only_service_names(text,catalog):
    from .catalog_context import _words
    tokens=list(_words(text));matched=False
    for name in sorted(_service_names(catalog),key=len,reverse=True):
        i=0
        while i<=len(tokens)-len(name):
            if tuple(tokens[i:i+len(name)])==name:
                del tokens[i:i+len(name)];matched=True
            else:i+=1
    return matched and all(t in ('сервис','сервисы','сервиса','это','и','именно','про','подписка',
                                 'а','на','мне','нужен','нужна','нужно') for t in tokens)


def _contextual_followup(text,catalog):
    """Short workflow/plan references may inherit client context, never account state."""
    from .catalog_context import _words,_contains
    tokens=_words(text)
    if not tokens or len(tokens)>60:return False
    if any(_contains(tokens,name) for name in _service_names(catalog)):return False
    if _workflow_help_followup(text):return True
    if _selection_continuation(text):return True
    if len(tokens)>24:return False
    allowed=set('а и это этот эта его ее её мне от меня вам вас у нужно нужны нужен потребуется требуется какие данные что как куда когда сколько времени ждать долго занимает оформить оформление оформления оплатить оплата оплаты оплатил оплатила оплачено после передать прислать отправить почта почту пароль аккаунт аккаунта вход активация активации активировать подписка подписку подписки тариф тарифа тарифы выбрать выбирать месяц месяца месяцев год года годовой месячный можно ли будет для на по с со без уже не прошло работает спасибо пожалуйста подскажите тогда хочу да нет двух этих сервисов обоих обоим них получить скопировать найти ссылка ссылку ссылки платежную платёжную страницу страницы'.split())
    allowed.update('существующий существующем существующего текущий текущем текущего старый старом старого собственный своем своём ссылке ссылки почте паролю данным давайте давай выбираю выберу вариант способом через удобнее наверное бота бот ботом его него продолжить продолжать расскажите подробнее поподробнее пожалуйста попробовать остановимся остановлюсь плюс plus какой какую какой-то посоветуете порекомендуете подойдет подойдёт лучше выбрать определиться помогите'.split())
    allowed.update('скинуть скидывать кидать выслать присылать пришлю отправлю оплачу оплатим оплату сейчас хорошо'.split())
    for row in catalog:
        allowed.update(_words(row.get('plan','')))
    intent=set('нужно нужны нужен потребуется требуется данные оформить оформление оформления оплатить оплата оплаты оплатил оплатила оплачено активация активации активировать ждать долго месяц год годовой месячный пароль почту аккаунт тариф тарифа ссылку ссылка ссылки'.split())
    intent.update(('ссылке','почте','паролю','данным'))
    intent.update(('оплачу','оплатим','оплату'))
    plans={word for row in catalog for word in _words(row.get('plan',''))}
    return bool(set(tokens)&(intent|plans)) and all(t in allowed or t.isdigit() for t in tokens)


def _selection_continuation(text):
    """Bounded references to a selected plan/task, not a fresh unknown brand."""
    value=str(text).strip()
    if len(value)>350:return False
    if re.search(r'теперь|кстати|другая\s+тема|отменяю|забудь',value,re.I):return False
    if (re.search(r'\b(?:отдельн[а-яё]*|друг[а-яё]*|альтернативн[а-яё]*)\s+(?:сервис|инструмент|нейросет)',value,re.I)
            and re.search(r'посовет|предлож|порекоменду|подойд[её]т|выбрать',value,re.I)
            and not re.search(r'[A-Za-z][A-Za-z0-9_-]*',value)):
        return True
    return bool(re.fullmatch(
        r'(?:(?:а|и|ну|тогда)\s+)?(?:'
        r'(?:давайте|беру|выбираю|хочу)\s+(?:тариф\s+)?(?:плюс|plus|pro|basic|go|max)(?:[.!?\s]+(?:это\s+)?на\s+мой\s+(?:существующий|текущий|старый)\s+аккаунт)?[.!?)]*|'
        r'(?:это\s+)?на\s+мой\s+(?:существующий|текущий|старый)\s+аккаунт[.!?)]*|'
        r'(?:если\s+)?нужен\s+отдельный\s+сервис[,\s]+что\s+(?:посоветуете|предложите)(?:\s+и\s+почему)?[.!?)]*|'
        r'что\s+(?:посоветуете|предложите)\s+(?:под|для)\s+(?:этой?\s+)?задач[уи][.!?)]*'
        r')',value,re.I))


def _workflow_help_followup(text):
    """Bounded help with the active procedure, not a new product selection.

    It only marks a possible continuation. Existing client evidence and the
    normal expiry rules still decide whether any service can be inherited.
    """
    value=str(text)
    if re.fullmatch(r'\s*(?:а\s+)?(?:что\s+(?:тогда\s+)?можно\s+сделать|долго\s+(?:ещ[её]\s+)?ждать|сколько\s+(?:ещ[её]\s+)?(?:ждать|времени\s+ждать))[.!?\s)]*',value,re.I):return True
    if re.search(r'\b(?:теперь|кстати|вместо|другой\s+сервис|новый\s+сервис|другая\s+тема|отменяю|забудь)\b',value,re.I):return False
    if re.search(r'генерир|генерац|создавать|монтаж|снимать\s+видео|делать\s+(?:фото|видео)',value,re.I):return False
    # A new Cyrillic login target is still a product name, not an implicit UI
    # continuation ("как войти в Авито"). Ordinary account/browser nouns may
    # refer to the active procedure; an unrecognized target must stay fresh.
    target=re.search(r'\bвойти\s+(?:в|на)\s+([а-яёa-z][а-яёa-z0-9_-]*)',value,re.I)
    generic_targets={'свой','свою','мой','мою','аккаунт','браузер','профиль','систему',
                     'приложение','личный','кабинет','учетную','учётную','сайт','него','неё','нее'}
    if target and target[1].casefold() not in generic_targets:return False
    # Brand-like Latin tokens must not silently rebind an unfamiliar service to
    # the previous one. Common browser/technical UI words are not new products.
    technical={'chrome','safari','firefox','edge','google','json','api','auth',
               'session','login','account','id','organization','org','http','https',
               'url','email','browser','cookies','cookie','copy','paste'}
    if any(word.casefold() not in technical for word in re.findall(r'[A-Za-z][A-Za-z0-9_-]*',value)):
        return False
    anchor=re.search(r'авториз|авторизац|войти|вход|сесси|токен|'
                     r'активир|активац|кнопк|браузер|скопир|встав|пункт|шаг|ссылк|'
                     r'organization\s+id|\borg\s+id\b',value,re.I)
    request=re.search(r'\b(?:как|что|где|откуда|почему|какой|какая|какие|объясни[а-яё]*|'
                      r'подскаж[а-яё]*|помог[а-яё]*|не\s+(?:могу|получается|открывается|понимаю|вижу|видно|работает))\b',value,re.I)
    return bool(anchor and request)


def _same_service_reference(text, previous, catalog):
    """An explicit same-brand 'can it do this?' retains the client's task.

    A named different service always starts its own request. This is based on
    actual client messages only, never on a generated capability claim.
    """
    from .catalog_context import service_mentions
    current=set(service_mentions(text,catalog));prior=set(service_mentions(previous,catalog))
    return bool(current and current<=prior and re.search(
        r'\b(?:это|такое|эти\s+задачи|этой\s+задач[а-яё]*|для\s+этого|с\s+этим)\b',text,re.I))


def _purpose_answer_candidate(text,catalog):
    """Concrete task descriptions may answer a pending choice clarification.

    This flag alone never imports history. The prior client selection or a
    recent sent task question must also be established below.
    """
    from .selection_help import has_selection_purpose
    from .catalog_context import service_mentions
    if not has_selection_purpose(text) or service_mentions(text,catalog):return False
    # An explicitly introduced, unfamiliar Latin product must not be rebound to
    # an older catalogue brand merely because its purpose contains "documents".
    # Formats/ordinary technical requirements are not service introductions.
    technical={'pdf','docx','xlsx','csv','txt','api','sso','scim','saml','webhook',
               'offline','python','javascript','typescript','html','css','sql',
               'excel','word','google','docs','sheets','drive','powerpoint'}
    introduced=re.search(r'(?:^|\b(?:сервис[а-яё]*|подписк[а-яё]*|оформить|нужен|нужна|интересует)\s+)'
                         r'([A-Za-z][A-Za-z0-9_-]{2,})(?!\w)',str(text).strip(),re.I)
    if introduced and introduced[1].casefold() not in technical:return False
    if re.search(r'\b(?:теперь|кстати|вместо|другой\s+сервис|другая\s+тема|отменяю|забудь)\b',text,re.I):return False
    # A new how-to, troubleshooting or purchase question is not an answer
    # describing what the customer intends to do with the earlier service.
    if re.match(r'^\s*(?:а\s+)?(?:как|почему|где|когда|сколько|можно\s+ли|не\s+работает|ошибка)\b',text,re.I):return False
    return True


def _purpose_answers_previous(db,chat,first,bounds,previous,catalog):
    from .catalog_context import service_mentions,annotate
    from .selection_help import is_selection_request
    if not service_mentions(previous,catalog):return False
    if is_selection_request(previous,annotate({'catalog':catalog},previous)):
        return True
    asked=db.execute("SELECT text,at FROM messages WHERE chat=? AND role IN ('assistant','owner') AND mid>? AND mid<? ORDER BY mid DESC LIMIT 1",
                     (chat,bounds[1],first)).fetchone()
    if not asked:return False
    from .session_context import SESSION_GAP
    if type(asked['at']) not in (int,float) or not 0<=time.time()-asked['at']<=SESSION_GAP:return False
    return bool(re.search(r'для\s+каких\s+задач|что\s+(?:именно\s+)?(?:вы\s+)?хотите\s+делать|'
                          r'какие\s+задачи\s+(?:планируете|хотите)|под\s+какие\s+задачи',asked['text'] or '',re.I))


def _recent_service_labels(db,chat,first,catalog,source_mids=None):
    """Only explicit client mentions, bounded by time and size; never model answers."""
    from .catalog_context import service_mentions
    from .session_context import _active,_boundary,_new_inquiry,SESSION_GAP
    now=time.time()
    rows=db.execute("SELECT mid,text,at FROM messages WHERE chat=? AND role='client' AND mid<? ORDER BY mid DESC LIMIT 61",(chat,first)).fetchall()
    from .context_validity import validate_client_rows
    rows=reversed(validate_client_rows(rows,now=now).rows)
    kept=[];used=0;later=now
    for row in rows:
        text=row['text'] or '';used+=len(text)
        if used>24000:break
        at=row['at']
        if isinstance(at,(int,float)):
            if not 0<=later-at<=SESSION_GAP:break
            later=at
        if _boundary(text):break
        found=list(service_mentions(text,catalog).items())
        if len({identity for identity,_ in found})>2:return []
        kept.append(dict(row))
        if _new_inquiry(text):break
    services,_=_active(list(reversed(kept)),catalog)
    selected=sorted(services,key=lambda service:service['mid'])[-2:]
    if source_mids is not None and {service['mid'] for service in selected}!=set(source_mids):return []
    return [(service['label'],service['mid']) for service in selected] if len(selected)==2 else []


def _resolve_plural_reference(db,chat,mid,text,catalog):
    if re.search(r'\b(?:двух\s+этих|этих\s+двух|обоих|обоим)\b',text,re.I):
        db.execute('CREATE TABLE IF NOT EXISTS plural_context_refs (chat INTEGER, mid INTEGER, refs TEXT, PRIMARY KEY(chat,mid))')
        saved=db.execute('SELECT refs FROM plural_context_refs WHERE chat=? AND mid=?',(chat,mid)).fetchone()
        sources=json.loads(saved['refs']) if saved else None
        names=_recent_service_labels(db,chat,mid,catalog,sources)
        if saved is None:
            db.execute('INSERT INTO plural_context_refs VALUES (?,?,?)',(chat,mid,json.dumps(sorted({ref for _,ref in names}))))
        if len(names)==2:return 'Сервисы: '+', '.join(label for label,_ in names)+'\n'+text
    return text


def _reply_client_context(db,chat,first,last,text,catalog):
    """Resolve a reply ID to original client evidence, never the quoted answer.

    Only same-chat existing bounded source messages are usable. Telegram reply
    text itself is not evidence and is neither stored nor reconstructed here.
    """
    from .catalog_context import service_mentions
    from .session_context import MAX_AGE,SESSION_GAP
    rows=db.execute('SELECT media FROM messages WHERE chat=? AND role=\'client\' AND mid BETWEEN ? AND ? ORDER BY mid',(chat,first,last)).fetchall()
    targets=[]
    for row in rows:
        try:media=json.loads(row['media'] or '{}')
        except (ValueError,TypeError):continue
        target=media.get('reply_mid') if isinstance(media,dict) else None
        if isinstance(target,int) and not isinstance(target,bool) and 0<target<first:targets.append(target)
    if len(set(targets))!=1:return None
    target=db.execute('SELECT mid,role,text,at FROM messages WHERE chat=? AND mid=?',(chat,targets[0])).fetchone()
    if not target:return None
    if target['role']=='client':bounds=(target['mid'],target['mid'])
    elif target['role'] in ('assistant','owner'):
        draft=db.execute('SELECT mid FROM drafts WHERE chat=? AND telegram_mid=? ORDER BY id DESC LIMIT 1',(chat,target['mid'])).fetchone()
        end=draft['mid'] if draft else target['mid']-1
        boundary=db.execute("SELECT MAX(mid) AS mid FROM messages WHERE chat=? AND role IN ('assistant','owner') AND mid<?",(chat,end+1)).fetchone()['mid']
        bounds=((boundary+1) if boundary is not None else 0,end)
    else:return None
    sources=db.execute("SELECT mid,text,at FROM messages WHERE chat=? AND role='client' AND mid BETWEEN ? AND ? ORDER BY mid LIMIT 61",(chat,*bounds)).fetchall()
    now=time.time()
    if not sources or len(sources)>60:return None
    from .context_validity import validate_client_rows
    valid=validate_client_rows(reversed(sources),now=now,max_age=min(MAX_AGE,SESSION_GAP))
    if not valid.complete:return None
    previous='\n'.join(r['text'] for r in sources if r['text'] and not is_acknowledgement(r['text']))
    if not previous or len(previous)+len(text)>24000:return None
    current=set(service_mentions(text,catalog));prior=set(service_mentions(previous,catalog))
    if current and not current<=prior:return None
    return previous+'\n'+text


def resolve_request(db, chat, first, last, text, catalog=()):
    """Persist only message references. Deleted/expired content is not resurrected.

    Older deployments have no pointer: use the most recent draft's client batch,
    bounded by the preceding draft. Never use generated draft text as evidence.
    """
    db.execute('CREATE TABLE IF NOT EXISTS request_context (chat INTEGER PRIMARY KEY, first_mid INTEGER, last_mid INTEGER)')
    db.execute('CREATE TABLE IF NOT EXISTS request_context_sources (chat INTEGER PRIMARY KEY,mids TEXT)')
    def point(start,end):
        db.execute('INSERT OR REPLACE INTO request_context VALUES (?,?,?)',(chat,start,end))
        mids=[r['mid'] for r in db.execute("SELECT mid FROM messages WHERE chat=? AND role='client' AND mid BETWEEN ? AND ? ORDER BY mid",(chat,start,end))]
        db.execute('INSERT OR REPLACE INTO request_context_sources VALUES (?,?)',(chat,json.dumps(mids)))
    from .turn_contract import entity_reference, reference_intent, save_reference,plan_reference,contextual_question,focused_history
    save_reference(db,chat,last,None)
    replied=_reply_client_context(db,chat,first,last,text,catalog)
    if replied is not None:
        point(first,last)
        return replied
    text=_resolve_plural_reference(db,chat,first,text,catalog)
    if _has_apple_topic(text):
        text=_current_apple_details(text)
    if is_acknowledgement(text):return text
    reminder=is_reminder(text)
    contextual=contextual_question(text,catalog)
    plan_only=bool(plan_reference(text,catalog))
    followup=_dependent_followup(text) or _contextual_followup(text,catalog) or contextual or plan_only
    lines=[line.strip() for line in text.splitlines() if line.strip() and not is_acknowledgement(line)]
    # A collected batch can ask about the previous service and introduce a
    # second service on its next line. Keep the first explicit continuation;
    # do not reduce the entire batch to only the last named product.
    leading_followup=(len(lines)>1 and _dependent_followup(lines[0]))
    from .catalog_context import _words,_contains
    service_only=entity_reference(text,catalog) is not None
    purpose_candidate=_purpose_answer_candidate(text,catalog)
    reference_candidate=bool(re.search(r'\b(?:это|такое|эти\s+задачи|этой\s+задач[а-яё]*|для\s+этого|с\s+этим)\b',text,re.I))
    # Region-only replies are considered below only after examining prior client
    # content. A standalone region must never revive an unrelated service.
    region_candidate=_apple_details_candidate(text)
    if not reminder and not followup and not leading_followup and not region_candidate and not service_only and not purpose_candidate and not reference_candidate:
        point(first,last)
        return text
    pointer=db.execute('SELECT first_mid,last_mid FROM request_context WHERE chat=?',(chat,)).fetchone()
    if pointer:
        bounds=(pointer['first_mid'],pointer['last_mid'])
    else:
        drafts=db.execute('SELECT DISTINCT mid FROM drafts WHERE chat=? AND mid<? ORDER BY mid DESC LIMIT 2',(chat,first)).fetchall()
        if not drafts:
            if not reminder:
                point(first,last)
                save_reference(db,chat,last,reference_intent(text,[],catalog))
                return text
            return ''
        bounds=(drafts[1]['mid']+1 if len(drafts)>1 else drafts[0]['mid'],drafts[0]['mid'])
    rows=db.execute("SELECT mid,text,at FROM messages WHERE chat=? AND role='client' AND mid BETWEEN ? AND ? ORDER BY mid LIMIT 61",(chat,*bounds)).fetchall()
    # The pointer is not proof that an old inquiry is still active. Apply the
    # same maximum age/session gap as the multi-service context, before any
    # dependent question can inherit a service, region or denomination.
    from .session_context import MAX_AGE,SESSION_GAP
    now=time.time()
    from .context_validity import validate_client_rows,require_sources
    valid=validate_client_rows(reversed(rows),now=now,max_age=MAX_AGE,session_gap=SESSION_GAP)
    original=db.execute('SELECT mids FROM request_context_sources WHERE chat=?',(chat,)).fetchone() if pointer else None
    if original:valid=require_sources(valid,json.loads(original['mids']))
    if not valid.complete:
        point(first,last)
        from .turn_contract import IntentReference,EntityReference
        save_reference(db,chat,last,IntentReference(entity_reference(text,catalog) or EntityReference((),()),
                                                    (),None,'unavailable_client_context',valid.reason,
                                                    valid.checked_count,valid.missing_source_count))
        return '' if reminder else text
    if len(rows)>60:return '' if reminder else text
    if contextual or plan_only:
        rows=focused_history([dict(row) for row in rows],catalog)
    parts=[_resolve_plural_reference(db,chat,r['mid'],_reply_client_context(db,chat,r['mid'],r['mid'],r['text'],catalog) or r['text'],catalog) for r in rows if r['text'] and not is_reminder(r['text'])
           and not is_acknowledgement(r['text'])]
    previous=''
    for part in parts:
        linked=_link_apple_turn(previous,part) if previous else None
        previous=linked if linked is not None else (previous+'\n'+part if previous else part)
    if contextual or plan_only:
        from .turn_contract import client_entities
        from .catalog_context import service_mentions
        if not service_mentions(previous,catalog):
            focus={}
            for row in reversed(rows):
                focus=client_entities(row['text'],catalog)
                if focus:break
            if focus and not any(identity.startswith('unverified:') for identity in focus):
                previous=', '.join(dict.fromkeys(focus.values()))+'\n'+previous
    if _has_apple_topic(previous):
        previous=_current_apple_details(previous)
    if not previous or len(previous)>24000:return '' if reminder else text
    if leading_followup:
        from .catalog_context import service_mentions
        followup=bool(service_mentions(previous,catalog))
    if purpose_candidate and _purpose_answers_previous(db,chat,first,bounds,previous,catalog):
        followup=True
    if not reminder:
        linked=_link_apple_turn(previous,text)
        if linked is None:
            reference=reference_intent(text,[dict(row) for row in rows],catalog)
            if reference is not None:
                save_reference(db,chat,last,reference)
                if not reference.goals:
                    point(first,last)
                    return text
                point(bounds[0],last)
                if reference.reason=='same_entity_reference':return previous+'\n'+text
                return reference.question if reference.goals else text
        if linked is None and reference_candidate and _same_service_reference(text,previous,catalog):
            linked=previous+'\n'+text
        if linked is None and service_only:
            previous_has_service=any(_contains(_words(previous),name) for name in _service_names(catalog))
            if not previous_has_service and (_dependent_followup(previous) or _contextual_followup(previous,catalog)):
                linked=previous+'\n'+text
            elif previous_has_service:
                current_names={name for name in _service_names(catalog) if _contains(_words(text),name)}
                previous_names={name for name in _service_names(catalog) if _contains(_words(previous),name)}
                from .catalog_context import _families
                same_families=_families(_words(text)) and _families(_words(text))<=_families(_words(previous))
                if (current_names and current_names<=previous_names) or same_families:linked=previous+'\n'+text
        if followup or linked is not None:
            combined=linked if linked is not None else previous+'\n'+text
            if len(combined)>24000:return text
            point(bounds[0],last)
            return combined
        point(first,last)
        return text
    point(*bounds)
    return previous
