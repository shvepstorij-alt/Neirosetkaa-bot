"""Prepare owner-review drafts from a dated, explicitly reviewed source snapshot.

No paid model requests or fallback from failed web search to model memory.
This command does not claim to verify the internet at invocation time.
"""
import logging
from catalog_descriptions import describe, REVIEW_VERSION, CHECKED_DATE
from config import SHOP_CATALOG, ADMIN_ID
from db import save_desc_drafts

logger = logging.getLogger(__name__)


def prepare_description_drafts(catalog):
    drafts=[];counts={};services=0
    for key,svc in catalog.items():
        if not isinstance(svc,dict):continue
        services+=1
        entries=[('',svc)] + [(str(p.get('name','')),p) for p in svc.get('plans',[]) if isinstance(p,dict)]
        for plan,target in entries:
            result=describe(key,svc.get('name',''),plan if plan else None)
            status=result['status'];counts[status]=counts.get(status,0)+1
            old=str(target.get('desc') or '').strip()
            if old!=result['text']:drafts.append((key,plan,old,result['text']))
    return drafts,counts,services


async def refresh_all_descriptions(notify=True):
    """Save review drafts, never publish or invent descriptions automatically."""
    drafts,counts,services=prepare_description_drafts(SHOP_CATALOG)
    summary=(f'Подготовленный снимок описаний: {CHECKED_DATE}. '
             f'Версия: {REVIEW_VERSION}.\n'
             'Свежая интернет-проверка этой командой не выполняется; платных API-запросов нет.\n'
             f'Сервисов: {services}; изменений: {len(drafts)}.\n'
             f'Требуют подтверждения состава: {counts.get("needs_confirmation",0)}. '
             'Нейтральные описания таких вариантов не означают, что их функции проверены.')
    if not drafts:
        return summary+'\nТексты совпадают со снимком. Это не подтверждение актуальности условий на сегодня.'
    try:
        await save_desc_drafts(drafts)
    except Exception as exc:
        logger.error('Description drafts persistence failed: stage=save; type=%s',type(exc).__name__)
        raise RuntimeError('Этап: сохранение черновиков описаний. Не удалось записать комплект; тип='+type(exc).__name__+'. Публикация не выполнялась.') from None
    if notify:
        try:
            from common import bot
            from handlers_desc import render_page
            text,keyboard=await render_page(0)
            await bot.send_message(ADMIN_ID,summary+'\n\n'+text,parse_mode='HTML',
                                   reply_markup=keyboard,disable_web_page_preview=True)
        except Exception as exc:
            logger.warning('Description preview unavailable: stage=owner_preview; type=%s',type(exc).__name__)
            return summary+'\nЧерновики сохранены, но превью не доставлено владельцу. Откройте раздел описаний повторно; публикация не выполнялась.'
    return summary+'\nЧерновики сохранены для просмотра; автоматически не опубликованы.'
