"""Owner-only delivery of media referenced on a retained dialogue page."""
import asyncio
import json
import time

async def send_page_media(service,cid,mids):
    chat=service.store.chat(cid)
    if not chat or chat['uid'] not in service.allowed:return
    if not hasattr(service,'dialog_media_lock'):
        service.dialog_media_lock=asyncio.Lock()
        service.dialog_media_attempts={}
    async with service.dialog_media_lock:
        now=time.monotonic()
        service.dialog_media_attempts={k:v for k,v in service.dialog_media_attempts.items() if now-v<600}
        for mid in dict.fromkeys(mids):
            row=service.store.db.execute('SELECT media FROM messages WHERE chat=? AND mid=?',(cid,mid)).fetchone()
            if not row:continue
            try:media=json.loads(row['media'] or '{}')
            except (TypeError,ValueError):continue
            if not isinstance(media,dict) or media.get('kind') not in ('photo','voice') or media.get('blocked_transcript'):continue
            ident=media.get('file_id')
            if not isinstance(ident,str) or not ident:continue
            key=(cid,mid,ident)
            if key in service.dialog_media_attempts:continue
            # Mark before sending: uncertain Telegram outcomes must not auto-replay.
            if len(service.dialog_media_attempts)>=1000:
                service.dialog_media_attempts.pop(next(iter(service.dialog_media_attempts)))
            service.dialog_media_attempts[key]=time.monotonic()
            kind=media['kind']
            field='voice' if kind=='voice' else 'document' if media.get('delivery_kind')=='document' else 'photo'
            method={'voice':'sendVoice','document':'sendDocument','photo':'sendPhoto'}[field]
            caption=service.chat_identity(cid)+f'\nСообщение №{mid} · '+('Голосовое' if kind=='voice' else 'Изображение')
            try:
                async with service.owner_delivery_lock:
                    delay=service.owner_interval-(time.monotonic()-service.owner_last_send)
                    if delay>0:await asyncio.sleep(delay)
                    current=service.store.db.execute('SELECT media FROM messages WHERE chat=? AND mid=?',(cid,mid)).fetchone()
                    if not current or current['media']!=row['media']:continue
                    service.owner_last_send=time.monotonic()
                    await service.tg.call(method,chat_id=service.owner,**{field:ident},caption=caption)
            except Exception:
                await service.owner_message(service.chat_identity(cid)+f'\nНе удалось подтвердить отправку вложения №{mid}. Проверьте оригинал в личке; автоматически повторять отправку не буду.')
