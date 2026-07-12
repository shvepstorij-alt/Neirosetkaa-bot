"""
models_refresh.py — авто-обновление описаний тарифов актуальными моделями.

Раз в неделю (или по команде /refresh_desc) через Claude + web_search находит
текущие модели/функции каждого сервиса и переписывает описания сервиса и тарифов.
Цены (в рублях) НЕ трогаются — переписывается только текст описания.
Результат сохраняется в таблицу shop_desc_overrides (переживает рестарт) и
применяется к SHOP_CATALOG в памяти. Админу приходит сводка изменений.
"""
import asyncio
import json
import logging
import re

from config import SHOP_CATALOG, ADMIN_ID
from db import save_desc_drafts

logger = logging.getLogger(__name__)

# Модели Claude для переписывания (по убыванию предпочтения)
_MODELS = ["claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"]


def _refreshable_services():
    """Сервисы с тарифами, которые можно авто-обновлять (без App Store/NS Gifts)."""
    out = []
    for k, s in SHOP_CATALOG.items():
        if s.get("_nsgifts"):
            continue
        if not s.get("plans"):
            continue
        out.append((k, s))
    return out


async def _rewrite_one(key: str, svc: dict):
    """Возвращает dict {'service_desc': str, 'plans': {имя: desc}} или None."""
    from common import claude_client

    _plans_txt = "\n".join(
        f"- {p.get('name','')}: {p.get('desc','')}" for p in svc.get("plans", [])
    )
    _sys = (
        "Ты обновляешь описания подписок для русскоязычного бота-реселлера ИИ-подписок. "
        "Через web_search проверь, какие МОДЕЛИ и ключевые функции актуальны на СЕГОДНЯ "
        "для указанного сервиса (флагманы, новые версии). Не выдумывай модели и цифры — "
        "опирайся только на найденное. Верни СТРОГО JSON без пояснений и без markdown."
    )
    _usr = (
        f"Сервис: {svc.get('name', key)}.\n"
        f"Текущее описание сервиса: {svc.get('desc','')}\n"
        f"Тарифы:\n{_plans_txt}\n\n"
        "Задача: перепиши описание сервиса и КАЖДОГО тарифа на русском, СОХРАНИВ стиль, "
        "примерную длину, структуру и месячную цену в долларах (формат «— $X/мес»), но "
        "ОБНОВИВ названия/версии моделей и функции на актуальные сегодня (сверься через "
        "web_search). Рубли и внутренние цены НЕ упоминай. "
        'Верни JSON строго вида: '
        '{"service_desc":"...","plans":{"<точное имя тарифа>":"<описание>", ...}}. '
        "Ключи в plans — точные имена тарифов из списка выше."
    )

    def _call(_model):
        return claude_client.messages.create(
            model=_model,
            max_tokens=1600,
            system=_sys,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": _usr}],
        )

    resp = None
    for _m in _MODELS:
        try:
            resp = await asyncio.to_thread(_call, _m)
            break
        except Exception as e:
            logger.warning(f"models_refresh {key} [{_m}]: {type(e).__name__}: {str(e)[:200]}")
            continue
    if resp is None:
        return None

    _txt = ""
    for b in resp.content:
        if getattr(b, "type", None) == "text":
            _txt += getattr(b, "text", "")
    m = re.search(r"\{.*\}", _txt, re.S)
    if not m:
        logger.warning(f"models_refresh {key}: JSON не найден в ответе")
        return None
    try:
        data = json.loads(m.group(0))
    except Exception as e:
        logger.warning(f"models_refresh {key}: битый JSON: {e}")
        return None
    return data if isinstance(data, dict) else None


async def refresh_all_descriptions(notify: bool = True) -> str:
    """Генерирует ЧЕРНОВИКИ обновлённых описаний (web_search), сохраняет их и
    присылает админу превью с кнопками (Применить / Редактировать / Отклонить).
    НЕ публикует автоматически. Возвращает сводку (для команды /refresh_desc)."""
    from common import bot

    drafts = []   # (key, plan_name, old, new)
    for key, svc in _refreshable_services():
        data = await _rewrite_one(key, svc)
        if not data:
            continue
        _svc_name = svc.get("name", key)

        _sd = (data.get("service_desc") or "").strip()
        _old_sd = (svc.get("desc") or "").strip()
        if _sd and len(_sd) > 10 and _sd != _old_sd:
            drafts.append((key, "", _old_sd, _sd))

        _plans = data.get("plans") or {}
        if isinstance(_plans, dict):
            for p in svc.get("plans", []):
                _nm = p.get("name", "")
                _nd = (_plans.get(_nm) or "").strip()
                _old = (p.get("desc") or "").strip()
                if _nd and len(_nd) > 10 and _nd != _old:
                    drafts.append((key, _nm, _old, _nd))

        await asyncio.sleep(1.0)  # мягкий rate-limit между сервисами

    if not drafts:
        summary = ("♻️ <b>Обновление описаний тарифов</b>\n\n"
                   "Изменений нет — описания актуальны ✅")
        return summary

    await save_desc_drafts(drafts)

    # первое сообщение — постраничная сводка с навигацией (рендер из handlers_desc)
    try:
        from handlers_desc import render_page
        _text, _kb = await render_page(0)
        await bot.send_message(ADMIN_ID, _text, parse_mode="HTML",
                               reply_markup=_kb, disable_web_page_preview=True)
    except Exception as _e:
        logger.error(f"models_refresh preview: {_e}")
        try:
            await bot.send_message(
                ADMIN_ID, f"📝 Черновик описаний готов: {len(drafts)} изменений. Открой /refresh_desc.")
        except Exception:
            pass
    logger.info(f"models_refresh: черновиков {len(drafts)}")
    return f"📝 Черновик готов: {len(drafts)} изменений. Отправил превью с навигацией."
