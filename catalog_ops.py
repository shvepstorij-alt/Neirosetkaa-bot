# -*- coding: utf-8 -*-
"""/catalog_update — разовые правки каталога (Александр 21.11.2026).

1) Переименования и удаления тарифов по актуальным официальным названиям.
2) Годовые тарифы: цена = годовая цена сервиса в $ × 100 − 10 ₽
   (например, $192 в год → 19 190 ₽).

Повторный запуск безопасен: уже сделанное пропускается.
"""
import logging
import re

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.types import Message

from config import ADMIN_ID, SHOP_CATALOG, dp

# (сервис, старое название тарифа, новое название)
RENAMES = [
    ("freepik", "Premium +", "Plus"),
    ("freepik", "Бизнес", "Pro"),
    ("gamma", "Pro на год (х2)", "Pro на год"),
    ("manus", "Standard", "Pro"),          # 22.11.2026: у Manus сейчас Free / Pro / Team
    ("higgsfield", "Pro", "Ultra"),        # 22.11.2026: самый дорогой тариф — Ultra
]
# (сервис, тариф) — удалить
DELETES = [
    ("freepik", "Pro starter"),
]
# (сервис, базовый тариф, годовая цена в $ — официальная, за 12 месяцев)
ANNUAL = [
    ('perplexity', 'Pro', 204),
    ('perplexity', 'Max', 2000),
    ('gemini', 'Pro', 199.99),
    ('lovable', 'Pro', 250),
    ('midjourney', 'Basic', 96),
    ('midjourney', 'Standard', 288),
    ('midjourney', 'Pro', 576),
    ('canva', 'Pro', 119.99),
    ('runway', 'Standard', 144),
    ('runway', 'Pro', 336),
    ('claude', 'Pro', 200),
    ('heygen', 'Creator', 288),
    ('heygen', 'Pro', 488),
    ('elevenlabs', 'Starter', 60),
    ('elevenlabs', 'Creator', 220),
    ('suno', 'Pro', 96),
    ('suno', 'Premier', 288),
    ('gamma', 'Plus', 108),
    ('krea', 'Basic', 60),
    ('krea', 'Pro', 252),
    ('freepik', 'Plus', 405),
    ('genspark', 'Plus', 239.99),
    ('higgsfield', 'Starter', 180),
    ('kimi', 'Pro', 372),
    ('make', 'Core', 108),
    ('zai', 'Lite', 151.2),
    ('zai', 'Pro', 672),
    ('zai', 'Max', 1411.2),
    ('zoom', 'Pro', 169.92),
    ('zoom', 'Business', 219.96),
    ('manus', 'Pro', 204),
]

USD_RUB = 100
MINUS = 10

_ALIASES = {
    "freepik": ["freepik", "magnific"],
    "grok": ["grok", "supergrok"],
    "gemini": ["gemini", "googleai"],
    "github": ["github", "githubcopilot", "copilot"],
    "zai": ["zai"],
    "telegram": ["telegrampremium", "telegram"],
    "youtube": ["youtube", "youtubepremium"],
}


def _n(s) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", str(s or "").lower())


def find_service(name: str):
    want = set(_ALIASES.get(name, [name]))
    for k, v in SHOP_CATALOG.items():
        if _n(k) in want or _n((v or {}).get("name", "")) in want:
            return k
    for k, v in SHOP_CATALOG.items():
        if any(_n(k).startswith(w) or _n((v or {}).get("name", "")).startswith(w) for w in want):
            return k
    return None


def annual_price(usd: float) -> int:
    return int(round(float(usd) * USD_RUB)) - MINUS


@dp.message(F.text.startswith("/catalog_update"), StateFilter("*"))
async def catalog_update(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    from db import get_pool
    from common import shift_plan_settings
    pool = await get_pool()
    log = []

    for svc, old, new in RENAMES:
        k = find_service(svc)
        plans = (SHOP_CATALOG.get(k) or {}).get("plans", []) if k else []
        p = next((x for x in plans if x.get("name") == old), None)
        if not p:
            continue
        if any(x.get("name") == new for x in plans):
            log.append(f"⏭ {svc}: «{new}» уже есть, «{old}» не переименован")
            continue
        async with pool.acquire() as c:
            await c.execute("UPDATE bot_shop_items SET plan_name=$1 WHERE key=$2 AND plan_idx=$3",
                            new, k, p.get("_db_plan_idx"))
        p["name"] = new
        log.append(f"✏️ {SHOP_CATALOG[k].get('name')}: «{old}» → «{new}»")

    for svc, name in DELETES:
        k = find_service(svc)
        plans = (SHOP_CATALOG.get(k) or {}).get("plans", []) if k else []
        idx = next((i for i, x in enumerate(plans) if x.get("name") == name), None)
        if idx is None:
            continue
        async with pool.acquire() as c, c.transaction():
            await c.execute("DELETE FROM bot_shop_items WHERE key=$1 AND plan_idx=$2",
                            k, plans[idx].get("_db_plan_idx"))
            await shift_plan_settings(c, k, idx)
        plans.pop(idx)
        log.append(f"🗑 {SHOP_CATALOG[k].get('name')}: удалён «{name}»")

    for svc, base, usd in ANNUAL:
        k = find_service(svc)
        if not k:
            log.append(f"❓ {svc}: сервис не найден в каталоге")
            continue
        scat = SHOP_CATALOG[k]
        plans = scat.setdefault("plans", [])
        name = f"{base} на год"
        if any(_n(x.get("name")) == _n(name) for x in plans):
            continue
        price = annual_price(usd)
        try:
            from catalog_descriptions import describe
            desc = describe(k, scat.get("name", ""), name)["text"]
        except Exception:
            desc = ""
        async with pool.acquire() as c, c.transaction():
            new_idx = int(await c.fetchval(
                "SELECT COALESCE(MAX(plan_idx),-1) FROM bot_shop_items WHERE key=$1", k)) + 1
            new_sort = int(await c.fetchval(
                "SELECT COALESCE(MAX(sort_order),-1) FROM bot_shop_items WHERE key=$1", k)) + 1
            await c.execute(
                "INSERT INTO bot_shop_items (key, plan_idx, service_name, emoji, service_desc, "
                "plan_name, price, stars, plan_desc, sort_order) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                k, new_idx, scat.get("name", k), scat.get("emoji", ""), scat.get("desc", ""),
                name, price, 0, desc, new_sort)
            await c.execute(
                "INSERT INTO settings(key,value) VALUES($1,$2) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                f"admin_plan_desc:{k}:{new_idx}", desc)
        plans.append({"name": name, "price": price, "stars": 0, "desc": desc, "_db_plan_idx": new_idx})
        log.append(f"➕ {scat.get('name')}: «{name}» — {price}₽ (${usd:g}/год)")

    text = "🛠 <b>Обновление каталога</b>\n\n" + ("\n".join(log) if log else "Нечего менять — всё уже сделано.")
    for i in range(0, len(text), 3900):
        await message.answer(text[i:i + 3900], parse_mode="HTML")
    logging.info(f"catalog_update: {len(log)} изменений")
