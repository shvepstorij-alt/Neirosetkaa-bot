"""Validated administrative catalogue edits and idempotent coin grants.

No network actions, customer messages, credential data or order rewriting.
"""
import hashlib
import json
import re
import uuid
from decimal import Decimal, InvalidOperation


class AdminInputError(ValueError):
    pass


class AdminConflict(ValueError):
    pass


def text_field(value, name, maximum, required=False):
    if not isinstance(value,str):raise AdminInputError(name+': требуется текст')
    value=value.strip()
    if required and not value:raise AdminInputError(name+': поле не заполнено')
    if len(value)>maximum:raise AdminInputError(name+': превышена длина '+str(maximum))
    if any(ord(c)<32 and c not in '\n\t' for c in value):raise AdminInputError(name+': недопустимые управляющие символы')
    return value


def integer(value, name, low, high):
    if isinstance(value,bool) or not re.fullmatch(r'[0-9]+',str(value)):
        raise AdminInputError(name+': требуется целое число')
    number=int(value)
    if not low<=number<=high:raise AdminInputError(name+': допустимо от '+str(low)+' до '+str(high))
    return number


def validate_grant(body):
    uid=integer(body.get('id'),'ID клиента',1,2**63-1)
    amount=integer(body.get('amount'),'Монеты',1,1_000_000)
    reason=text_field(body.get('reason'),'Причина',240,True)
    try:request_id=str(uuid.UUID(str(body.get('request_id',''))))
    except (ValueError,TypeError):raise AdminInputError('Идентификатор операции: требуется UUID') from None
    return uid,amount,reason,request_id


async def grant_coins(conn, admin_id, uid, amount, reason, request_id):
    """Caller owns a transaction. Unique request ID serializes duplicate retries."""
    inserted=await conn.fetchrow(
        'INSERT INTO admin_coin_grants (request_id,admin_id,user_id,amount,reason) VALUES ($1,$2,$3,$4,$5) '
        'ON CONFLICT (request_id) DO NOTHING RETURNING request_id',request_id,admin_id,uid,amount,reason)
    if not inserted:
        prior=await conn.fetchrow('SELECT admin_id,user_id,amount,reason FROM admin_coin_grants WHERE request_id=$1',request_id)
        if not prior or (prior['admin_id'],prior['user_id'],int(prior['amount']),prior['reason'])!=(admin_id,uid,amount,reason):
            raise AdminConflict('Идентификатор операции уже использован с другими данными; повторное начисление не выполнено')
        balance=await conn.fetchval('SELECT COALESCE(coins,0) FROM users WHERE user_id=$1',uid)
        return {'balance':float(balance or 0),'credited':0,'replayed':True,'request_id':request_id}
    row=await conn.fetchrow('UPDATE users SET coins=COALESCE(coins,0)+$1 WHERE user_id=$2 AND COALESCE(coins,0)+$1<=99999999.99 RETURNING coins',amount,uid)
    if not row:
        exists=await conn.fetchval('SELECT EXISTS(SELECT 1 FROM users WHERE user_id=$1)',uid)
        if not exists:raise LookupError('Клиент не найден; начисление не выполнено')
        raise AdminInputError('Начисление превысит максимальный баланс 99999999.99 монет; монеты не начислены')
    await conn.execute('INSERT INTO events (user_id,kind,data) VALUES ($1,$2,$3)',uid,'coins',
                       '+'+str(amount)+' ₽ — '+reason+'; admin='+str(admin_id)+'; operation='+request_id)
    await conn.execute('UPDATE admin_coin_grants SET balance_after=$1 WHERE request_id=$2',row['coins'],request_id)
    return {'balance':float(row['coins']),'credited':amount,'replayed':False,'request_id':request_id}


def revision(service):
    fields={k:service.get(k) for k in ('key','name','emoji','desc','plans')}
    return hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def validate_catalog_edit(body):
    key=text_field(body.get('key'),'Ключ сервиса',160,True)
    rev=body.get('revision')
    if not isinstance(rev,str) or not re.fullmatch('[0-9a-f]{64}',rev):
        raise AdminInputError('Версия каталога отсутствует: обновите страницу перед редактированием')
    name=text_field(body.get('serviceName'),'Название сервиса',120,True)
    emoji=text_field(body.get('emoji',''),'Эмодзи сервиса',80)
    if any(c in name+emoji for c in '<>'):raise AdminInputError('Название и эмодзи сервиса должны быть обычным текстом без HTML')
    desc=text_field(body.get('serviceDesc',''),'Описание сервиса',12000)
    plans=body.get('plans')
    if not isinstance(plans,list) or not 1<=len(plans)<=500:raise AdminInputError('Тарифы: требуется непустой список до 500 позиций')
    out=[];seen=set()
    for row in plans:
        if not isinstance(row,dict):raise AdminInputError('Тариф: неверная структура')
        pid=integer(row.get('planId'),'ID тарифа',0,2**31-1)
        if pid in seen:raise AdminInputError('ID тарифа повторён; обновите страницу')
        seen.add(pid)
        plan={'planId':pid,'name':text_field(row.get('name'),'Название тарифа',160,True),
              'price':integer(row.get('price'),'Цена тарифа',1,100_000_000),
              'desc':text_field(row.get('desc',''),'Описание тарифа',12000)}
        if any(c in plan['name'] for c in '<>'):raise AdminInputError('Название тарифа должно быть обычным текстом без HTML')
        try:
            if isinstance(row.get('costUsd'),bool):raise ValueError()
            cost=Decimal(str(row.get('costUsd',0)).replace(',','.'))
            if not cost.is_finite() or not 0<=cost<=100_000_000:raise ValueError()
        except (ValueError,InvalidOperation):raise AdminInputError('Закуп: требуется число от 0 до 100000000') from None
        if type(row.get('manual')) is not bool:raise AdminInputError('Ручная выдача: требуется переключатель да/нет')
        plan.update(costUsd=float(cost),manual=row['manual'])
        for route in ('Ph','Ios'):
            field='cost'+route
            if field not in row:continue
            if key!='chatgpt':raise AdminInputError('Закуп по маршруту доступен только для ChatGPT')
            try:
                if isinstance(row[field],bool):raise ValueError()
                route_cost=Decimal(str(row[field]).replace(',','.'))
                if not route_cost.is_finite() or not 0<=route_cost<=100_000_000:raise ValueError()
            except (ValueError,InvalidOperation):raise AdminInputError(field+': требуется число от 0 до 100000000') from None
            plan[field]=float(route_cost)
        out.append(plan)
    return {'key':key,'revision':rev,'name':name,'emoji':emoji,'desc':desc,'plans':out}


def validate_renamed_labels(before,after):
    from collections import Counter
    old=Counter(p['name'].strip().casefold() for p in before)
    new=Counter(p['name'].strip().casefold() for p in after)
    if any(count>1 and count>old[label] for label,count in new.items()):
        raise AdminInputError('Переименование создаёт одинаковые названия тарифов в одном сервисе; задайте разные названия. Существующие неизменённые дубликаты допустимы')
