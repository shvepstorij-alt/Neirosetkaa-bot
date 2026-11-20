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
SMS_MAX_ATTEMPTS = 5   # Александр 20.11.2026: до 5 номеров на один заказ
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
            # Сначала самые надёжные линии (процент получения кода), при
            # равенстве — дешевле. Покупка по умолчанию — самая надёжная.
            lines.sort(key=lambda x: (-(x["rate"] or 0), x["cost"]))
            b = lines[0]
            out.append({"country": country, "operator": b["operator"], "cost": b["cost"],
                        "min_cost": min(x["cost"] for x in lines),
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
ALTER TABLE sms_orders ADD COLUMN IF NOT EXISTS admin_msg_id BIGINT;
ALTER TABLE sms_orders ADD COLUMN IF NOT EXISTS log TEXT NOT NULL DEFAULT '';
ALTER TABLE sms_orders ADD COLUMN IF NOT EXISTS tried TEXT NOT NULL DEFAULT '';
ALTER TABLE sms_orders ADD COLUMN IF NOT EXISTS last_error TEXT NOT NULL DEFAULT '';
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
async def _budget_cost(o: dict) -> float:
    """Сколько можно заплатить поставщику за номер, чтобы не уйти в минус:
    цена клиента минус наценка, в долларах. Замена номера идёт в этих рамках."""
    try:
        return max(0.0, (int(o["price_rub"]) - await _markup()) / await _rate())
    except Exception:
        return float(o.get("cost_usd") or 0)


async def _candidates(o: dict) -> list:
    """Линии для покупки: выбранная, потом остальные линии этой страны по
    надёжности (в рамках оплаченной суммы). Уже пробованные — в конец."""
    tried = [t for t in (o.get("tried") or "").split(",") if t]
    budget = await _budget_cost(o) * 1.02
    ops = [o["operator"]] if o.get("operator") and o["operator"] != "any" else []
    try:
        off = await find_offer(o["product"], o["country"])
        for ln in (off or {}).get("lines") or []:
            if ln["cost"] <= budget and ln["operator"] not in ops:
                ops.append(ln["operator"])
    except Exception as e:
        logging.warning(f"sms candidates {o['order_id']}: {e}")
    fresh = [x for x in ops if x not in tried]
    return fresh + [x for x in ops if x in tried]


def _reason(st, d) -> str:
    t = (d if isinstance(d, str) else json.dumps(d, ensure_ascii=False))[:160]
    return f"HTTP {st}: {t}"


async def _buy(o: dict):
    """→ (ok, data|reason, errors). Пробует несколько линий страны."""
    if not await _token():
        return False, "no_token", ["не задан ключ API"]
    errors = []
    budget = await _budget_cost(o)
    for op in (await _candidates(o))[:6] + ["any"]:
        # maxPrice поставщик принимает только для operator=any.
        params = {"maxPrice": f"{budget:.4f}"} if op == "any" and budget > 0 else None
        try:
            st, d = await _get(f"/user/buy/activation/{o['country']}/{op}/{o['product']}",
                               auth=True, params=params)
        except Exception as e:
            errors.append(f"{op}: {str(e)[:80]}")
            continue
        if st == 200 and isinstance(d, dict) and d.get("id"):
            return True, d, errors
        errors.append(f"{op}: {_reason(st, d)}")
        logging.warning(f"sms buy {o['order_id']} op={op}: {st} {str(d)[:200]}")
        if isinstance(d, str) and "not enough user balance" in d.lower():
            return False, "balance", errors
    return False, "no_numbers", errors


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
        [InlineKeyboardButton(text="🚫 Номер занят / ошибка", callback_data=f"smsn:ban:{order_id}"),
         InlineKeyboardButton(text="⏹ Код не пришёл", callback_data=f"smsn:cnl:{order_id}")],
        [InlineKeyboardButton(text="🔁 Нужен номер для другого сервиса", callback_data=f"smsn:svc:{order_id}")],
    ])


async def _alt_countries(o: dict, n: int = 3) -> list:
    """Топ-N стран по % получения кода, которые укладываются в оплаченную сумму."""
    try:
        budget = await _budget_cost(o) * 1.02
        offs = [x for x in await offers(o["product"])
                if x["country"] != o["country"] and (x.get("rate") or 0) > 0]
        good = []
        for x in offs:
            ln = next((l for l in x.get("lines") or [] if l["cost"] <= budget), None)
            if ln:
                good.append({"country": x["country"], "rate": ln["rate"] or 0, "count": ln["count"]})
        good.sort(key=lambda x: -x["rate"])
        return good[:n]
    except Exception as e:
        logging.warning(f"sms alt countries: {e}")
        return []


async def _kb_offer(o: dict, alts: list):
    oid = o["order_id"]
    left = SMS_MAX_ATTEMPTS - int(o.get("attempts") or 0)
    rows = []
    if left > 0:
        rows.append([InlineKeyboardButton(text=f"🔄 Новый номер этой страны (осталось {left})",
                                          callback_data=f"smsn:new:{oid}")])
        for a in alts:
            ci = await country_info(a["country"])
            rows.append([InlineKeyboardButton(
                text=f"{_flag(ci['iso'])} {ci['en']} · ✅ {a['rate']:.0f}%",
                callback_data=f"smsn:cty:{oid}:{a['country']}"[:64])])
        rows.append([InlineKeyboardButton(text="🔁 Другой сервис", callback_data=f"smsn:svc:{oid}")])
    rows.append([InlineKeyboardButton(text="🪙 Вернуть деньги монетками", callback_data=f"smsn:ref:{oid}")])
    if left <= 0:
        try:
            from config import PERSONAL_USERNAME
            rows.append([InlineKeyboardButton(text="✍️ Написать Александру",
                                              url=f"https://t.me/{PERSONAL_USERNAME}")])
        except Exception:
            pass
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
    att = int(o.get("attempts") or 1)
    t = (await _head(o)
         + f"\n☎️ Номер: <code>{_esc(o['phone'])}</code>\n"
           f"⏳ Ждём SMS · осталось {_left(o)} · номер {att} из {SMS_MAX_ATTEMPTS}\n\n"
           "<b>Что делать:</b>\n"
           "1. Скопируйте номер (нажмите на него) и введите в сервисе.\n"
           "2. Запросите код — он придёт сюда сам, обычно за 1–3 минуты.\n"
           "3. Включите VPN той же страны, что и номер.\n\n"
           "<b>Если что-то не так:</b>\n"
           "• Сервис пишет, что номер недоступен, уже используется или выдаёт другую ошибку — «🚫 Номер занят / ошибка».\n"
           "• Ошиблись сервисом (нужен, например, ChatGPT, а не Telegram) — «🔁 Нужен номер для другого сервиса».\n"
           "• Код не пришёл за 3–5 минут — «⏹ Код не пришёл».\n"
           f"<i>Виртуальные номера иногда срабатывают не с первого раза — может понадобиться "
           f"до {SMS_MAX_ATTEMPTS} попыток. Замена номера бесплатная.</i>")
    await _show(o, t, _kb_active(o["order_id"]))


async def _render_services(o):
    """Смена сервиса: свои сервисы из каталога + популярные."""
    try:
        pr = await products()
    except Exception:
        pr = []
    ids = {p["id"] for p in pr}
    cat = [pid for pid in catalog_matches() if pid in ids]
    top = [k for k in ("telegram", "whatsapp", "openai", "claudeai", "google", "instagram",
                       "tiktok", "microsoft", "discord", "apple", "other") if k in ids]
    seen, lst = set(), []
    for k in cat + top:
        if k not in seen and k != o["product"]:
            seen.add(k); lst.append(k)
    lst = lst[:20]
    rows, row = [], []
    for k in lst:
        row.append(InlineKeyboardButton(text=product_name(k)[:28], callback_data=f"smsn:sp:{o['order_id']}:{k}"[:64]))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"smsn:back:{o['order_id']}")])
    t = (await _head(o) + "\n🔁 <b>Для какого сервиса нужен номер?</b>\n"
         "Смена бесплатная, если номер укладывается в оплаченную сумму. "
         "Нужного сервиса нет в списке — выберите «Любой другой» или напишите Александру.")
    await _show(o, t, InlineKeyboardMarkup(inline_keyboard=rows))


async def _render_offer(o, why: str):
    left = SMS_MAX_ATTEMPTS - int(o.get("attempts") or 0)
    alts = await _alt_countries(o) if left > 0 else []
    if left > 0:
        tip = (f"Можно бесплатно взять новый номер — осталось попыток: <b>{left}</b>.\n"
               + ("Если номера этой страны не подходят, попробуйте страну, где код "
                  "приходит чаще всего (✅ — % получения кода):\n" if alts else "")
               + f"\nИли вернём <b>{o['price_rub']}₽</b> монетками на баланс — ими можно оплатить "
                 "любой товар в боте.")
    else:
        tip = (f"Все {SMS_MAX_ATTEMPTS} попыток использованы. Вернём <b>{o['price_rub']}₽</b> "
               "монетками на баланс или напишите Александру — подберём номер вручную.")
    t = await _head(o) + f"\n⚠️ {why}\n\n" + tip
    await _show(o, t, await _kb_offer(o, alts))


async def _ref(order_id: str) -> str:
    """«🧾 Заказ #N · FreeKassa NNN» — номер FreeKassa появляется после оплаты."""
    try:
        from common import _order_ref_line
        return await _order_ref_line(order_id)
    except Exception:
        return f"🧾 Заказ {order_id}"


_ST_RU = {"pending": "⏳ ждёт оплату", "paid": "💳 оплачен, покупаю номер",
          "active": "📲 номер выдан, ждём SMS", "received": "✅ код получен",
          "done": "✅ завершён", "offer": "⚠️ ждёт выбор клиента",
          "refunded": "🪙 возврат монетками", "cancelled": "❌ отменён"}
_admin_locks: dict = {}


async def _admin(o: dict, title: str, extra: str = "") -> None:
    """ОДНА карточка заказа у владельца: при каждом событии дописываем строку
    в хронологию и редактируем то же сообщение (Александр 20.11.2026)."""
    import re as _re
    oid = o["order_id"]
    lock = _admin_locks.setdefault(oid, asyncio.Lock())
    async with lock:
        try:
            from common import _who_user
            from config import _BOT_TZ
            import datetime as _dt
            o = await _order(oid) or o
            now = _dt.datetime.now(_BOT_TZ).strftime("%H:%M")
            plain = _re.sub(r"<[^>]+>", "", extra or "").replace("\n", " · ")
            log = (o.get("log") or "") + f"{now} {title}" + (f" — {plain}" if plain else "") + "\n"
            log = "\n".join(log.strip().splitlines()[-25:]) + "\n"
            await _upd(oid, log=log)
            ci = await country_info(o["country"])
            fo = {}
            try:
                from db import fk_get_order
                fo = await fk_get_order(oid) or {}
            except Exception:
                pass
            coins = int(fo.get("coins_spent") or 0)
            paid_rub = int(fo.get("amount_rub") or 0)
            pm = {"card": "Карта"}.get(str(fo.get("payment_method") or ""), "СБП")
            txt = (
                f"📱 <b>Номер для SMS</b> · {_ST_RU.get(o['status'], o['status'])}\n\n"
                f"👤 {await _who_user(o['user_id'])}\n"
                f"📦 {_esc(product_name(o['product']))} · {_flag(ci['iso'])} {_esc(ci['en'])}"
                f" · линия {_esc(o.get('operator') or 'any')}\n"
                f"💵 <b>{o['price_rub']}₽</b> · закуп ${float(o.get('cost_usd') or 0):g}\n"
                + (f"🪙 Монетками: {coins}₽\n" if coins else "")
                + (f"💳 {pm}: {paid_rub}₽\n" if paid_rub else "")
                + f"{_esc(await _ref(oid))}\n"
                f"🆔 <code>{_esc(oid)}</code>\n"
                + (f"☎️ <code>{_esc(o['phone'])}</code>" if o.get("phone") else "")
                + (f" · код <code>{_esc(o['code'])}</code>" if o.get("code") else "")
                + (f"\n🔁 Номеров выдано: {o.get('attempts') or 0} из {SMS_MAX_ATTEMPTS}" if o.get("attempts") else "")
                + (f"\n⚠️ Последняя ошибка: <i>{_esc(o.get('last_error'))}</i>" if o.get("last_error") else "")
                + f"\n\n<b>Хронология</b>\n<blockquote>{_esc(log.strip())}</blockquote>"
            )
            mid = o.get("admin_msg_id")
            if mid:
                try:
                    await bot.edit_message_text(txt, chat_id=ADMIN_ID, message_id=mid, parse_mode="HTML")
                    return
                except Exception as e:
                    if "not modified" in str(e).lower():
                        return
            m = await bot.send_message(ADMIN_ID, txt, parse_mode="HTML")
            await _upd(oid, admin_msg_id=m.message_id)
        except Exception as e:
            logging.warning(f"sms admin card {oid}: {e}")


async def notify_paid(order_id: str, method: str = "", amount=None) -> None:
    """Вызывается при оплате (вебхук FreeKassa) — карточка «оплачен» с номером FK."""
    o = await _order(order_id)
    if not o:
        return
    coins = 0
    try:
        from db import fk_get_order
        fo = await fk_get_order(order_id) or {}
        coins = int(fo.get("coins_spent") or 0)
    except Exception:
        pass
    pay = []
    if coins:
        pay.append(f"🪙 Монетками: <b>{coins}₽</b>")
    if amount:
        pay.append(f"💳 {_esc(method or 'СБП')}: <b>{amount}₽</b>")
    await _admin(o, "Оплачен ✅", " · ".join(pay))


# ── выдача после оплаты ────────────────────────────────────────────────
async def fulfill(order_id: str, user_id: int = 0):
    o = await _order(order_id)
    if not o or o["status"] not in ("pending", "paid", "offer"):
        return
    if o["status"] == "pending":
        await _upd(order_id, status="paid")
        o["status"] = "paid"
    ok, d, errs = await _buy(o)
    if not ok:
        await _upd(order_id, status="offer", last_error="; ".join(errs)[-300:])
        o = await _order(order_id)
        if d in ("balance", "no_token"):
            # Клиенту — сразу возврат: замена номера тут не поможет.
            await _upd(order_id, attempts=SMS_MAX_ATTEMPTS)
            o = await _order(order_id)
            await _admin(o, "🚨 Не купился номер",
                         "закончился баланс у поставщика" if d == "balance" else "не задан ключ API")
            await _render_offer(o, "Сервис номеров временно недоступен.")
            return
        await _admin(o, "Нет номеров у этой страны", f"перепробовано линий: {len(errs)}")
        await _render_offer(o, "Сейчас не удалось взять номер этой страны — "
                               "попробуйте ещё раз или выберите другую страну.")
        return
    exp = None
    try:
        import datetime as _dt
        exp = _dt.datetime.fromisoformat(str(d.get("expires")).replace("Z", "+00:00"))
    except Exception:
        pass
    op = str(d.get("operator") or o["operator"] or "any")
    tried = ",".join(sorted(set([t for t in (o.get("tried") or "").split(",") if t] + [op])))
    await _upd(order_id, status="active", provider_id=int(d["id"]), phone=str(d.get("phone") or ""),
               expires_at=exp, attempts=int(o.get("attempts") or 0) + 1, operator=op, tried=tried,
               cost_usd=float(d.get("price") or o["cost_usd"] or 0), code=None, sms_text=None,
               last_error="")
    o = await _order(order_id)
    await _render_active(o)
    await _admin(o, f"Номер {o['attempts']} выдан", f"{o['phone']} · линия {op} · ${float(o['cost_usd']):g}")


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
            _first = not (o.get("code") or o.get("sms_text"))
            await _upd(o["order_id"], status="received", code=code, sms_text=text)
            o = await _order(o["order_id"])
            if _first:
                await _admin(o, "Код получен ✅", f"{o['phone']} · {code or '—'}")
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
        await _admin(o, "Время номера вышло", f"{o.get('phone') or ''} · SMS не пришла")


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
    new_c = ""
    if act in ("cty", "sp") and ":" in order_id:
        order_id, new_c = order_id.rsplit(":", 1)
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
            await _admin(o, "Клиент: «Номер занят» (ban)" if act == "ban"
                         else "Клиент: «Код не пришёл» (cancel)",
                         f"{o.get('phone') or ''} · у поставщика: {st or d.get('_err', '?')}")
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
            await _admin(o, "Клиент: новый номер той же страны")
            await fulfill(order_id)
            return

        if act == "svc":
            # Клиент ошибся сервисом. Активный номер сначала отменяем у поставщика.
            if o["status"] == "active":
                d = await _provider("cancel", o["provider_id"])
                st = str(d.get("status") or "").upper()
                if st not in ("CANCELED", "BANNED"):
                    await _check_one(o)
                    if (await _order(order_id))["status"] == "received":
                        await cb.answer("На этот номер уже пришёл код — сменить сервис нельзя", show_alert=True)
                        return
                await _upd(order_id, status="offer")
                o = await _order(order_id)
                await _admin(o, "Клиент: сменить сервис", f"номер {o.get('phone') or ''} отменён · {st or '?'}")
            if o["status"] != "offer":
                await cb.answer("Недоступно для этого заказа", show_alert=True)
                return
            if int(o.get("attempts") or 0) >= SMS_MAX_ATTEMPTS:
                await cb.answer("Попытки закончились — можно вернуть монетками", show_alert=True)
                return
            await _render_services(o)
            await cb.answer()
            return

        if act == "back":
            if o["status"] == "offer":
                await _render_offer(o, "Выберите, что сделать дальше.")
            await cb.answer()
            return

        if act == "sp":
            if not new_c or o["status"] != "offer" or int(o.get("attempts") or 0) >= SMS_MAX_ATTEMPTS:
                await cb.answer("Недоступно для этого заказа", show_alert=True)
                return
            budget = await _budget_cost(o) * 1.02
            offs = await offers(new_c)
            # Та же страна, если есть номер в рамках оплаты, иначе — самая надёжная подходящая.
            pick = None
            for x in sorted(offs, key=lambda x: (x["country"] != o["country"], -(x.get("rate") or 0))):
                ln = next((l for l in x.get("lines") or [] if l["cost"] <= budget), None)
                if ln:
                    pick = (x["country"], ln["operator"]); break
            if not pick:
                await cb.answer("Для этого сервиса нет номеров в рамках оплаты. Можно вернуть монетками "
                                "и купить заново.", show_alert=True)
                return
            await _upd(order_id, product=new_c, country=pick[0], operator=pick[1], tried="")
            o = await _order(order_id)
            await _admin(o, "Клиент сменил сервис", f"{product_name(new_c)} · {pick[0]}")
            await cb.answer(f"Сервис: {product_name(new_c)}")
            await _render_offer(o, f"Сервис изменён на <b>{_esc(product_name(new_c))}</b>. "
                                   "Возьмите номер — страну можно выбрать ниже.")
            return

        if act.startswith("cty"):
            # smsn:cty:{order}:{страна} — номер другой страны в рамках оплаты
            if not new_c or o["status"] != "offer" or int(o.get("attempts") or 0) >= SMS_MAX_ATTEMPTS:
                await cb.answer("Недоступно для этого заказа", show_alert=True)
                return
            off = await find_offer(o["product"], new_c)
            budget = await _budget_cost(o) * 1.02
            ln = next((l for l in (off or {}).get("lines") or [] if l["cost"] <= budget), None)
            if not ln:
                await cb.answer("Номера этой страны закончились — выберите другую", show_alert=True)
                return
            await _upd(order_id, country=new_c, operator=ln["operator"], tried="")
            await cb.answer("Беру номер другой страны…")
            o = await _order(order_id)
            await _admin(o, "Клиент сменил страну", new_c)
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
            await _admin(o, "Возврат монетками 🪙", f"{o['price_rub']}₽ на баланс")
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
                    "minPrice": price_rub(o.get("min_cost", o["cost"]), rate, mk),
                    "rate": o.get("rate"),
                    "lines": [{"op": ln["operator"], "price": price_rub(ln["cost"], rate, mk),
                               "count": ln["count"], "rate": ln["rate"]}
                              for ln in (o.get("lines") or [])]})
    # Страны: сначала с самым высоким процентом получения кода.
    out.sort(key=lambda x: (-(x["rate"] or 0), x["price"], -x["count"]))
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
    o_new = await _order(order_id)
    if o_new:
        await _admin(o_new, "Заказ создан",
                     f"монетками {coins_used}₽" if full_coins
                     else f"{'карта' if method == 'card' else 'СБП'} {rest}₽"
                          + (f" + монетки {coins_used}₽" if coins_used else "") + " · ждём оплату")
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
