"""One client-derived intent contract shared by context, routing and renderers.

Brand identity comes from the live catalogue/alias registry, never a list of
example conversations. References transfer the latest informational question;
they never transfer a tariff, price, regional slot or performed account action.
"""
from dataclasses import dataclass, asdict
import re


INFORMATION_GOALS = ('price', 'purchase', 'features', 'activation', 'installation')
_PROMPTS = {
    'price': 'Сколько стоит?',
    'purchase': 'Что нужно для оформления?',
    'features': 'Какие возможности и ограничения?',
    'activation': 'Как активировать?',
    'installation': 'Как установить приложение?',
}
_REFERENCE_WORDS = frozenset('а и или на у с для про по поводу насчет насчёт тогда '
    'то же самое еще ещё также это сервис сервиса сервисы подписка подписку '
    'мне нужен нужна нужно именно пожалуйста'.split())


@dataclass(frozen=True)
class EntityReference:
    identities: tuple[str, ...]
    labels: tuple[str, ...]
    verified_identity: bool = True


@dataclass(frozen=True)
class IntentReference:
    entities: EntityReference
    goals: tuple[str, ...]
    source_mid: int | None
    reason: str
    context_condition: str = ''
    checked_sources: int = 0
    missing_sources: int = 0
    product_kind: str = ''
    plan_labels: tuple[str, ...] = ()

    @property
    def question(self):
        return ', '.join(self.entities.labels)+'\n'+intent_questions(self.goals,self.product_kind)


def canonical_plan_words(value):
    """Catalogue plan typography: Pro5x, Pro 5× and Pro 5х share a label.

    This normalises a label's spelling, not a model tier or a numeric value.
    Only a complete catalogue plan match may select a SKU afterwards.
    """
    from .catalog_context import _words
    value=str(value).casefold().replace('×','x')
    value=re.sub(r'(?<=\d)х(?=\W|$)','x',value)
    value=re.sub(r'(?<=[a-zа-яё])(?=\d)|(?<=\d)(?=[a-zа-яё])',' ',value)
    aliases={'плюс':'plus','макс':'max','го':'go','бизнес':'business',
             'бейсик':'basic','бэйсик':'basic','премиум':'premium'}
    return tuple(aliases.get(word,word) for word in _words(value))


def normalize_plan_choice(text, catalog):
    """Normalize only a complete, catalog-backed plan choice, never prose."""
    from .catalog_context import _words
    words=list(_words(str(text)))
    if not words or len(words)>18:return str(text)
    prefix={'а','и','нет','лучше','тогда','выбираю','беру','хочу','давайте','давай',
            'мне','нужен','нужна','нужно','именно','пожалуйста','тариф','тарифа','план','плана'}
    while words and words[0] in prefix:words.pop(0)
    while words and words[-1] in {'пожалуйста'}:words.pop()
    if not words:return str(text)
    aliases={'про':'pro','стандарт':'standard','стандартный':'standard','ультра':'ultra',
             'тим':'team','премиум':'premium','плюс':'plus','макс':'max','го':'go',
             'бизнес':'business','бейсик':'basic','бэйсик':'basic'}
    proposed=canonical_plan_words(' '.join(aliases.get(w,w) for w in words))
    options={canonical_plan_words(row.get('plan','')) for row in catalog if isinstance(row,dict)}
    if proposed and proposed in options:
        return ' '.join(proposed)
    return str(text)


def plan_reference(text,catalog):
    """A whole catalogue plan-only reply, never an arbitrary new task."""
    from .fulfillment_reply import _selected_plan_rows
    from .catalog_context import service_mentions
    if len(str(text))>350 or service_mentions(text,catalog):return []
    matched=_selected_plan_rows(catalog,text,require_match=True)
    if not matched:return []
    tokens=list(canonical_plan_words(normalize_plan_choice(text,catalog)))
    for plan in sorted({canonical_plan_words(row.get('plan','')) for row in matched},key=len,reverse=True):
        if not plan:continue
        for start in range(len(tokens)-len(plan),-1,-1):
            if tuple(tokens[start:start+len(plan)])==plan:del tokens[start:start+len(plan)]
    allowed=set('а и нет лучше тогда тариф тарифа тарифе план плана выбираю беру хочу давайте давай '
                'мне нужен нужна нужно это именно пожалуйста подписка подписку'.split())
    return matched if not set(tokens)-allowed else []


def contextual_question(text,catalog):
    """Service-less informational follow-ups retain validated client focus."""
    from .catalog_context import service_mentions,_words
    from .request_goals import goals
    value=str(text)
    intent=goals(value)
    if (not intent or intent-set(INFORMATION_GOALS) or len(value)>500
            or service_mentions(value,catalog)):return False
    # A newly introduced product must reach discovery, including one not yet
    # present in the catalogue. Catalogue plan labels remain legitimate slots.
    public_tokens={word for row in catalog for word in canonical_plan_words(row.get('plan',''))}
    technical={'android','ios','iphone','apk','app','store','google','chrome','safari','windows','macos'}
    if any(word.casefold() not in public_tokens|technical
           for word in re.findall(r'[A-Za-z][A-Za-z0-9_-]*',value)):return False
    words=_words(value)
    if len(words)>45:return False
    ordinary=set('оформления оплаты покупки подписки тарифов тарифами тарифах тариф тарифы плана планов '
                 'них них же этого этого него нее неё аккаунта аккаунт приложении приложения приложение '
                 'телефоне телефона андроид айфон входа активации установки'.split())
    for target in re.findall(r'(?<!\w)(?:на|для|про|об|о|у|сервиса|сервисе)\s+([а-яё][а-яё0-9_-]*)',value,re.I):
        if target.casefold() not in ordinary and target.casefold() not in public_tokens:
            return False
    return True


def focused_history(history,catalog):
    """Keep the latest explicitly named client focus and its parameter turns."""
    from .catalog_context import service_mentions
    focus=None;kept=[]
    for row in reversed(history):
        named=client_entities(row.get('text',''),catalog)
        if named:
            if focus is None:focus=set(named)
            elif set(named)!=focus:break
        kept.append(row)
    return list(reversed(kept)) if focus else list(history)


def product_kind(text):
    """The requested object kind, not a plan, quantity or account condition."""
    value=str(text)
    credits=bool(re.search(r'кредит|\bcredits?\b',value,re.I))
    if credits and re.search(r'пополн|докуп|купить|покупк',value,re.I):return 'credits'
    if re.search(r'подписк',value,re.I):return 'subscription'
    if credits and re.search(r'пакет',value,re.I):return 'credits'
    if re.search(r'пополн',value,re.I):return 'balance_topup'
    return ''


def _historical_product_kind(history,catalog):
    from .catalog_context import service_mentions
    kind='';focus={}
    for row in history:
        text=row.get('text','')
        named=service_mentions(text,catalog)
        if named and focus and set(named)!=set(focus) and not entity_reference(text,catalog):
            kind=''
        explicit=product_kind(text)
        if explicit:kind=explicit
        if named:focus=named
    return kind


def entity_reference(text, catalog):
    """Recognise a whole entity-only reference, not a new independent question."""
    from .catalog_context import service_mentions, _words
    from .request_context import _service_names
    from .privacy import sensitive, scrub
    if not isinstance(text, str) or len(text)>350 or sensitive(text) or scrub(text)!=text:
        return None
    tokens=list(_words(text))
    names=service_mentions(text, catalog)
    if names and not re.search(r'кредит|\bcredits?\b',text,re.I):
        # A family name must not expand into every separately sold product.
        # With no explicit kind the catalogue may ask a choice afterwards.
        from .catalog_context import _credit_product
        regular={key:name for key,name in names.items() if not _credit_product({'service':name})}
        if regular:names=regular
    if names:
        for name in sorted(_service_names(catalog), key=len, reverse=True):
            i=0
            while i<=len(tokens)-len(name):
                if tuple(tokens[i:i+len(name)])==name:del tokens[i:i+len(name)]
                else:i+=1
        if not set(tokens)-_REFERENCE_WORDS:
            return EntityReference(tuple(names), tuple(dict.fromkeys(names.values())))
    if not names:
        # Emphatic whole labels such as «Курсорррр» are not arbitrary fuzzy
        # brands. Compress only runs of 3+ letters, considering the single and
        # double spellings, and accept exactly one known catalogue identity.
        value=text.casefold()
        runs=list(re.finditer(r'([^\W\d_])\1{2,}',value))
        if 0<len(runs)<=4:
            variants={value}
            for run in reversed(runs):
                variants={candidate[:run.start()]+run[1]*count+candidate[run.end():]
                          for candidate in variants for count in (1,2)}
            matches={}
            for variant in variants:
                known=service_mentions(variant,catalog)
                if not known:continue
                remaining=list(_words(variant))
                for name in sorted(_service_names(catalog),key=len,reverse=True):
                    for start in range(len(remaining)-len(name),-1,-1):
                        if tuple(remaining[start:start+len(name)])==name:del remaining[start:start+len(name)]
                if not set(remaining)-_REFERENCE_WORDS:
                    matches[tuple(known)]=EntityReference(tuple(known),tuple(dict.fromkeys(known.values())))
            if len(matches)==1:return next(iter(matches.values()))
    # A new public brand is a client-supplied name, not evidence of a product
    # sold by this shop. It still goes through normal discovery/availability.
    match=re.fullmatch(r'\s*(?:а|и|тогда)\s+(?:(?:на|у|для|про)\s+)?'
                       r'([A-Za-z][A-Za-z -]{1,47})\s*[?!.,]*\s*',text,re.I)
    if match:
        from .service_discovery import public_name
        label=match[1].strip()
        if public_name(label.casefold()) and label.casefold() not in {
                'google','pro','plus','basic','max','api','email','password','price','none'}:
            return EntityReference(('unverified:'+label.casefold(),),(label,),False)
    return None


def client_entities(text,catalog):
    from .catalog_context import service_mentions
    named=service_mentions(text,catalog)
    if named:return named
    reference=entity_reference(text,catalog)
    return dict(zip(reference.identities,reference.labels)) if reference else {}


def reference_intent(current, history, catalog):
    """Read actual, already freshness-validated client rows, newest intent wins."""
    from .request_goals import goals
    entities=entity_reference(current,catalog)
    plans=plan_reference(current,catalog) if not entities else []
    if plans:
        from .catalog_context import service_mentions
        focus={}
        for row in reversed(history):
            focus=client_entities(row.get('text',''),catalog)
            if focus:break
        applicable=[row for row in plans if set(service_mentions(str(row.get('service','')),catalog)) & set(focus)]
        if len(focus)==1 and applicable:
            entities=EntityReference(tuple(focus),tuple(dict.fromkeys(focus.values())))
    if not entities or goals(current):return None
    from .catalog_context import service_mentions
    focus={}
    for row in reversed(history):
        focus=client_entities(row.get('text',''),catalog)
        if focus:break
    same_entities=set(entities.identities)==set(focus)
    kind=product_kind(current) or _historical_product_kind(history,catalog)
    if kind in ('subscription','credits'):
        scope_word='подписка' if kind=='subscription' else 'пополнение кредитов'
        narrowed=service_mentions(current+'\n'+scope_word,catalog)
        if narrowed:
            entities=EntityReference(tuple(narrowed),tuple(dict.fromkeys(narrowed.values())))
        from .catalog_context import _credit_product
        scoped_focus={key:name for key,name in focus.items()
                      if _credit_product({'service':name})==(kind=='credits')}
        same_entities=set(entities.identities)==set(scoped_focus)
    for row in reversed(history):
        if row.get('role','client')!='client':continue
        text=str(row.get('text') or '')
        if re.search(r'отменяю|забудь|другая\s+тема|начн[её]м\s+заново|\bне\s+(?:нуж|буду|оформ)',text,re.I):
            return IntentReference(entities,(),None,'context_cancelled')
        if re.search(r'\b(?:оплатил[аи]?|оплачено|верни[а-яё]*|получил[аи]?)\b',text,re.I):
            return IntentReference(entities,(),row.get('mid'),'previous_intent_requires_clarification')
        intent=goals(text)
        if not intent:continue
        # Do not turn an account problem, refund request, recommendation task
        # or completed payment into a statement about a newly named product.
        if intent-set(INFORMATION_GOALS):
            return IntentReference(entities,(),row.get('mid'),'previous_intent_requires_clarification')
        return IntentReference(entities,tuple(g for g in INFORMATION_GOALS if g in intent),
                               row.get('mid'),'same_entity_reference' if same_entities else 'latest_client_question',
                               product_kind=kind,plan_labels=tuple(dict.fromkeys(str(row.get('plan','')) for row in plans)))
    return IntentReference(entities,(),None,'no_client_question')


def save_reference(db, chat, mid, reference):
    db.execute('CREATE TABLE IF NOT EXISTS turn_intent_refs '
               '(chat INTEGER,mid INTEGER,data TEXT,PRIMARY KEY(chat,mid))')
    import json
    if reference is None:
        db.execute('DELETE FROM turn_intent_refs WHERE chat=? AND mid=?',(chat,mid))
    else:
        db.execute('INSERT OR REPLACE INTO turn_intent_refs VALUES (?,?,?)',
                   (chat,mid,json.dumps(asdict(reference),ensure_ascii=False)))


def load_reference(db, chat, mid):
    import json
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_intent_refs'").fetchone()
    row=db.execute('SELECT data FROM turn_intent_refs WHERE chat=? AND mid=?',(chat,mid)).fetchone() if exists else None
    return json.loads(row['data']) if row else None


def project_turn(facts, resolved, current, reference=None):
    """Typed source of routing goals; context text is never a new user request."""
    from .request_goals import goals
    from .catalog_context import service_mentions
    own=goals(current)
    entities=service_mentions(current,facts.get('catalog',[]))
    inherited=set(reference.get('goals',())) if isinstance(reference,dict) else set()
    inherited &= set(INFORMATION_GOALS)
    # Only the reference resolver may supply this field, before model drafting.
    effective=own or inherited
    requests=clause_requests(resolved,current,facts.get('catalog',[]))
    # A short reply to a question this chat already asked is not a new unnamed topic.
    bound=bool((facts.get('dialog_state') or {}).get('bound')) if isinstance(facts,dict) else False
    return {'version':1,'current_text':current,'context_text':resolved,
            'current_goals':sorted(own),'goals':sorted(effective),
            'inherited_goals':sorted(inherited) if not own else [],
            'relation':('same_entity_reference' if reference and reference.get('reason')=='same_entity_reference' else
                        'entity_reference' if reference else 'current_request'),
            'current_act':('question' if own else 'plan_reference' if reference and reference.get('plan_labels') else 'entity_reference' if reference else
                           'parameter' if resolved.strip()!=current.strip() else 'unspecified'),
            'entities':list(entities),
            'source_mid':reference.get('source_mid') if reference else None,
            'requests':requests,
            'clarification_required':bool(not bound and reference and (not effective or reference.get('reason')=='unavailable_client_context')),
            'reference_reason':reference.get('reason') if reference else '',
            'context_condition':reference.get('context_condition','') if reference else '',
            'checked_sources':reference.get('checked_sources',0) if reference else 0,
            'missing_sources':reference.get('missing_sources',0) if reference else 0,
            'product_kind':product_kind(current) or (reference.get('product_kind','') if reference else ''),
            'reference_labels':reference.get('entities',{}).get('labels',[]) if reference else [],
            'plan_labels':reference.get('plan_labels',[]) if reference else []}


def intent_questions(goals,kind=''):
    prompts=_PROMPTS
    names={'subscription':('подписка','подписки','подписку'),
           'credits':('пополнение кредитов','пополнения кредитов','пополнение кредитов'),
           'balance_topup':('пополнение баланса','пополнения баланса','пополнение баланса')}
    if kind in names:
        nominative,genitive,accusative=names[kind]
        prompts={'price':f'Сколько стоит {nominative}?',
                 'purchase':f'Что нужно для оформления {genitive}?',
                 'features':f'Какие возможности и ограничения у {genitive}?',
                 'activation':f'Как активировать {accusative}?',
                 'installation':f'Как установить приложение для использования {genitive}?'}
    return '\n'.join(prompts[g] for g in INFORMATION_GOALS if g in goals)


def clause_requests(resolved,current,catalog):
    """Bind each current question to its entity, preserving actual qualifiers."""
    from .catalog_context import service_mentions
    from .request_goals import goals
    prefix=resolved[:-len(current)] if current and resolved.endswith(current) else ''
    focus={}
    for line in prefix.splitlines():
        mentioned=service_mentions(line,catalog)
        if mentioned:focus=mentioned
    requests=[]
    context_focus=bool(focus)
    for clause in re.split(r'(?<=[?!])\s*|\n+',current):
        if not clause.strip():continue
        named=service_mentions(clause,catalog)
        intent=goals(clause)
        if named and not intent and requests and re.match(r'^\s*(?:и|или|также|ещ[её])\b',clause,re.I):
            # A collected entity list may wrap onto the next message/line.
            # «Какие тарифы A / и B? / Что входит?» addresses both products.
            focus={**focus,**named}
            previous=requests[-1]
            previous['entities']=list(focus)
            previous['labels']=list(dict.fromkeys(focus.values()))
            previous['text']+=' '+clause.strip()
            context_focus=False
            continue
        if named:
            focus=named
            context_focus=False
        if intent:
            requests.append({'entities':list(focus),'labels':list(dict.fromkeys(focus.values())),
                             'goals':sorted(intent),'text':clause.strip(),
                             'explicit_entities':bool(named),
                             'context_entity_reference':context_focus and not named})
    return requests


def scoped_current_question(turn):
    """Only disambiguate multi-entity questions; do not rewrite their details."""
    requests=turn.get('requests',[])
    targets={tuple(r['entities']) for r in requests if r['entities']}
    if len(targets)<2 or any(not r['entities'] for r in requests) or not any(
            r.get('context_entity_reference') for r in requests):return None
    return '\n'.join(', '.join(r['labels'])+': '+r['text'] for r in requests)
