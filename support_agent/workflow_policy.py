"""Owner-approved facts only. No historical prices, runtime snapshots or secrets."""
import re


def policies_for(facts):
    rows=facts.get('requested_catalog',[])
    names=' '.join(str(r.get(k,'')) for r in rows if isinstance(r,dict)
                   for k in ('service_key','service'))
    current=str(facts.get('conversation',{}).get('latest_client_message',''))
    if not re.search(r'(?<!\w)(?:krea|креа)(?!\w)',names+' '+current,re.I):
        return []
    return [{'service':'krea','source':'owner_confirmed','confirmed_on':'2026-10-29',
             'required_fields':['account_email','account_password'],
             'delivery_channel':'personally_to_alexander_telegram',
             'customer_instruction':'После оплаты пришлите необходимые данные сюда, в эту переписку. Не направлять клиента в другой чат. Дальше требуется ручное оформление владельцем.',
             'agent_can_login':False,'agent_can_pay':False,
             'agent_collects_credentials':False,
             'duration':'not_confirmed','success_criterion':'not_confirmed'}]


def enrich(facts):
    return dict(facts,owner_workflow_policies=policies_for(facts))
