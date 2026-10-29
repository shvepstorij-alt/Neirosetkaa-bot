"""Owner-approved facts only. No historical prices, runtime snapshots or secrets."""
import re


def policies_for(facts):
    rows=facts.get('requested_catalog',[])
    names=' '.join(str(r.get(k,'')) for r in rows if isinstance(r,dict)
                   for k in ('service_key','service'))
    current=str(facts.get('conversation',{}).get('latest_client_message',''))
    services=[service for service,pattern in (('krea',r'krea|креа'),('suno',r'suno|суно'),
              ('zoom',r'zoom|зум|зуме|зума'),('openrouter',r'openrouter|open router|опенроутер'),
              ('spotify',r'spotify|спотифай'),('youtube',r'youtube|you tube|ютуб|ютубе'))
              if re.search(r'(?<!\w)(?:'+pattern+r')(?!\w)',names+' '+current,re.I)]
    return [{'service':service,'source':'owner_confirmed','confirmed_on':'2026-10-29',
             'expected_fulfillment':'account_details',
             'required_fields':['account_email','account_password'],
             'delivery_channel':'personally_to_alexander_telegram',
             'customer_instruction':'После оплаты пришлите необходимые данные сюда, в эту переписку. Не направлять клиента в другой чат. Дальше требуется ручное оформление владельцем.',
             'agent_can_login':False,'agent_can_pay':False,
             'agent_collects_credentials':False,
             'google_sign_in':({'required_fields':['google_account_email','google_account_password'],
                                'condition':'only_when_customer_uses_google_sign_in',
                                'never_request':['one_time_code','backup_code','session_token']}
                               if service in ('suno','zoom','openrouter','spotify','youtube') else None),
             'duration':'not_confirmed','success_criterion':'not_confirmed'} for service in services]


def enrich(facts):
    return dict(facts,owner_workflow_policies=policies_for(facts))
