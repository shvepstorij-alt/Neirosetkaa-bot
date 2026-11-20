# -*- coding: utf-8 -*-
"""Запас готовых аккаунтов и автовыдача после оплаты (Александр 20.11.2026).

Первым сервисом идёт CapCut: Александр покупает аккаунты пачками по 5,
загружает их в админ-панели, а бот после успешной оплаты выдаёт клиенту
ровно ОДИН аккаунт. Модуль общий: подойдёт любому сервису магазина.

Правила:
  • аккаунт с пустым тарифом подходит к любому тарифу сервиса, с указанным —
    только к нему (сначала ищем точный тариф, потом «любой»);
  • один заказ = один аккаунт: повтор обработки оплаты вернёт тот же
    аккаунт, второй не выдаётся;
  • запаса нет → заказ уходит в обычную ручную выдачу, Александру тревога;
  • осталось ≤ STOCK_LOW_LEFT → предупреждение, что пора докупить.
"""
import html
import logging

STOCK_LOW_LEFT = 1

_DDL = """
CREATE TABLE IF NOT EXISTS stock_accounts (
    id          SERIAL PRIMARY KEY,
    service_key TEXT NOT NULL,
    plan_name   TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    issued_to   BIGINT,
    issued_at   TIMESTAMPTZ,
    order_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_stock_free ON stock_accounts(service_key, plan_name)
    WHERE issued_to IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_order ON stock_accounts(order_id)
    WHERE order_id IS NOT NULL;
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


def _esc(s) -> str:
    return html.escape(str(s or ""))


# ── выдача ──────────────────────────────────────────────────────────────
async def stock_enabled(service_key: str) -> bool:
    """Сервис работает через запас, если в него хоть раз загружали аккаунты."""
    p = await _pool()
    async with p.acquire() as c:
        return bool(await c.fetchval(
            "SELECT 1 FROM stock_accounts WHERE service_key=$1 LIMIT 1", service_key))


async def stock_free_count(service_key: str, plan_name: str = None) -> int:
    p = await _pool()
    async with p.acquire() as c:
        if plan_name is None:
            return int(await c.fetchval(
                "SELECT COUNT(*) FROM stock_accounts WHERE service_key=$1 AND issued_to IS NULL",
                service_key) or 0)
        return int(await c.fetchval(
            "SELECT COUNT(*) FROM stock_accounts WHERE service_key=$1 AND issued_to IS NULL "
            "AND plan_name IN ($2, '')", service_key, plan_name or "") or 0)


async def stock_take(service_key: str, plan_name: str, user_id: int, order_id: str):
    """Закрепляет один свободный аккаунт за заказом. Атомарно и идемпотентно."""
    p = await _pool()
    async with p.acquire() as c, c.transaction():
        row = await c.fetchrow("SELECT * FROM stock_accounts WHERE order_id=$1", order_id)
        if row:
            return dict(row)
        row = await c.fetchrow(
            """UPDATE stock_accounts SET issued_to=$3, issued_at=NOW(), order_id=$4
               WHERE id = (
                   SELECT id FROM stock_accounts
                   WHERE service_key=$1 AND issued_to IS NULL AND plan_name IN ($2, '')
                   ORDER BY (plan_name = $2) DESC, id
                   LIMIT 1 FOR UPDATE SKIP LOCKED)
               RETURNING *""",
            service_key, plan_name or "", user_id, order_id)
        return dict(row) if row else None


async def stock_issued_for_order(order_id: str):
    p = await _pool()
    async with p.acquire() as c:
        row = await c.fetchrow("SELECT * FROM stock_accounts WHERE order_id=$1", order_id)
    return dict(row) if row else None


async def stock_try_deliver(*, bot, admin_id, user_id, shop_key, service_name,
                            plan_name, order_id, amount_rub, order_ref="",
                            delayed_note="") -> bool:
    """Пытается выдать аккаунт из запаса. True — выдано (заказ закрыт).
    False — сервис не на запасе или запас пуст (тогда работает ручная выдача)."""
    try:
        if not await stock_enabled(shop_key):
            return False
        acc = await stock_take(shop_key, plan_name, user_id, order_id)
    except Exception as e:
        logging.error(f"stock_try_deliver {shop_key} {order_id}: {e}", exc_info=True)
        return False

    if not acc:
        try:
            await bot.send_message(
                admin_id,
                f"🚨 <b>Закончились аккаунты {_esc(service_name)}</b>\n\n"
                f"Заказ <code>{_esc(order_id)}</code> ({_esc(plan_name)}, {amount_rub}₽) "
                f"ушёл в ручную выдачу.\nЗагрузи аккаунты: админ-панель → "
                f"«Аккаунты на выдачу».",
                parse_mode="HTML")
        except Exception:
            pass
        return False

    _note = (acc.get("note") or "").strip()
    try:
        await bot.send_message(
            user_id,
            f"🎉 <b>Оплата прошла!</b>\n\n"
            f"📦 <b>{_esc(service_name)}</b> — {amount_rub}₽\n"
            + (f"{order_ref}\n" if order_ref else "")
            + f"\n🔐 <b>Ваш аккаунт:</b>\n<code>{_esc(acc['payload'])}</code>\n"
            + (f"\n💬 {_esc(_note)}\n" if _note else "")
            + "\n📌 Войдите в аккаунт с этими данными. Сохраните их — сообщение "
              "лучше не удалять. Не меняйте почту аккаунта.\n"
              "Если что-то не получилось — напишите Александру."
            + (delayed_note or ""),
            parse_mode="HTML")
    except Exception as e:
        # Аккаунт уже закреплён за заказом — повторная обработка выдаст тот же.
        logging.error(f"stock deliver msg uid={user_id}: {e}")
        try:
            await bot.send_message(
                admin_id,
                f"⚠️ Аккаунт #{acc['id']} закреплён за заказом <code>{_esc(order_id)}</code>, "
                f"но сообщение клиенту не ушло: {_esc(str(e)[:150])}. Отправь вручную:\n"
                f"<code>{_esc(acc['payload'])}</code>", parse_mode="HTML")
        except Exception:
            pass

    try:
        left = await stock_free_count(shop_key)
        if left <= STOCK_LOW_LEFT:
            await bot.send_message(
                admin_id,
                f"📉 <b>{_esc(service_name)}: осталось аккаунтов — {left}</b>\n"
                f"Пора докупить и загрузить в «Аккаунты на выдачу».",
                parse_mode="HTML")
    except Exception:
        pass
    return True


async def stock_admin_line(order_id: str) -> str:
    """Строка для карточки «Заказ оплачен» у Александра."""
    try:
        acc = await stock_issued_for_order(order_id)
    except Exception:
        return ""
    if not acc:
        return ""
    try:
        left = await stock_free_count(acc["service_key"])
    except Exception:
        left = "?"
    _login = (acc["payload"] or "").split()[0] if acc.get("payload") else ""
    return (f"\n\n📦 <b>Выдан аккаунт из запаса</b> #{acc['id']}: "
            f"<code>{_esc(_login)}</code>\nОсталось в запасе: <b>{left}</b>")


# ── админ-панель ────────────────────────────────────────────────────────
async def api_admin_stock_list_handler(request):
    from aiohttp import web
    from common import _admin_uid_from_body
    from config import ADMIN_ID, SHOP_CATALOG
    try:
        body = await request.json()
    except Exception:
        body = {}
    if _admin_uid_from_body(body) != ADMIN_ID:
        return web.json_response({"ok": False}, status=403)
    key = str(body.get("key") or "capcut")
    p = await _pool()
    async with p.acquire() as c:
        free = await c.fetch(
            "SELECT id, plan_name, payload, note, added_at FROM stock_accounts "
            "WHERE service_key=$1 AND issued_to IS NULL ORDER BY id", key)
        issued = await c.fetch(
            "SELECT id, plan_name, payload, issued_to, issued_at, order_id FROM stock_accounts "
            "WHERE service_key=$1 AND issued_to IS NOT NULL ORDER BY issued_at DESC LIMIT 30", key)
    svc = SHOP_CATALOG.get(key) or {}
    _f = lambda d: d.strftime("%d.%m %H:%M") if d else ""
    return web.json_response({
        "ok": True, "key": key, "name": svc.get("name", key),
        "plans": [pl.get("name", "") for pl in (svc.get("plans") or [])],
        "free": [{"id": r["id"], "plan": r["plan_name"], "payload": r["payload"],
                  "note": r["note"], "added": _f(r["added_at"])} for r in free],
        "issued": [{"id": r["id"], "plan": r["plan_name"], "payload": r["payload"],
                    "user": r["issued_to"], "when": _f(r["issued_at"]),
                    "order": r["order_id"]} for r in issued],
    })


async def api_admin_stock_add_handler(request):
    from aiohttp import web
    from common import _admin_uid_from_body
    from config import ADMIN_ID
    try:
        body = await request.json()
    except Exception:
        body = {}
    if _admin_uid_from_body(body) != ADMIN_ID:
        return web.json_response({"ok": False}, status=403)
    key = str(body.get("key") or "capcut")
    plan = str(body.get("plan") or "").strip()
    note = str(body.get("note") or "").strip()[:300]
    lines = [l.strip() for l in str(body.get("text") or "").splitlines() if l.strip()]
    if not lines:
        return web.json_response({"ok": False, "msg": "Вставь хотя бы один аккаунт"})
    p = await _pool()
    added = dup = 0
    async with p.acquire() as c:
        for l in lines[:200]:
            if await c.fetchval("SELECT 1 FROM stock_accounts WHERE service_key=$1 AND payload=$2",
                                key, l):
                dup += 1
                continue
            await c.execute(
                "INSERT INTO stock_accounts (service_key, plan_name, payload, note) "
                "VALUES ($1,$2,$3,$4)", key, plan, l, note)
            added += 1
    return web.json_response({"ok": True, "added": added, "dup": dup})


async def api_admin_stock_delete_handler(request):
    from aiohttp import web
    from common import _admin_uid_from_body
    from config import ADMIN_ID
    try:
        body = await request.json()
    except Exception:
        body = {}
    if _admin_uid_from_body(body) != ADMIN_ID:
        return web.json_response({"ok": False}, status=403)
    try:
        _id = int(body.get("id"))
    except Exception:
        return web.json_response({"ok": False, "msg": "Нет id"})
    p = await _pool()
    async with p.acquire() as c:
        r = await c.execute(
            "DELETE FROM stock_accounts WHERE id=$1 AND issued_to IS NULL", _id)
    return web.json_response({"ok": r.endswith(" 1"),
                              "msg": "" if r.endswith(" 1") else "Уже выдан или не найден"})
