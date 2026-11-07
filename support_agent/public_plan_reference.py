"""Recognise a catalogue plan label at intake without masking secret values."""
import hashlib
import hmac
import json
import secrets

_KEY=secrets.token_bytes(32)


def _payload(current,plans):
    return json.dumps([current,plans],ensure_ascii=False,sort_keys=True).encode('utf-8')


def attach(answer,facts,current=''):
    from .turn_contract import plan_reference
    from .privacy import scrub,redaction_categories
    current=str(current or facts.get('conversation_turn',{}).get('current_text',''))
    if not current or scrub(current)!=current or redaction_categories(current):return answer
    rows=plan_reference(current,facts.get('catalog',[]))
    if not rows:return answer
    plans=sorted({str(row.get('plan','')) for row in rows})
    proof=hmac.new(_KEY,_payload(current,plans),hashlib.sha256).hexdigest()
    result=dict(answer);result['evidence']=dict(answer.get('evidence',{}))
    result['evidence']['public_plan_reference']={'plans':plans,'proof':proof}
    return result


def valid(current,evidence):
    contract=evidence.get('public_plan_reference',{})
    if not isinstance(contract,dict) or set(contract)!={'plans','proof'}:return False
    if not isinstance(contract['proof'],str):return False
    try:expected=hmac.new(_KEY,_payload(str(current),contract['plans']),hashlib.sha256).hexdigest()
    except (TypeError,ValueError):return False
    return hmac.compare_digest(contract['proof'],expected)
