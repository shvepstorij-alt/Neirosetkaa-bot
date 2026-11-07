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

    @property
    def question(self):
        return ', '.join(self.entities.labels)+'\n'+intent_questions(self.goals,self.product_kind)


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


def reference_intent(current, history, catalog):
    """Read actual, already freshness-validated client rows, newest intent wins."""
    from .request_goals import goals
    entities=entity_reference(current,catalog)
    if not entities or goals(current):return None
    from .catalog_context import service_mentions
    focus={}
    for row in reversed(history):
        focus=service_mentions(row.get('text',''),catalog)
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
                               product_kind=kind)
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
    return {'version':1,'current_text':current,'context_text':resolved,
            'current_goals':sorted(own),'goals':sorted(effective),
            'inherited_goals':sorted(inherited) if not own else [],
            'relation':('same_entity_reference' if reference and reference.get('reason')=='same_entity_reference' else
                        'entity_reference' if reference else 'current_request'),
            'current_act':('question' if own else 'entity_reference' if reference else
                           'parameter' if resolved.strip()!=current.strip() else 'unspecified'),
            'entities':list(entities),
            'source_mid':reference.get('source_mid') if reference else None,
            'requests':requests,
            'clarification_required':bool(reference and (not effective or reference.get('reason')=='unavailable_client_context')),
            'reference_reason':reference.get('reason') if reference else '',
            'context_condition':reference.get('context_condition','') if reference else '',
            'checked_sources':reference.get('checked_sources',0) if reference else 0,
            'missing_sources':reference.get('missing_sources',0) if reference else 0,
            'product_kind':product_kind(current) or (reference.get('product_kind','') if reference else ''),
            'reference_labels':reference.get('entities',{}).get('labels',[]) if reference else []}


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
