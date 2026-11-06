"""Pure, deterministic suggestions from a reviewed source snapshot; no model call."""
from catalog_descriptions import describe, CHECKED_DATE, _DATA, _identity


def suggestions(key, service):
    def preview(result):
        return {'description':result['text'],'sources':result['source_urls'],
                'status':result['status'],'limitation':result['limitation']}
    result=describe(key,service.get('name',''))
    row=_DATA['services'].get(result['service_id'],{})
    svc=preview(result);svc['name']=service.get('name','')
    plans=[]
    for index,plan in enumerate(service.get('plans',[])):
        current=str(plan.get('name',''))
        result=describe(key,service.get('name',''),current)
        item=preview(result)
        official=next((name for name,entry in row.get('plans',{}).items()
                       if _identity(name)==_identity(current) and entry.get('status')=='verified_plan'),None)
        item.update(planIdx=plan.get('_db_plan_idx',index),currentName=current,suggestedName=official)
        plans.append(item)
    return {'service':svc,'plans':plans,
            'officialPlanNames':[name for name,entry in row.get('plans',{}).items() if entry.get('status')=='verified_plan']}
