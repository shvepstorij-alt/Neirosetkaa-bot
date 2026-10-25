"""Owner-only browser of retained messages, without extra model/API requests."""
import datetime as dt
import html
import json


PAGE_SIZE=5
PREVIEW_UNITS=150
PART_UNITS=1500
LOCAL_TZ=dt.timezone(dt.timedelta(hours=5))


def text_parts(text,limit=PART_UNITS):
    """Split by Telegram UTF-16 length without losing characters or splitting emoji."""
    if not text:
        return ['']
    parts=[];chars=[];units=0
    for char in text:
        width=2 if ord(char)>0xffff else 1
        if chars and units+width>limit:
            parts.append(''.join(chars));chars=[];units=0
        chars.append(char);units+=width
    parts.append(''.join(chars))
    return parts


def _number(value,default=0):
    try:
        number=int(value)
        return number if 0<=number<=9223372036854775807 else default
    except (ValueError,TypeError,OverflowError):
        return default


def _button(text,cid,page,mid=0,part=0):
    return {'text':text,'callback_data':f'dlg:{cid}:{page}:{mid}:{part}','style':'success'}


def _time(value):
    try:
        return dt.datetime.fromtimestamp(float(value),LOCAL_TZ).strftime('%d.%m.%Y %H:%M')
    except (TypeError,ValueError,OverflowError,OSError):
        return 'время не сохранено'


def _media_label(raw):
    try:
        value=json.loads(raw or '{}')
        kind=value.get('kind') if isinstance(value,dict) else None
    except (TypeError,ValueError):
        kind=None
    return {'photo':'📷 Фото — оригинал в личке',
            'voice':'🎤 Голосовое — оригинал в личке',
            'unsupported':'📎 Вложение — оригинал в личке',
            'sensitive':'🔒 Сообщение скрыто: возможны данные доступа'}.get(kind,'')


async def show_dialog(service,cid,page=0,expanded_mid=None,part=0,admin_mid=None):
    cid=_number(cid);page=_number(page);part=_number(part)
    expanded_mid=_number(expanded_mid)
    chat=service.store.chat(cid)
    if not chat:
        await service.admin_panel('Чат не найден.',[[('Все чаты','chats:0')]],admin_mid,
                                  parse_mode='HTML')
        return
    db=service.store.db
    count=db.execute('SELECT count(*) FROM messages WHERE chat=?',(cid,)).fetchone()[0]
    pages=max(1,(count+PAGE_SIZE-1)//PAGE_SIZE)
    page=min(page,pages-1)
    missing=False
    if expanded_mid:
        target=db.execute('SELECT mid FROM messages WHERE chat=? AND mid=?',(cid,expanded_mid)).fetchone()
        if target:
            # Stable message identity: an old button still opens its own message
            # even after retention cleanup moved the page boundary.
            before=db.execute('SELECT count(*) FROM messages WHERE chat=? AND mid<?',
                              (cid,expanded_mid)).fetchone()[0]
            page=before//PAGE_SIZE
        else:
            missing=True;expanded_mid=0;part=0
    rows=db.execute('SELECT mid,role,text,media,at FROM messages WHERE chat=? ORDER BY mid LIMIT ? OFFSET ?',
                    (cid,PAGE_SIZE,page*PAGE_SIZE)).fetchall()
    label=html.escape(service.chat_label(cid,chat['uid']))
    blocks=[f'💬 <b>Сохранённый диалог · чат №{cid}</b>\n{label} · ID {chat["uid"]}',
            f'<i>Сообщений: {count} · страница {page+1}/{pages} · время UTC+5</i>',
            '<i>Это сообщения, полученные помощником за срок хранения 7 суток, '
            'а не вся история Telegram. Скрытые данные и удалённые сообщения недоступны.</i>']
    if missing:
        blocks.append('<i>Выбранное сообщение уже удалено или недоступно в этом чате.</i>')
    buttons=[]
    roles={'client':'Клиент','owner':'Александр · вручную','assistant':'Помощник · одобрено владельцем'}
    for row in rows:
        raw=row['text'] or ''
        role=roles.get(row['role'],'Сообщение')
        mid=row['mid']
        head=f'<b>{role}</b> · {_time(row["at"])} · №{mid}'
        media=_media_label(row['media'])
        expanded=mid==expanded_mid
        if expanded:
            chunks=text_parts(raw);current=min(part,len(chunks)-1)
            body=chunks[current] or '[Текст отсутствует]'
            if len(chunks)>1:
                head+=f'\n<i>Часть {current+1}/{len(chunks)} полного сообщения</i>'
            nav=[]
            if current:nav.append(_button('← Предыдущая часть',cid,page,mid,current-1))
            if current+1<len(chunks):nav.append(_button('Следующая часть →',cid,page,mid,current+1))
            buttons.append([_button(f'Свернуть №{mid}',cid,page)])
            if nav:buttons.append(nav)
        else:
            chunks=text_parts(raw,PREVIEW_UNITS)
            body=(chunks[0]+'…' if len(chunks)>1 else chunks[0]) or '[Текст отсутствует]'
            buttons.append([_button(f'Развернуть №{mid}',cid,page,mid)])
        block=head+'\n<blockquote>'+html.escape(body)+'</blockquote>'
        if media:block+='\n<i>'+media+'</i>'
        blocks.append(block)
    if not rows:
        blocks.append('Сохранённых сообщений пока нет.')
    nav=[]
    if page:nav.append(_button('← Раньше',cid,page-1))
    if page+1<pages:nav.append(_button('Позже →',cid,page+1))
    if nav:buttons.append(nav)
    buttons.append([_button('К последним',cid,pages-1),_button('Обновить',cid,page,expanded_mid,part)])
    buttons.append([('Карточка чата',f'view:{cid}'),('Все чаты','chats:0')])
    await service.admin_panel('\n\n'.join(blocks),buttons,admin_mid,parse_mode='HTML')
