# -*- coding: utf-8 -*-
"""Виртуальные номера для приёма SMS (Александр 20.11.2026).

Клиент в Магазине выбирает сервис и страну, оплачивает как обычно (СБП, карта,
монетки), а бот покупает номер у поставщика и присылает в чат номер, а потом
и код из SMS. Название поставщика клиенту НИГДЕ не показывается.

Цена клиенту = ⌈цена поставщика ($) × курс⌉ + наценка (по умолчанию +100 ₽).

Если код не пришёл или номер уже занят — клиент жмёт кнопку, номер отменяется
(«занят» → ban, иначе cancel), и бот предлагает:
  • 🔄 взять новый номер (без доплаты, до SMS_MAX_ATTEMPTS попыток на заказ);
  • 🪙 вернуть деньги монетками на баланс.
Если SMS так и не пришла до конца срока — то же самое предложение.
"""
import asyncio
import html
import json
import logging
import math
import time

import aiohttp
from aiogram import F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import ADMIN_ID, bot, dp

API = "https://5sim.net/v1"
SMS_MAX_ATTEMPTS = 3
POLL_SEC = 8

_cache: dict = {}


def _esc(s) -> str:
    return html.escape(str(s or ""))


# ── настройки ───────────────────────────────────────────────────────────
async def _setting(key, default=""):
    from db import get_setting
    return await get_setting(key, default)


async def _token() -> str:
    import os
    return (os.getenv("FIVESIM_TOKEN") or await _setting("sms_provider_token", "") or "").strip()


async def _rate() -> float:
    for k in ("sms_usd_rate", "cost_usd_rate"):
        try:
            v = float(await _setting(k, "") or 0)
            if v > 0:
                return v
        except Exception:
            pass
    return 95.0


async def _markup() -> int:
    try:
        return int(float(await _setting("sms_markup_rub", "100") or 100))
    except Exception:
        return 100


async def enabled() -> bool:
    return (await _setting("sms_enabled", "1")) != "0" and bool(await _token())


def price_rub(cost_usd: float, rate: float, markup: int) -> int:
    return int(math.ceil(float(cost_usd) * rate)) + int(markup)


# ── HTTP к поставщику ───────────────────────────────────────────────────
async def _get(path: str, auth: bool = False, params=None, timeout=20):
    h = {"Accept": "application/json"}
    if auth:
        h["Authorization"] = f"Bearer {await _token()}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.get(API + path, headers=h, params=params) as r:
            txt = await r.text()
            try:
                return r.status, json.loads(txt)
            except Exception:
                return r.status, txt.strip()


async def _cached(key, ttl, fn):
    """Кэш с запасом: если поставщик не ответил — отдаём прошлые данные,
    а пустой ответ не кэшируем (иначе «не удалось загрузить» висит долго)."""
    v = _cache.get(key)
    if v and time.time() - v[0] < ttl:
        return v[1]
    data = None
    for _try in range(2):
        try:
            data = await fn()
        except Exception as e:
            logging.warning(f"sms cache {key} попытка {_try + 1}: {e}")
            data = None
        if data:
            break
        await asyncio.sleep(1)
    if data:
        _cache[key] = (time.time(), data)
        return data
    return v[1] if v else data


async def countries() -> dict:
    async def _f():
        st, d = await _get("/guest/countries")
        return d if st == 200 and isinstance(d, dict) else {}
    return await _cached("countries", 6 * 3600, _f)


async def products() -> list:
    """Список сервисов, у которых сейчас есть номера (любая страна)."""
    async def _f():
        st, d = await _get("/guest/products/any/any", timeout=40)
        if st != 200 or not isinstance(d, dict):
            return []
        out = []
        for k, v in d.items():
            if (v or {}).get("Category") == "activation" and int((v or {}).get("Qty") or 0) > 0:
                out.append({"id": k, "qty": int(v.get("Qty") or 0)})
        out.sort(key=lambda x: -x["qty"])
        return out
    return await _cached("products", 1800, _f)


async def offers(product: str) -> list:
    """Страны для сервиса. По каждой стране — все линии (операторы) с ценой,
    количеством и процентом успешных активаций, как на сайте поставщика."""
    async def _f():
        st, d = await _get("/guest/prices", params={"product": product}, timeout=30)
        if st != 200 or not isinstance(d, dict):
            return []
        by = (d.get(product) or {})
        out = []
        for country, ops in by.items():
            lines = []
            for op, v in (ops or {}).items():
                cnt = int((v or {}).get("count") or 0)
                c = float((v or {}).get("cost") or 0)
                if cnt <= 0 or c <= 0:
                    continue
                r = v.get("rate")
                lines.append({"operator": op, "cost": c, "count": cnt,
                              "rate": float(r) if r is not None else None})
            if not lines:
                continue
            lines.sort(key=lambda x: (x["cost"], -(x["rate"] or 0)))
            b = lines[0]
            out.append({"country": country, "operator": b["operator"], "cost": b["cost"],
                        "count": sum(x["count"] for x in lines), "rate": b["rate"],
                        "lines": lines})
        return out
    return await _cached(f"offers:{product}", 120, _f)


async def find_offer(product: str, country: str, operator: str = ""):
    for o in await offers(product):
        if o["country"] == country:
            if operator:
                for ln in o.get("lines") or []:
                    if ln["operator"] == operator:
                        return {**o, "operator": ln["operator"], "cost": ln["cost"],
                                "count": ln["count"], "rate": ln["rate"]}
                return None
            return o
    return None


# ── база ────────────────────────────────────────────────────────────────
_DDL = """
CREATE TABLE IF NOT EXISTS sms_orders (
    order_id    TEXT PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    product     TEXT NOT NULL,
    country     TEXT NOT NULL,
    operator    TEXT NOT NULL DEFAULT 'any',
    cost_usd    NUMERIC NOT NULL DEFAULT 0,
    price_rub   INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    provider_id BIGINT,
    phone       TEXT,
    code        TEXT,
    sms_text    TEXT,
    expires_at  TIMESTAMPTZ,
    attempts    INTEGER NOT NULL DEFAULT 0,
    msg_id      BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sms_active ON sms_orders(status) WHERE status='active';
"""
_ready = False


async def _pool():
    from db import get_pool
    global _ready
    p = await get_pool()
    if not _ready:
        async with p.acquire() as c:
            await c.execute(_DDL)
        _ready = True
    return p


async def _order(order_id: str):
    p = await _pool()
    async with p.acquire() as c:
        r = await c.fetchrow("SELECT * FROM sms_orders WHERE order_id=$1", order_id)
    return dict(r) if r else None


async def _upd(order_id: str, **kw):
    if not kw:
        return
    cols = list(kw)
    sets = ", ".join(f"{k}=${i + 2}" for i, k in enumerate(cols))
    p = await _pool()
    async with p.acquire() as c:
        await c.execute(f"UPDATE sms_orders SET {sets}, updated_at=NOW() WHERE order_id=$1",
                        order_id, *[kw[k] for k in cols])


# ── названия для клиента ────────────────────────────────────────────────
_PRODUCT_NAMES = {
    "telegram": "Telegram", "whatsapp": "WhatsApp", "google": "Google / Gmail / YouTube",
    "openai": "OpenAI / ChatGPT", "instagram": "Instagram", "facebook": "Facebook",
    "vkontakte": "ВКонтакте", "tiktok": "TikTok", "twitter": "X (Twitter)",
    "microsoft": "Microsoft / Outlook", "apple": "Apple", "amazon": "Amazon",
    "discord": "Discord", "viber": "Viber", "wechat": "WeChat", "yandex": "Яндекс",
    "mailru": "Mail.ru", "avito": "Авито", "ozon": "Ozon", "wildberries": "Wildberries",
    "uber": "Uber", "netflix": "Netflix", "steam": "Steam", "tinder": "Tinder",
    "claude": "Claude", "claudeai": "Claude", "anthropic": "Claude",
    "gemini": "Gemini", "genspark": "Genspark", "manus": "Manus", "suno": "Suno",
    "heygen": "HeyGen", "kimi": "Kimi", "perplexity": "Perplexity", "cursor": "Cursor",
    "capcut": "CapCut", "canva": "Canva", "other": "Любой другой",
    "paypal": "PayPal", "linkedin": "LinkedIn", "snapchat": "Snapchat", "line": "LINE",
    "signal": "Signal", "spotify": "Spotify", "airbnb": "Airbnb", "aliexpress": "AliExpress",
}


# Сервис каталога → id сервиса у поставщика (если называются по-разному).
_CATALOG_ALIASES = {
    "chatgpt": ["openai"], "gpt": ["openai"], "openai": ["openai"],
    "claude": ["claudeai", "claude", "anthropic"], "gemini": ["google"], "google": ["google"],
    "youtube": ["google"], "grok": ["twitter"], "supergrok": ["twitter"],
    "x": ["twitter"], "midjourney": ["discord"], "capcut": ["capcut", "tiktok"],
    "microsoft": ["microsoft"], "copilot": ["microsoft"], "kimi": ["moonshot"],
}


def _norm(s: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def catalog_matches() -> dict:
    """{id поставщика: ключ сервиса каталога} для сервисов, которые есть у меня."""
    try:
        from config import SHOP_CATALOG
    except Exception:
        return {}
    out = {}
    for k, v in SHOP_CATALOG.items():
        for tok in {_norm(k), _norm((v or {}).get("name", ""))}:
            if not tok:
                continue
            for pid in _CATALOG_ALIASES.get(tok, []) + [tok]:
                out.setdefault(pid, k)
    return out


def product_name(pid: str) -> str:
    return _PRODUCT_NAMES.get(pid) or pid.replace("_", " ").capitalize()


async def country_info(cid: str) -> dict:
    c = (await countries()).get(cid) or {}
    iso = ""
    try:
        iso = list((c.get("iso") or {}).keys())[0].upper()
    except Exception:
        pass
    pref = ""
    try:
        pref = list((c.get("prefix") or {}).keys())[0]
    except Exception:
        pass
    return {"iso": iso, "prefix": pref, "en": c.get("text_en") or cid.capitalize()}


def _flag(iso: str) -> str:
    if len(iso or "") != 2:
        return "🌍"
    return "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in iso.upper())


# ── покупка у поставщика ───────────────────────────────────────────────
async def _buy(o: dict):
    """→ (ok, data|reason)."""
    if not await _token():
        return False, "no_token"
    maxp = float(o["cost_usd"] or 0) * 1.3 if o.get("cost_usd") else None
    params = {"maxPrice": f"{maxp:.4f}"} if maxp else None
    for op in (o["operator"], "any"):
        try:
            st, d = await _get(f"/user/buy/activation/{o['country']}/{op}/{o['product']}",
                               auth=True, params=params)
        except Exception as e:
            logging.warning(f"sms buy {o['order_id']}: {e}")
            continue
        if st == 200 and isinstance(d, dict) and d.get("id"):
            return True, d
        logging.warning(f"sms buy {o['order_id']} op={op}: {st} {str(d)[:200]}")
        if isinstance(d, str) and "not enough user balance" in d.lower():
            return False, "balance"
    return False, "no_numbers"


async def _provider(action: str, pid) -> dict:
    try:
        st, d = await _get(f"/user/{action}/{int(pid)}", auth=True)
        return d if isinstance(d, dict) else {"_err": str(d)[:200], "_st": st}
    except Exception as e:
        return {"_err": str(e)[:200]}


# ── сообщения клиенту ───────────────────────────────────────────────────
def _kb_active(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Проверить SMS", callback_data=f"smsn:chk:{order_id}")],
        [InlineKeyboardButton(text="🚫 Номер уже занят", callback_data=f"smsn:ban:{order_id}"),
         InlineKeyboardButton(text="⏹ Код не пришёл", callback_data=f"smsn:cnl:{order_id}")],
    ])


def _kb_offer(order_id, can_new: bool):
    rows = []
    if can_new:
        rows.append([InlineKeyboardButton(text="🔄 Взять новый номер", callback_data=f"smsn:new:{order_id}")])
    rows.append([InlineKeyboardButton(text="🪙 Вернуть деньги монетками", callback_data=f"smsn:ref:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _head(o) -> str:
    ci = await country_info(o["country"])
    return (f"📱 <b>Виртуальный номер</b>\n"
            f"Сервис: <b>{_esc(product_name(o['product']))}</b>\n"
            f"Страна: {_flag(ci['iso'])} {_esc(ci['en'])}\n")


async def _show(o, text, kb):
    """Одно живое сообщение на заказ: редактируем, а если нельзя — шлём новое."""
    if o.get("msg_id"):
        try:
            await bot.edit_message_text(text, chat_id=o["user_id"], message_id=o["msg_id"],
                                        parse_mode="HTML", reply_markup=kb)
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
    m = await bot.send_message(o["user_id"], text, parse_mode="HTML", reply_markup=kb)
    await _upd(o["order_id"], msg_id=m.message_id)


def _left(o) -> str:
    try:
        sec = int(o["expires_at"].timestamp() - time.time())
        return f"{max(0, sec) // 60} мин" if sec > 0 else "истёк"
    except Exception:
        return "—"


async def _render_active(o):
    t = (await _head(o)
         + f"\n☎️ Номер: <code>{_esc(o['phone'])}</code>\n"
           f"⏳ Ждём SMS · осталось {_left(o)}\n\n"
           "Введите номер в сервисе и запросите код — он придёт сюда автоматически.\n"
           "<i>Совет: включите VPN той же страны, что и номер.</i>")
    await _show(o, t, _kb_active(o["order_id"]))


async def _render_offer(o, why: str):
    can_new = int(o.get("attempts") or 0) < SMS_MAX_ATTEMPTS
    t = (await _head(o)
         + f"\n⚠️ {why}\n\n"
         + ("Можем бесплатно выдать новый номер или вернуть "
            if can_new else "Попытки замены закончились. Можем вернуть ")
         + f"<b>{o['price_rub']}₽</b> монетками на баланс — ими можно оплатить любой товар в боте.")
    await _show(o, t, _kb_offer(o["order_id"], can_new))


# ── выдача после оплаты ────────────────────────────────────────────────
async def fulfill(order_id: str, user_id: int = 0):
    o = await _order(order_id)
    if not o or o["status"] not in ("pending", "paid", "offer"):
        return
    if o["status"] == "pending":
        await _upd(order_id, status="paid")
        o["status"] = "paid"
    ok, d = await _buy(o)
    if not ok:
        await _upd(order_id, status="offer")
        o = await _order(order_id)
        if d in ("balance", "no_token"):
            try:
                await bot.send_message(
                    ADMIN_ID,
                    "🚨 <b>Номера: не удалось купить</b> — "
                    + ("закончился баланс у поставщика" if d == "balance" else "не задан ключ API")
                    + f"\n🆔 <code>{_esc(order_id)}</code> · {_esc(product_name(o['product']))} · "
                      f"{_esc(o['country'])} · {o['price_rub']}₽", parse_mode="HTML")
            except Exception:
                pass
            # Клиенту — возврат сразу, «новый номер» тут не поможет.
            o["attempts"] = SMS_MAX_ATTEMPTS
            await _upd(order_id, attempts=SMS_MAX_ATTEMPTS)
        await _render_offer(o, "Сейчас нет свободных номеров для этой страны.")
        return
    exp = None
    try:
        import datetime as _dt
        exp = _dt.datetime.fromisoformat(str(d.get("expires")).replace("Z", "+00:00"))
    except Exception:
        pass
    await _upd(order_id, status="active", provider_id=int(d["id"]), phone=str(d.get("phone") or ""),
               expires_at=exp, attempts=int(o.get("attempts") or 0) + 1,
               cost_usd=float(d.get("price") or o["cost_usd"] or 0), code=None, sms_text=None)
    o = await _order(order_id)
    await _render_active(o)


# ── слежение за SMS ─────────────────────────────────────────────────────
async def _check_one(o) -> None:
    d = await _provider("check", o["provider_id"])
    if not d or d.get("_err"):
        return
    st = str(d.get("status") or "").upper()
    sms = d.get("sms") or []
    if sms:
        last = sms[-1]
        code = str(last.get("code") or "").strip()
        text = str(last.get("text") or "").strip()
        if code != (o.get("code") or "") or text != (o.get("sms_text") or ""):
            await _upd(o["order_id"], status="received", code=code, sms_text=text)
            o = await _order(o["order_id"])
            t = (await _head(o)
                 + f"\n☎️ Номер: <code>{_esc(o['phone'])}</code>\n\n"
                   f"✅ <b>Код: <code>{_esc(code or '—')}</code></b>\n"
                 + (f"<i>{_esc(text[:500])}</i>\n" if text else "")
                 + "\nЕсли сервис пришлёт ещё одно SMS — оно тоже появится здесь.")
            await _show(o, t, InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Проверить ещё SMS", callback_data=f"smsn:chk:{o['order_id']}")]]))
        return
    if st in ("TIMEOUT", "CANCELED", "BANNED"):
        await _upd(o["order_id"], status="offer")
        o = await _order(o["order_id"])
        await _render_offer(o, "SMS не пришла за отведённое время, номер отменён.")


async def poll_loop():
    """Фоновая проверка активных номеров."""
    await asyncio.sleep(20)
    try:
        await countries()
        await products()
    except Exception as e:
        logging.warning(f"sms warmup: {e}")
    while True:
        try:
            p = await _pool()
            async with p.acquire() as c:
                rows = await c.fetch(
                    "SELECT * FROM sms_orders WHERE status IN ('active','received') "
                    "AND provider_id IS NOT NULL AND updated_at > NOW() - INTERVAL '40 minutes'")
            for r in rows:
                o = dict(r)
                try:
                    if o["status"] == "received":
                        # Код уже есть: после конца срока закрываем заказ у поставщика.
                        if o["expires_at"] and o["expires_at"].timestamp() < time.time():
                            await _provider("finish", o["provider_id"])
                            await _upd(o["order_id"], status="done")
                        else:
                            await _check_one(o)
                    else:
                        await _check_one(o)
                except Exception as e:
                    logging.warning(f"sms poll {o['order_id']}: {e}")
        except Exception as e:
            logging.error(f"sms poll_loop: {e}")
        await asyncio.sleep(POLL_SEC)


# ── кнопки в чате ───────────────────────────────────────────────────────
_locks: dict = {}


@dp.callback_query(F.data.startswith("smsn:"))
async def cb_sms(cb: CallbackQuery):
    try:
        _, act, order_id = cb.data.split(":", 2)
    except ValueError:
        await cb.answer()
        return
    o = await _order(order_id)
    if not o or int(o["user_id"]) != cb.from_user.id:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    lock = _locks.setdefault(order_id, asyncio.Lock())
    if lock.locked():
        await cb.answer("Секунду, обрабатываю…")
        return
    async with lock:
        o["msg_id"] = o.get("msg_id") or cb.message.message_id
        if act == "chk":
            if o["status"] in ("active", "received") and o.get("provider_id"):
                await _check_one(o)
                o2 = await _order(order_id)
                if o2["status"] == "active":
                    await _render_active(o2)
                    await cb.answer("SMS пока нет — ждём")
                    return
            await cb.answer()
            return

        if act in ("ban", "cnl"):
            if o["status"] != "active":
                await cb.answer("Этот номер уже закрыт", show_alert=True)
                return
            d = await _provider("ban" if act == "ban" else "cancel", o["provider_id"])
            st = str(d.get("status") or "").upper()
            if st not in ("BANNED", "CANCELED"):
                # Уже пришла SMS или номер истёк — проверим и покажем как есть.
                await _check_one(o)
                o2 = await _order(order_id)
                if o2["status"] == "received":
                    await cb.answer("Код уже пришёл — смотрите сообщение", show_alert=True)
                    return
            await _upd(order_id, status="offer")
            o = await _order(order_id)
            await _render_offer(o, "Номер занят — отменили его." if act == "ban"
                                else "Код не пришёл — номер отменён.")
            await cb.answer()
            return

        if act == "new":
            if o["status"] != "offer":
                await cb.answer("Недоступно для этого заказа", show_alert=True)
                return
            if int(o.get("attempts") or 0) >= SMS_MAX_ATTEMPTS:
                await cb.answer("Попытки закончились — можно вернуть монетками", show_alert=True)
                return
            await cb.answer("Ищу новый номер…")
            await fulfill(order_id)
            return

        if act == "ref":
            if o["status"] != "offer":
                await cb.answer("Недоступно для этого заказа", show_alert=True)
                return
            from db import add_coins
            p = await _pool()
            async with p.acquire() as c, c.transaction():
                r = await c.fetchrow(
                    "UPDATE sms_orders SET status='refunded', updated_at=NOW() "
                    "WHERE order_id=$1 AND status='offer' RETURNING price_rub", order_id)
                if not r:
                    await cb.answer("Уже вернули", show_alert=True)
                    return
                await add_coins(int(o["user_id"]), int(r["price_rub"]),
                                reason=f"возврат за номер {order_id}", conn=c)
            o = await _order(order_id)
            await _show(o, await _head(o) + f"\n🪙 Вернули <b>{o['price_rub']}₽</b> монетками на баланс.", None)
            await cb.answer("Монетки зачислены")
            try:
                from common import _who_user
                await bot.send_message(ADMIN_ID,
                    f"🪙 <b>Номер: возврат монетками</b> {o['price_rub']}₽\n"
                    f"👤 {await _who_user(o['user_id'])}\n🆔 <code>{_esc(order_id)}</code>",
                    parse_mode="HTML")
            except Exception:
                pass
            return
    await cb.answer()


# ── API мини-приложения магазина ────────────────────────────────────────
def _json(d, status=200):
    from aiohttp import web
    return web.json_response(d, status=status)


async def _uid(request):
    from config import _verify_tg_init_data
    try:
        body = await request.json()
    except Exception:
        body = {}
    return body, _verify_tg_init_data((body.get("initData") if isinstance(body, dict) else "") or "")


async def api_sms_products_handler(request):
    body, uid = await _uid(request)
    if not uid:
        return _json({"ok": False, "error": "auth"}, 403)
    if not await enabled():
        return _json({"ok": False, "msg": "Сервис номеров временно недоступен."})
    try:
        pr = await products()
    except Exception as e:
        logging.error(f"sms products: {e}")
        return _json({"ok": False, "msg": "Не удалось загрузить список. Попробуйте позже."})
    if not pr:
        return _json({"ok": False, "msg": "Не удалось загрузить сервисы. Попробуйте через минуту."})
    ids = {p["id"] for p in pr}
    cat = {pid: ck for pid, ck in catalog_matches().items() if pid in ids}
    mine = [p["id"] for p in pr if p["id"] in cat]            # есть в моём каталоге
    top = [k for k in ("telegram", "whatsapp", "openai", "google", "instagram", "tiktok",
                       "microsoft", "claudeai", "discord", "apple") if k in ids and k not in cat]
    rest = [p["id"] for p in pr if p["id"] not in cat and p["id"] not in top]
    return _json({"ok": True, "items": [
        {"id": k, "name": product_name(k), "cat": cat.get(k, ""), "top": k in top}
        for k in mine + top + rest]})


async def api_sms_offers_handler(request):
    body, uid = await _uid(request)
    if not uid:
        return _json({"ok": False, "error": "auth"}, 403)
    product = str(body.get("product") or "").strip().lower()[:60]
    if not product:
        return _json({"ok": False}, 400)
    try:
        offs = await offers(product)
    except Exception as e:
        logging.error(f"sms offers {product}: {e}")
        return _json({"ok": False, "msg": "Не удалось загрузить страны."})
    rate, mk = await _rate(), await _markup()
    out = []
    for o in offs:
        ci = await country_info(o["country"])
        out.append({"country": o["country"], "iso": ci["iso"], "flag": _flag(ci["iso"]),
                    "en": ci["en"], "price": price_rub(o["cost"], rate, mk), "count": o["count"],
                    "rate": o.get("rate"),
                    "lines": [{"op": ln["operator"], "price": price_rub(ln["cost"], rate, mk),
                               "count": ln["count"], "rate": ln["rate"]}
                              for ln in (o.get("lines") or [])]})
    out.sort(key=lambda x: x["price"])
    return _json({"ok": True, "name": product_name(product), "items": out})


async def api_sms_pay_handler(request):
    """Как App Store: цена только на сервере, монетки списываются в транзакции."""
    import common as C
    body, uid = await _uid(request)
    if not uid:
        return _json({"ok": False, "error": "auth"}, 403)
    _den, _den_msg = await C._web_access_denied(uid)
    if _den:
        return _json({"ok": False, "error": _den, "msg": _den_msg}, 403 if _den == "blocked" else 503)
    if not await enabled():
        return _json({"ok": False, "error": "unavailable"})
    product = str(body.get("product") or "").strip().lower()[:60]
    country = str(body.get("country") or "").strip().lower()[:60]
    operator = str(body.get("operator") or "").strip().lower()[:60]
    method = str(body.get("method", "sbp")).strip().lower()
    use_coins = bool(body.get("useCoins"))
    off = await find_offer(product, country, operator)
    if not off:
        return _json({"ok": False, "error": "not_found", "msg": "Номеров этой страны сейчас нет."})
    price = price_rub(off["cost"], await _rate(), await _markup())
    try:
        exp = int(body.get("expPrice")) if body.get("expPrice") is not None else None
    except Exception:
        exp = None
    if exp is not None and exp != price:
        return _json({"ok": False, "error": "stale", "price": price})
    coins_used = 0
    if use_coins:
        try:
            bal = int(await C.get_coins(int(uid)))
        except Exception:
            bal = 0
        if bal >= 1:
            coins_used = int(min(bal, price))
    rest = max(0, price - coins_used)
    full_coins = coins_used > 0 and rest == 0
    from config import _rand_sfx
    order_id = f"sms_{uid}_{int(time.time())}{_rand_sfx()}"
    p = await _pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            if coins_used > 0:
                if not await C.deduct_coins(int(uid), coins_used,
                                            reason=f"оплата заказа {order_id}", conn=conn):
                    return _json({"ok": False, "error": "coins", "msg": "Недостаточно монеток"})
            await conn.execute(
                "INSERT INTO sms_orders (order_id,user_id,product,country,operator,cost_usd,price_rub,status) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                order_id, int(uid), product, country, off["operator"], off["cost"], price,
                "paid" if full_coins else "pending")
            if full_coins:
                await conn.execute(
                    "INSERT INTO fk_orders (order_id,user_id,credits,amount_rub,pack,coins_spent,status,paid_at) "
                    "VALUES ($1,$2,0,0,$3,$4,'paid',NOW())",
                    order_id, int(uid), f"sms:{product}:{country}", coins_used)
            else:
                await conn.execute(
                    "INSERT INTO fk_orders (order_id,user_id,credits,amount_rub,pack,coins_spent) "
                    "VALUES ($1,$2,0,$3,$4,$5)",
                    order_id, int(uid), rest, f"sms:{product}:{country}", coins_used)
    url = None
    if not full_coins:
        if method == "card":
            try:
                url = await C.fk_create_order(float(rest), order_id, int(uid), payment_id=36)
            except Exception as ce:
                logging.error(f"sms pay card {order_id}: {ce}")
                back = await C._web_pay_rollback(order_id, int(uid), coins_used)
                await _upd(order_id, status="cancelled")
                return _json({"ok": False, "error": "card_failed", "coinsBack": back})
        else:
            from config import fk_pay_url
            url = fk_pay_url(rest, order_id)
    try:
        ci = await country_info(country)
        await bot.send_message(
            ADMIN_ID,
            f"📱 <b>Заказ номера</b> (магазин)\n"
            f"👤 {await C._who_user(uid)}\n"
            f"📦 {_esc(product_name(product))} · {_flag(ci['iso'])} {_esc(ci['en'])}\n"
            f"💵 <b>{price}₽</b> · закуп ${off['cost']}\n"
            + (f"🪙 Монетками: <b>{coins_used}₽</b>\n" if coins_used else "")
            + (f"💳 {'Карта' if method == 'card' else 'СБП'}: <b>{rest}₽</b>\n" if rest else "")
            + f"🆔 <code>{order_id}</code>\n"
            + ("✅ Оплачен монетками — покупаю номер" if full_coins else "⏳ Ожидает оплаты"),
            parse_mode="HTML")
    except Exception:
        pass
    if full_coins:
        asyncio.create_task(fulfill(order_id, int(uid)))
    return _json({"ok": True, "url": url, "orderId": order_id, "coinsUsed": coins_used,
                  "rest": rest, "price": price, "paidByCoins": full_coins})


# ── админ-панель ────────────────────────────────────────────────────────
async def api_admin_sms_handler(request):
    from config import ADMIN_ID as _A
    from db import set_setting
    body, uid = await _uid(request)
    if uid != _A:
        return _json({"ok": False}, 403)
    if body.get("save"):
        if "token" in body and str(body.get("token") or "").strip():
            await set_setting("sms_provider_token", str(body["token"]).strip())
        for k, sk in (("markup", "sms_markup_rub"), ("rate", "sms_usd_rate")):
            if str(body.get(k) or "").strip():
                try:
                    await set_setting(sk, str(float(str(body[k]).replace(",", "."))))
                except Exception:
                    pass
        if "enabled" in body:
            await set_setting("sms_enabled", "1" if body.get("enabled") else "0")
    bal, err = None, ""
    if await _token():
        try:
            st, d = await _get("/user/profile", auth=True)
            if st == 200 and isinstance(d, dict):
                bal = d.get("balance")
            else:
                err = f"Ключ не подошёл (HTTP {st})"
        except Exception as e:
            err = f"Нет связи: {str(e)[:80]}"
    p = await _pool()
    async with p.acquire() as c:
        rows = await c.fetch("SELECT * FROM sms_orders ORDER BY created_at DESC LIMIT 30")
    _st = {"pending": "ждёт оплату", "paid": "оплачен", "active": "ждёт SMS", "received": "код получен",
           "done": "завершён", "offer": "ждёт выбор клиента", "refunded": "возврат монетками",
           "cancelled": "отменён"}
    return _json({
        "ok": True, "hasToken": bool(await _token()), "balance": bal, "error": err,
        "enabled": (await _setting("sms_enabled", "1")) != "0",
        "markup": await _markup(), "rate": await _rate(),
        "orders": [{"id": r["order_id"], "user": r["user_id"], "product": product_name(r["product"]),
                    "country": r["country"], "price": r["price_rub"], "phone": r["phone"] or "",
                    "code": r["code"] or "", "status": _st.get(r["status"], r["status"]),
                    "when": r["created_at"].strftime("%d.%m %H:%M")} for r in rows],
    })


def register_routes(app):
    app.router.add_post("/api/sms/products", api_sms_products_handler)
    app.router.add_post("/api/sms/offers", api_sms_offers_handler)
    app.router.add_post("/api/sms/pay", api_sms_pay_handler)
    app.router.add_post("/api/admin/sms", api_admin_sms_handler)
