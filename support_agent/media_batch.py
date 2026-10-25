"""Bounded, chronological media collection for one unanswered client turn."""
import json
import math

from .clients import normalize_image
from .privacy import scrub, sensitive

MAX_PHOTOS=3
MAX_VOICES=2
MAX_VOICE_SECONDS=180
MAX_FILE_BYTES=8_000_000
MAX_TOTAL_BYTES=16_000_000


def _manual(reason):
    return [], {'reply':'Уточню детали вашего обращения и вернусь с ответом.',
                'summary':reason+' Все вложения автоматически не разобраны; проверьте обращение в личке.',
                'handoff':True}


def _number(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)):
        raise ValueError('Invalid media metadata')
    if not math.isfinite(value) or value<0:
        raise ValueError('Invalid media metadata')
    return value


def _save_media(service,job,row,media,text=None):
    # No await between revision check and update: edits cannot overwrite this row.
    if not service.store.still_current(job):return False
    if text is None:
        service.store.db.execute('UPDATE messages SET media=? WHERE chat=? AND mid=?',
                                (json.dumps(media,ensure_ascii=False),job['chat'],row['mid']))
    else:
        service.store.db.execute('UPDATE messages SET media=?,text=? WHERE chat=? AND mid=?',
                                (json.dumps(media,ensure_ascii=False),text,job['chat'],row['mid']))
    return True


async def prepare_media(service,job):
    """Return (images, manual_notice); (None, None) means job became stale.

    Files are validated as a whole before any paid transcription. Successful
    sanitized transcripts persist in the message's media metadata; uncertain
    transcription attempts never automatically consume the budget again.
    """
    if not service.store.still_current(job):return None,None
    rows=service.store.db.execute('''SELECT mid,text,media FROM messages
        WHERE chat=? AND role='client' AND mid<=? AND mid>COALESCE(
          (SELECT MAX(mid) FROM messages WHERE chat=? AND role IN ('owner','assistant') AND mid<?),0)
        ORDER BY mid''',(job['chat'],job['mid'],job['chat'],job['mid'])).fetchall()
    files=[];photos=voices=0;duration=declared=0
    # Preflight the entire turn, including unsupported media among valid files.
    for row in rows:
        try:
            media=json.loads(row['media'] or '{}')
            if not isinstance(media,dict):raise ValueError()
            kind=media.get('kind','none')
            if kind=='none':continue
            if kind=='sensitive' or media.get('blocked_transcript'):
                return _manual('В обращении возможны данные доступа. Они не переданы в подготовку ответа.')
            if kind not in ('photo','voice'):
                return _manual('В обращении есть неподдерживаемое вложение.')
            if not isinstance(media.get('file_id'),str) or not media['file_id']:
                raise ValueError()
            size=_number(media.get('file_size',0))
            if size>MAX_FILE_BYTES:
                return _manual('Один файл превышает лимит 8 МБ.')
            declared+=size
            if kind=='photo':photos+=1
            else:
                voices+=1
                duration+=_number(media.get('duration'))
            if photos>MAX_PHOTOS or voices>MAX_VOICES or duration>MAX_VOICE_SECONDS:
                return _manual('Превышен лимит обращения: 3 изображения, 2 голосовых, суммарно 180 секунд голоса.')
            if declared>MAX_TOTAL_BYTES:
                return _manual('Суммарный размер вложений превышает 16 МБ.')
            cached=media.get('transcript') if kind=='voice' else None
            if cached is not None:
                if not isinstance(cached,str) or not cached.strip() or len(cached)>16000:
                    raise ValueError()
                if sensitive(cached) or scrub(cached)!=cached:
                    return _manual('Сохранённая расшифровка требует ручной проверки.')
                _number(media.get('transcript_bytes'))
            elif kind=='voice' and media.get('transcription_state') in ('pending','failed'):
                return _manual('Предыдущая расшифровка не завершилась подтверждённым результатом. Повторный платный запрос не запущен.')
            files.append((row,media,cached))
        except (ValueError,TypeError,OverflowError):
            return _manual('Не удалось безопасно проверить параметры вложения.')
    images=[];voices_to_process=[];actual=0
    for row,media,cached in files:
        if not service.store.still_current(job):return None,None
        if cached is not None:
            size=media['transcript_bytes']
            if size>MAX_FILE_BYTES:return _manual('Сохранённые параметры файла превышают лимит.')
            actual+=size
            if actual>MAX_TOTAL_BYTES:return _manual('Суммарный размер вложений превышает 16 МБ.')
            continue
        try:
            data=await service.tg.download(media['file_id'])
            if not service.store.still_current(job):return None,None
            if not isinstance(data,bytes) or not data or len(data)>MAX_FILE_BYTES:
                return _manual('Вложение пустое или превышает лимит 8 МБ.')
            actual+=len(data)
            if actual>MAX_TOTAL_BYTES:return _manual('Суммарный размер вложений превышает 16 МБ.')
            if media['kind']=='photo':
                images.append({'mid':row['mid'],'caption':row['text'] or '', 'data':normalize_image(data)})
            else:
                if not data.startswith(b'OggS'):
                    return _manual('Формат голосового не поддерживается: требуется Telegram OGG.')
                voices_to_process.append((row,media,data))
        except Exception:
            if not service.store.still_current(job):return None,None
            return _manual('Не удалось загрузить или проверить одно из вложений. Анализ неполного набора не запускался.')
    for row,media,data in voices_to_process:
        if not service.store.still_current(job):return None,None
        try:
            service.charge_call()
        except Exception:
            return _manual('Не удалось выделить лимит API для расшифровки голосового.')
        media=dict(media,transcription_state='pending')
        if not _save_media(service,job,row,media):return None,None
        try:
            transcript=await service.ai.transcribe(data)
        except Exception:
            if not service.store.still_current(job):return None,None
            _save_media(service,job,row,dict(media,transcription_state='failed'))
            return _manual('Расшифровка голосового не завершилась. Повторный платный запрос не запущен.')
        if not service.store.still_current(job):return None,None
        if not isinstance(transcript,str) or not transcript.strip() or len(transcript)>16000:
            _save_media(service,job,row,dict(media,transcription_state='failed'))
            return _manual('Получена неполная или некорректная расшифровка голосового.')
        if sensitive(transcript):
            _save_media(service,job,row,dict(media,transcription_state='blocked',blocked_transcript=True))
            return _manual('В голосовом возможны данные доступа. Расшифровка не сохранена и не включена в ответ.')
        clean=scrub(transcript)
        media.update(transcript=clean,transcript_bytes=len(data),transcription_state='done')
        caption=row['text'] or ''
        text=(caption+'\n' if caption else '')+'[Расшифровка голосового; возможны ошибки]\n'+clean
        if not _save_media(service,job,row,media,text):return None,None
    return images,None
