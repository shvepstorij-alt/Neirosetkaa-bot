"""Owner-only browser of retained messages, without extra model/API requests."""
import datetime as dt
import html
import json


PAGE_SIZE=5
PAGE_BODY_UNITS=3000
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
    return {'text':text,'callback_data':f'dlg:{cid}:{page}:{mid}:{part}'}


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
    return {'photo':'📷 Фото — отправляется отдельным вложением при открытии страницы',
            'voice':'🎤 Голосовое — отправляется отдельным вложением при открытии страницы',
            'unsupported':'📎 Вложение — оригинал в личке',
            'sensitive':'🔒 Сообщение скрыто: возможны данные доступа'}.get(kind,'')


async def show_dialog(service,cid,page=None,expanded_mid=None,part=0,admin_mid=None):
    open_latest=page is None
    cid=_number(cid);page=_number(page);part=_number(part)
    expanded_mid=_number(expanded_mid)
    chat=service.store.chat(cid)
    if not chat:
        await service.admin_panel('Чат не найден.',[[('Все чаты','chats:0')]],admin_mid,
                                  parse_mode='HTML')
        return
    db=service.store.db
    # Paginate complete quoted text, including text hidden by Telegram's native
    # expand arrow. Hidden text still counts towards the message length limit.
    rows=db.execute('SELECT mid,role,text,media,at FROM messages WHERE chat=? ORDER BY mid',
                    (cid,)).fetchall()
    count=len(rows)
    roles={'client':'Клиент','owner':'Александр · вручную','assistant':'Помощник · одобрено владельцем'}
    packed=[[]];page_mids=[[]];used=0;target_page=None
    for row in rows:
        chunks=text_parts(row['text'] or '[Текст отсутствует]')
        for index,body in enumerate(chunks):
            head=f'<b>{roles.get(row["role"],"Сообщение")}</b> · {_time(row["at"])} · №{row["mid"]}'
            if len(chunks)>1:
                head+=f'\n<i>Часть {index+1}/{len(chunks)} полного сообщения</i>'
            media=_media_label(row['media'])
            block=head+'\n<blockquote expandable>'+html.escape(body)+'</blockquote>'
            if media:block+='\n<i>'+media+'</i>'
            visible=head.replace('<b>','').replace('</b>','').replace('<i>','').replace('</i>','')+'\n'+body+'\n'+media
            units=len(visible.encode('utf-16-le'))//2+2
            if packed[-1] and (used+units>PAGE_BODY_UNITS or len(packed[-1])>=PAGE_SIZE):
                packed.append([]);page_mids.append([]);used=0
            packed[-1].append(block);page_mids[-1].append(row["mid"]);used+=units
            # Accept old per-message callbacks without restoring their keyboard.
            if row['mid']==expanded_mid and index==min(part,len(chunks)-1):
                target_page=len(packed)-1
    pages=len(packed)
    missing=bool(expanded_mid and target_page is None)
    page=target_page if target_page is not None else pages-1 if open_latest else min(page,pages-1)
    label=html.escape(service.chat_label(cid,chat['uid']))
    blocks=[f'💬 <b>Сохранённый диалог · чат №{cid}</b>\n{label} · ID {chat["uid"]}',
            f'<i>Сообщений: {count} · страница {page+1}/{pages} · время UTC+5</i>',
            '<i>Это сообщения, полученные помощником за срок хранения 7 суток, '
            'а не вся история Telegram. Скрытые данные и удалённые сообщения недоступны.</i>']
    if missing:
        blocks.append('<i>Выбранное сообщение уже удалено или недоступно в этом чате.</i>')
    buttons=[]
    blocks.extend(packed[page])
    if not rows:
        blocks.append('Сохранённых сообщений пока нет.')
    nav=[]
    if page:nav.append(_button('← Раньше',cid,page-1))
    if page+1<pages:nav.append(_button('Позже →',cid,page+1))
    if nav:buttons.append(nav)
    buttons.append([_button('К последним',cid,pages-1),_button('Обновить',cid,page)])
    buttons.append([('Карточка чата',f'view:{cid}'),('Все чаты','chats:0')])
    await service.admin_panel('\n\n'.join(blocks),buttons,admin_mid,parse_mode='HTML')
    from .dialog_media import send_page_media
    await send_page_media(service,cid,page_mids[page])
