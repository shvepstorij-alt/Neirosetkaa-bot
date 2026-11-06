"""Versioned reviewed descriptions and explicit owner-confirmed Suno route.

Only description columns/settings and Suno method flags are touched. Backup is
stored in the database; never contains credentials, customer data or prices.
"""
import json
from catalog_descriptions import REVIEW_VERSION, describe


async def apply_reviewed_catalog(conn, catalog, *, force=False):
    marker='catalog_review_applied:'+REVIEW_VERSION
    updates=[]
    async with conn.transaction():
        # Serialise concurrent starts before checking the version marker.
        await conn.execute("SELECT pg_advisory_xact_lock(74110601)")
        done=await conn.fetchval('SELECT value FROM settings WHERE key=$1',marker)
        if done=='1' and not force:return False
        backup=[]
        for key,service in catalog.items():
            service_text=describe(key,service.get('name',''))['text']
            backup.append({'key':key,'service_desc':service.get('desc',''),
                           'plans':[{'id':p.get('_db_plan_idx',i),'name':p.get('name',''),'desc':p.get('desc','')}
                                    for i,p in enumerate(service.get('plans',[]))]})
            await conn.execute('UPDATE bot_shop_items SET service_desc=$1 WHERE key=$2',service_text,key)
            await conn.execute("INSERT INTO shop_desc_overrides(key,plan_name,descr) VALUES($1,'',$2) ON CONFLICT(key,plan_name) DO UPDATE SET descr=EXCLUDED.descr,updated_at=NOW()",key,service_text)
            plans=[]
            for index,plan in enumerate(service.get('plans',[])):
                text=describe(key,service.get('name',''),plan.get('name',''))['text']
                stable_id=plan.get('_db_plan_idx',index)
                await conn.execute('UPDATE bot_shop_items SET plan_desc=$1 WHERE key=$2 AND plan_idx=$3',text,key,stable_id)
                await conn.execute('DELETE FROM shop_desc_overrides WHERE key=$1 AND plan_name=$2',key,plan.get('name',''))
                await conn.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',f'admin_plan_desc:{key}:{stable_id}',text)
                plans.append((plan,text))
            updates.append((service,service_text,plans))
        await conn.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO NOTHING',marker+':backup',json.dumps(backup,ensure_ascii=False))
        await conn.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',marker,'1')
    # Only mutate runtime after the database transaction committed successfully.
    for service,text,plans in updates:
        service['desc']=text
        for plan,description in plans:plan['desc']=description
    return True


async def align_suno_method(conn, catalog):
    marker='owner_suno_account_details_v1'
    async with conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(74110602)')
        if await conn.fetchval('SELECT value FROM settings WHERE key=$1',marker)=='1':return False
        keys=[key for key,svc in catalog.items() if str(key).casefold()=='suno' or str(svc.get('name','')).strip().casefold()=='suno']
        if not keys:return False
        backup={}
        for key in keys:
            for prefix,value in [('linkpay:enabled:','0'),('creds:enabled:','1')]:
                setting=prefix+key
                backup[setting]=await conn.fetchval('SELECT value FROM settings WHERE key=$1',setting)
                await conn.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',setting,value)
        await conn.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO NOTHING',marker+':backup',json.dumps(backup))
        await conn.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',marker,'1')
    return True
