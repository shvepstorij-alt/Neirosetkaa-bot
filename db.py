# -*- coding: utf-8 -*-
# Auto-split module "db" — part of Neirosetkaa-bot (refactored from bot.py).
import asyncio, logging, os, re, uuid, base64, hashlib, hmac, json, time
import datetime
import datetime as _dt_tz
import time as _time_module
import asyncpg
import aiohttp
from aiohttp import web
import anthropic
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, ChatMemberUpdated, InlineKeyboardMarkup,
    InlineKeyboardButton, CallbackQuery,
    LabeledPrice, PreCheckoutQuery, BufferedInputFile,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION, StateFilter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import (
    ANIM_MODELS, CREDIT_PACKS, CUSTOM_EMOJI_IDS, DATABASE_URL, DISABLED_MODELS, EDIT_MODELS,
    FREE_CREDITS, IMAGE_MODELS, REF_BONUS, SHOP_CATALOG, VIDEO_MODELS, _pool,
)

async def get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL не задан! Добавь переменную в Railway.")
        # Railway PostgreSQL требует SSL
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=20,
            ssl="require",
            statement_cache_size=0,  # совместимость с pgbouncer
        )
        logging.info("✅ PostgreSQL pool создан")
    return _pool

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id        BIGINT PRIMARY KEY,
                credits        INTEGER DEFAULT 0,
                is_blocked     INTEGER DEFAULT 0,
                username       TEXT DEFAULT '',
                full_name      TEXT DEFAULT '',
                last_active    TIMESTAMP DEFAULT NOW(),
                created_at     TIMESTAMP DEFAULT NOW(),
                referred_by    BIGINT DEFAULT NULL,
                ref_bonus_paid BOOLEAN DEFAULT FALSE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS generations (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT,
                type       TEXT,
                model      TEXT,
                credits    INTEGER,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT,
                credits    INTEGER,
                amount_rub INTEGER,
                method     TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS fk_orders (
                order_id   TEXT PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                credits    INTEGER NOT NULL,
                amount_rub INTEGER NOT NULL,
                pack       TEXT,
                status     TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Добавляем новые колонки к существующим таблицам (идемпотентно)
        for col, dfn in [
            ("payment_method", "TEXT"),     # 'sbp' | 'card'
            ("promo_code",     "TEXT"),     # применённый промокод
            ("paid_at",        "TIMESTAMP"), # когда пришёл webhook об оплате
            ("admin_msg_id",   "BIGINT"),   # ID сообщения админу для редактирования
        ]:
            try:
                await conn.execute(f"ALTER TABLE fk_orders ADD COLUMN {col} {dfn}")
            except Exception:
                pass
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments_fk (
                id         SERIAL PRIMARY KEY,
                order_id   TEXT UNIQUE,
                user_id    BIGINT,
                credits    INTEGER,
                amount_rub INTEGER,
                pack_key   TEXT,
                status     TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        for col, dfn in [
            ("referred_by",    "BIGINT DEFAULT NULL"),
            ("ref_bonus_paid", "BOOLEAN DEFAULT FALSE"),
            ("coins",          "NUMERIC(10,2) DEFAULT 0"),
        ]:
            try:
                await conn.execute(f"ALTER TABLE users ADD COLUMN {col} {dfn}")
            except Exception:
                pass
        # Таблица событий - для аудита критичных операций
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT,
                kind       TEXT NOT NULL,
                data       TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Избранное
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                file_id    TEXT NOT NULL,
                media_type TEXT DEFAULT 'photo',
                prompt     TEXT,
                model      TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Промокоды
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS promocodes (
                code         TEXT PRIMARY KEY,
                kind         TEXT NOT NULL,            -- 'percent' или 'credits'
                value        INTEGER NOT NULL,         -- % скидки (1-99) или кол-во кредитов
                max_uses     INTEGER DEFAULT 1,        -- макс. использований (0 = безлимит)
                used_count   INTEGER DEFAULT 0,
                expires_at   TIMESTAMP,                -- NULL = без срока
                active       BOOLEAN DEFAULT TRUE,
                created_at   TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_uses (
                id          SERIAL PRIMARY KEY,
                code        TEXT NOT NULL,
                user_id     BIGINT NOT NULL,
                used_at     TIMESTAMP DEFAULT NOW(),
                UNIQUE (code, user_id)
            )
        """)
        # Партии кредитов с истечением (новая модель - каждая покупка = отдельная партия)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS credit_batches (
                id            SERIAL PRIMARY KEY,
                user_id       BIGINT NOT NULL,
                credits_init  INTEGER NOT NULL,
                credits_left  INTEGER NOT NULL,
                source        TEXT,                    -- 'purchase', 'free', 'referral', 'promo', 'admin'
                expires_at    TIMESTAMP,               -- NULL = не сгорает
                created_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_batches_user ON credit_batches(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_batches_exp ON credit_batches(expires_at)")
        # Напоминания - чтобы не слать дважды
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders_sent (
                user_id    BIGINT NOT NULL,
                kind       TEXT NOT NULL,
                sent_at    TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (user_id, kind)
            )
        """)
        # Активные генерации - для защиты от двойного запуска.
        # Переживает рестарт бота (в отличие от set'а в памяти).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS active_generations (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                kind       TEXT NOT NULL,           -- 'photo'/'video'/'anim'/'motion'
                started_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_gens_created ON generations(created_at)")
        # Дефолтные настройки
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('maintenance', '0') ON CONFLICT DO NOTHING"
        )
        # Миграция: active_generations - если старая таблица с user_id PRIMARY KEY, пересоздаём
        try:
            has_id = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name='active_generations' AND column_name='id')"
            )
            if not has_id:
                await conn.execute("DROP TABLE IF EXISTS active_generations")
                await conn.execute("""
                    CREATE TABLE active_generations (
                        id         SERIAL PRIMARY KEY,
                        user_id    BIGINT NOT NULL,
                        kind       TEXT NOT NULL,
                        started_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                logging.info("✅ Migrated active_generations table (added id, removed PK on user_id)")
        except Exception as mig_err:
            logging.warning(f"active_generations migration: {mig_err}")

        # Подписки пользователей
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT NOT NULL,
                service_key TEXT NOT NULL,
                service_name TEXT NOT NULL,
                plan_name   TEXT DEFAULT '',
                started_at  TIMESTAMP DEFAULT NOW(),
                expires_at  TIMESTAMP NOT NULL,
                notified_3d BOOLEAN DEFAULT FALSE,
                notified_1d BOOLEAN DEFAULT FALSE,
                is_active   BOOLEAN DEFAULT TRUE,
                notes       TEXT DEFAULT '',
                created_by  BIGINT
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_subs_uid ON user_subscriptions(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_subs_expires ON user_subscriptions(expires_at) WHERE is_active=TRUE")
        # Таблицы для редактирования цен через админку
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_credit_packs (
                key         TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                credits     INTEGER NOT NULL,
                price       INTEGER NOT NULL,
                stars       INTEGER DEFAULT 0,
                description TEXT DEFAULT '',
                badge       TEXT DEFAULT '',
                enabled     BOOLEAN DEFAULT TRUE,
                sort_order  INTEGER DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_shop_items (
                key         TEXT NOT NULL,
                plan_idx    INTEGER NOT NULL,
                service_name TEXT NOT NULL,
                emoji       TEXT DEFAULT '',
                service_desc TEXT DEFAULT '',
                plan_name   TEXT NOT NULL,
                price       INTEGER NOT NULL,
                stars       INTEGER DEFAULT 0,
                plan_desc   TEXT DEFAULT '',
                enabled     BOOLEAN DEFAULT TRUE,
                sort_order  INTEGER DEFAULT 0,
                PRIMARY KEY (key, plan_idx)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_gen_prices (
                model_key   TEXT PRIMARY KEY,
                section     TEXT NOT NULL,
                credits     INTEGER NOT NULL,
                enabled     BOOLEAN DEFAULT TRUE
            )
        """)

        # ── GPT коды и pending активации
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gpt_codes (
                id          SERIAL PRIMARY KEY,
                code        TEXT NOT NULL UNIQUE,
                plan        TEXT NOT NULL DEFAULT 'plus',
                is_used     BOOLEAN NOT NULL DEFAULT FALSE,
                used_by     BIGINT,
                used_at     TIMESTAMPTZ,
                order_id    TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gpt_codes_free ON gpt_codes(plan, is_used) WHERE is_used = FALSE"
        )
        # Миграция: добавить email, reserved_at, check_status, last_checked_at, flagged_reason
        for _col, _def in [
            ("email",            "TEXT"),
            ("reserved_at",      "TIMESTAMPTZ"),
            ("check_status",     "TEXT DEFAULT 'unchecked'"),  # 'unchecked'|'ok'|'used'|'invalid'|'error'
            ("last_checked_at",  "TIMESTAMPTZ"),
            ("flagged_reason",   "TEXT"),
        ]:
            try:
                await conn.execute(f"ALTER TABLE gpt_codes ADD COLUMN {_col} {_def}")
            except Exception:
                pass
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gpt_pending_activations (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT NOT NULL UNIQUE,
                code        TEXT NOT NULL,
                order_id    TEXT NOT NULL,
                plan        TEXT NOT NULL DEFAULT 'plus',
                plan_name   TEXT NOT NULL DEFAULT 'Plus',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at  TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '2 hours')
            )
        """)

        # ── Claude коды и pending активации ─────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS claude_codes (
                id          SERIAL PRIMARY KEY,
                code        TEXT NOT NULL UNIQUE,
                plan        TEXT NOT NULL DEFAULT 'pro',
                is_used     BOOLEAN NOT NULL DEFAULT FALSE,
                used_by     BIGINT,
                used_at     TIMESTAMPTZ,
                order_id    TEXT,
                org_id      TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claude_codes_free "
            "ON claude_codes(plan, is_used) WHERE is_used = FALSE"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS claude_pending_activations (
                id           SERIAL PRIMARY KEY,
                user_id      BIGINT NOT NULL UNIQUE,
                code         TEXT NOT NULL,
                order_id     TEXT NOT NULL,
                plan         TEXT NOT NULL DEFAULT 'pro',
                plan_name    TEXT NOT NULL DEFAULT 'Pro',
                org_id       TEXT DEFAULT '',
                bpa_order_id INTEGER,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at   TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '2 hours')
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nsgifts_orders (
                id            SERIAL PRIMARY KEY,
                user_id       BIGINT NOT NULL,
                fk_order_id   TEXT NOT NULL UNIQUE,
                ns_custom_id  TEXT,
                service_id    INTEGER NOT NULL,
                service_name  TEXT NOT NULL DEFAULT \'\',
                quantity      INTEGER DEFAULT 1,
                price_usd     NUMERIC(10,4),
                price_rub     INTEGER,
                status        TEXT DEFAULT \'pending\',
                pins_json     TEXT,
                error_msg     TEXT,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_nsgifts_uid ON nsgifts_orders(user_id)"
        )
        for _k, _v in [
            ("nsgifts_usd_rate",          "100"),
            ("nsgifts_markup",            "15"),
            ("nsgifts_balance_threshold", "30"),
        ]:
            await conn.execute(
                "INSERT INTO settings(key, value) VALUES($1,$2) ON CONFLICT DO NOTHING",
                _k, _v
            )
    logging.info("✅ PostgreSQL инициализирован")


# ── GPT АКТИВАЦИЯ — вспомогательные функции ─────────────────────────────────

async def get_next_gpt_code(plan: str = "plus"):
    """Выдаёт следующий свободный код. Приоритет: check_status='ok' > 'unchecked'.
    Коды со статусом 'used'/'invalid' не выдаются."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Сначала пробуем 'ok' (проверенные речекером)
        row = await conn.fetchrow(
            """UPDATE gpt_codes SET is_used=TRUE, reserved_at=NOW()
               WHERE id=(SELECT id FROM gpt_codes
                         WHERE plan=$1 AND is_used=FALSE
                           AND COALESCE(check_status,'unchecked') = 'ok'
                         ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
               RETURNING code""", plan)
        if not row:
            # Fallback: любые непроверенные (не помеченные как плохие)
            row = await conn.fetchrow(
                """UPDATE gpt_codes SET is_used=TRUE, reserved_at=NOW()
                   WHERE id=(SELECT id FROM gpt_codes
                             WHERE plan=$1 AND is_used=FALSE
                               AND COALESCE(check_status,'unchecked') NOT IN ('used','invalid')
                             ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
                   RETURNING code""", plan)
    return row["code"] if row else None

async def release_gpt_code(code: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE gpt_codes SET is_used=FALSE, used_by=NULL, used_at=NULL, order_id=NULL WHERE code=$1", code)

def _extract_email_from_token(token: str) -> str:
    """Извлекает email из JWT accessToken без верификации подписи."""
    try:
        import base64, json as _json
        payload_b64 = token.split(".")[1]
        # base64url padding
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
        # OpenAI кладёт email в https://api.openai.com/profile
        profile = payload.get("https://api.openai.com/profile", {})
        return profile.get("email", "") or payload.get("email", "")
    except Exception:
        return ""


async def mark_gpt_code_used(code: str, user_id: int, order_id: str, email: str = ""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE gpt_codes SET used_by=$1, order_id=$2, email=$3, used_at=NOW() WHERE code=$4",
            user_id, order_id, email, code)

async def save_pending_activation(user_id: int, code: str, order_id: str, plan: str, plan_name: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO gpt_pending_activations (user_id, code, order_id, plan, plan_name)
               VALUES ($1,$2,$3,$4,$5)
               ON CONFLICT (user_id) DO UPDATE
               SET code=$2, order_id=$3, plan=$4, plan_name=$5,
                   created_at=NOW(), expires_at=NOW()+INTERVAL '2 hours'""",
            user_id, code, order_id, plan, plan_name)

async def get_pending_activation(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM gpt_pending_activations WHERE user_id=$1 AND expires_at>NOW()", user_id)
    return dict(row) if row else None

async def delete_pending_activation(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM gpt_pending_activations WHERE user_id=$1", user_id)


# ── МОНЕТКИ ────────────────────────────────────────────────────────────────────
async def get_gen_count(user_id: int) -> int:
    """Возвращает общее количество генераций пользователя."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE user_id=$1 AND kind LIKE 'gen_%'",
            user_id
        )
        return int(val or 0)


# ── ДИНАМИЧЕСКИЕ ЦЕНЫ ─────────────────────────────────────────────────────────

async def load_prices_from_db():
    """Загружает цены из БД и обновляет глобальные словари. 
    Если БД пуста - записывает дефолтные значения из кода."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Кредитные пакеты
        rows = await conn.fetch("SELECT * FROM bot_credit_packs WHERE enabled=TRUE ORDER BY sort_order, price")
        if rows:
            CREDIT_PACKS.clear()
            for i, r in enumerate(rows):
                CREDIT_PACKS[r["key"]] = {
                    "name": r["name"], "credits": r["credits"],
                    "price": r["price"], "stars": r["stars"],
                    "desc": r["description"], "badge": r["badge"],
                }
        else:
            # Записываем дефолтные в БД
            for i, (key, p) in enumerate(CREDIT_PACKS.items()):
                await conn.execute("""
                    INSERT INTO bot_credit_packs (key, name, credits, price, stars, description, badge, sort_order)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (key) DO NOTHING
                """, key, p["name"], p["credits"], p["price"], p.get("stars", 0),
                    p.get("desc", ""), p.get("badge", ""), i)

        # Товары магазина
        # Сохраняем дефолтные описания из кода (имена, emoji, тексты описаний)
        # Цены и включённость берём из БД — чтобы сохранялись правки через админку
        _code_catalog = {k: v for k, v in SHOP_CATALOG.items()}

        rows_shop = await conn.fetch("SELECT * FROM bot_shop_items ORDER BY key, sort_order, plan_idx")
        if rows_shop:
            SHOP_CATALOG.clear()
            for r in rows_shop:
                if not r["enabled"]:
                    continue
                k = r["key"]
                # Описание сервиса берём из кода (если есть), иначе из БД
                code_svc = _code_catalog.get(k, {})
                if k not in SHOP_CATALOG:
                    SHOP_CATALOG[k] = {
                        "_key":  k,
                        "name":  code_svc.get("name",  r["service_name"]),
                        "emoji": code_svc.get("emoji", r["emoji"]),
                        "emoji_id": code_svc.get("emoji_id", "") or CUSTOM_EMOJI_IDS.get(k, ""),
                        "desc":  code_svc.get("desc",  r["service_desc"]),
                        "plans": []
                    }
                plan_idx = r["plan_idx"]
                if plan_idx < 0:
                    continue  # placeholder-строка без тарифа
                # Описание плана берём из кода по индексу (если есть), иначе из БД
                code_plans = code_svc.get("plans", [])
                code_plan = code_plans[plan_idx] if plan_idx < len(code_plans) else {}
                SHOP_CATALOG[k]["plans"].append({
                    "name":  code_plan.get("name",  r["plan_name"]),
                    "price": r["price"],   # цена — из БД (сохраняет правки через /admin)
                    "stars": r["stars"],
                    "desc":  code_plan.get("desc",  r["plan_desc"]),
                })

            # Синхронизируем описания из кода обратно в БД (чтобы не устаревали)
            for key, svc in _code_catalog.items():
                await conn.execute(
                    "UPDATE bot_shop_items SET service_name=$1, emoji=$2, service_desc=$3 WHERE key=$4",
                    svc["name"], svc.get("emoji", ""), svc.get("desc", ""), key
                )
                for i, plan in enumerate(svc.get("plans", [])):
                    await conn.execute(
                        "UPDATE bot_shop_items SET plan_name=$1, plan_desc=$2 WHERE key=$3 AND plan_idx=$4",
                        plan["name"], plan.get("desc", ""), key, i
                    )
        else:
            # БД пуста — записываем всё из кода
            for key, s in SHOP_CATALOG.items():
                for i, p in enumerate(s.get("plans", [])):
                    await conn.execute("""
                        INSERT INTO bot_shop_items
                        (key, plan_idx, service_name, emoji, service_desc, plan_name, price, stars, plan_desc, sort_order)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (key, plan_idx) DO NOTHING
                    """, key, i, s["name"], s.get("emoji",""), s.get("desc",""),
                        p["name"], p["price"], p.get("stars",0), p.get("desc",""), i)

        # Цены на генерации + список отключённых моделей
        rows_gen = await conn.fetch("SELECT * FROM bot_gen_prices")
        if rows_gen:
            DISABLED_MODELS.clear()
            for r in rows_gen:
                key = r["model_key"]
                credits = r["credits"]
                enabled = r["enabled"]
                if not enabled:
                    DISABLED_MODELS.add(key)
                    continue
                if key in IMAGE_MODELS:
                    IMAGE_MODELS[key]["credits"] = credits
                elif key in VIDEO_MODELS:
                    VIDEO_MODELS[key]["credits"] = credits
                elif key in ANIM_MODELS:
                    ANIM_MODELS[key]["credits"] = credits
                elif key in EDIT_MODELS:
                    EDIT_MODELS[key]["credits"] = credits
        else:
            # Записываем дефолтные
            all_models = list(IMAGE_MODELS.items()) + list(VIDEO_MODELS.items()) + list(ANIM_MODELS.items()) + list(EDIT_MODELS.items())
            for key, m in all_models:
                section = "image" if key in IMAGE_MODELS else "video" if key in VIDEO_MODELS else "anim" if key in ANIM_MODELS else "edit"
                await conn.execute("""
                    INSERT INTO bot_gen_prices (model_key, section, credits)
                    VALUES ($1,$2,$3) ON CONFLICT (model_key) DO NOTHING
                """, key, section, m.get("credits", 10))

    logging.info("✅ Цены загружены из БД")


async def get_coins(user_id: int) -> float:
    pool = await get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT COALESCE(coins, 0) FROM users WHERE user_id=$1", user_id
        )
        return float(val or 0)

async def add_coins(user_id: int, amount: float, reason: str = ""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET coins = COALESCE(coins, 0) + $1 WHERE user_id=$2",
            round(amount, 2), user_id
        )
    logging.info(f"add_coins uid={user_id} +{amount:.2f} reason={reason}")

async def deduct_coins(user_id: int, amount: float) -> bool:
    # SECURITY: 0 или отрицательное списание недопустимо (иначе обход оплаты монетками)
    if amount is None or amount <= 0:
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET coins = coins - $1 WHERE user_id=$2 AND COALESCE(coins,0) >= $1",
            round(amount, 2), user_id
        )
        return int(result.split()[-1]) > 0


async def log_event(user_id: int | None, kind: str, data: str = ""):
    """Логирует критичное событие в БД. Ошибки не пробрасывает."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO events (user_id, kind, data) VALUES ($1, $2, $3)",
                user_id, kind, data[:2000] if data else None
            )
    except Exception as e:
        logging.error(f"log_event failed: {e}")


# ─── Промокоды ─────────────────────────────────────────────

async def create_promo(code: str, kind: str, value: int, max_uses: int = 1, days_valid: int = 0) -> tuple[bool, str]:
    """Создаёт промокод. kind: 'percent' или 'credits'. days_valid=0 - бессрочный."""
    code = code.strip().upper()
    if not code or not code.replace("_", "").replace("-", "").isalnum():
        return False, "Код должен содержать только буквы, цифры, _ и -"
    if kind not in ("percent", "credits"):
        return False, "kind должен быть 'percent' или 'credits'"
    if kind == "percent" and not (1 <= value <= 99):
        return False, "Процент должен быть от 1 до 99"
    if kind == "credits" and value < 1:
        return False, "Кредиты должны быть больше 0"

    expires_sql = "NOW() + ($5 || ' days')::INTERVAL" if days_valid > 0 else "NULL"
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if days_valid > 0:
                await conn.execute(
                    f"INSERT INTO promocodes (code, kind, value, max_uses, expires_at) "
                    f"VALUES ($1, $2, $3, $4, NOW() + ($5 || ' days')::INTERVAL)",
                    code, kind, value, max_uses, str(days_valid)
                )
            else:
                await conn.execute(
                    "INSERT INTO promocodes (code, kind, value, max_uses) VALUES ($1, $2, $3, $4)",
                    code, kind, value, max_uses
                )
        return True, f"Промокод {code} создан"
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            return False, "Такой код уже существует"
        return False, f"Ошибка: {e}"


async def get_promo(code: str) -> dict | None:
    code = code.strip().upper()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM promocodes WHERE code=$1 AND active=TRUE", code
        )
    return dict(row) if row else None


async def check_promo_for_user(code: str, user_id: int) -> tuple[bool, str, dict | None]:
    """Проверяет, может ли юзер применить промокод. Возвращает (ok, msg, promo_dict)."""
    p = await get_promo(code)
    if not p:
        return False, "Промокод не найден или деактивирован", None
    if p.get("expires_at"):
        import datetime as _dt
        if p["expires_at"] < _dt.datetime.now():
            return False, "Срок действия промокода истёк", None
    if p["max_uses"] and p["used_count"] >= p["max_uses"]:
        return False, "Промокод уже использован максимальное число раз", None
    # Проверка что юзер не применял
    pool = await get_pool()
    async with pool.acquire() as conn:
        used = await conn.fetchval(
            "SELECT 1 FROM promo_uses WHERE code=$1 AND user_id=$2", code.strip().upper(), user_id
        )
    if used:
        return False, "Ты уже применял этот промокод", None
    return True, "OK", p


async def redeem_promo(code: str, user_id: int) -> tuple[bool, str]:
    """Применяет промокод с типом 'credits' - начисляет кредиты.
    Для 'percent' применение происходит в оплате пакета.

    Защищена от race condition: если два запроса пройдут одновременно,
    UNIQUE (code, user_id) в promo_uses сработает для одного из них,
    и второй получит ошибку вместо двойного начисления.
    """
    code_upper = code.strip().upper()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1. Получаем промокод с блокировкой - никто другой не сможет его
            #    использовать параллельно для того же user_id (и не сможет
            #    исчерпать max_uses между нашими операциями)
            p = await conn.fetchrow(
                "SELECT * FROM promocodes WHERE code=$1 AND active=TRUE FOR UPDATE",
                code_upper
            )
            if not p:
                return False, "Промокод не найден или деактивирован"

            # 2. Проверка срока действия
            if p["expires_at"]:
                import datetime as _dt
                if p["expires_at"] < _dt.datetime.now():
                    return False, "Срок действия промокода истёк"

            # 3. Проверка лимита использований
            if p["max_uses"] and p["used_count"] >= p["max_uses"]:
                return False, "Промокод уже использован максимальное число раз"

            # 4. Проверка типа
            if p["kind"] != "credits":
                return False, "Этот код - скидка, применяется при покупке пакета"

            # 5. Пытаемся вставить запись об использовании - тут сработает UNIQUE
            try:
                await conn.execute(
                    "INSERT INTO promo_uses (code, user_id) VALUES ($1, $2)",
                    code_upper, user_id
                )
            except asyncpg.UniqueViolationError:
                return False, "Ты уже применял этот промокод"

            # 6. Инкрементим счётчик использований промокода
            await conn.execute(
                "UPDATE promocodes SET used_count = used_count + 1 WHERE code=$1",
                code_upper
            )

    # Начисляем кредиты ВНЕ транзакции (т.к. add_credits_batch сам открывает свою)
    await add_credits_batch(user_id, p["value"], source="promo", days_valid=30)
    await log_event(user_id, "promo_redeem", f"code={code_upper} value={p['value']}")
    return True, f"Начислено {p['value']} кредитов!"


async def list_promos(only_active: bool = True, limit: int = 50) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if only_active:
            rows = await conn.fetch(
                "SELECT * FROM promocodes WHERE active=TRUE ORDER BY created_at DESC LIMIT $1", limit
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM promocodes ORDER BY created_at DESC LIMIT $1", limit
            )
    return [dict(r) for r in rows]


async def deactivate_promo(code: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.execute(
            "UPDATE promocodes SET active=FALSE WHERE code=$1", code.strip().upper()
        )
    return "UPDATE 1" in r


# ─── Партии кредитов с истечением ────────────────────────

async def add_credits_batch(user_id: int, credits: int, source: str = "purchase", days_valid: int = 30):
    """Начисляет кредиты отдельной партией. Партия сгорает через days_valid дней.
    Также обновляет основной баланс пользователя для совместимости."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if days_valid > 0:
            await conn.execute(
                f"INSERT INTO credit_batches (user_id, credits_init, credits_left, source, expires_at) "
                f"VALUES ($1, $2, $2, $3, NOW() + ($4 || ' days')::INTERVAL)",
                user_id, credits, source, str(days_valid)
            )
        else:
            await conn.execute(
                "INSERT INTO credit_batches (user_id, credits_init, credits_left, source) "
                "VALUES ($1, $2, $2, $3)",
                user_id, credits, source
            )
        await conn.execute(
            "UPDATE users SET credits = credits + $1 WHERE user_id=$2",
            credits, user_id
        )
    await log_event(user_id, f"batch_add_{source}", f"credits={credits} days={days_valid}")


async def expire_old_batches() -> int:
    """Списывает истёкшие партии. Возвращает сумму сгоревших кредитов."""
    pool = await get_pool()
    total_expired = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT id, user_id, credits_left FROM credit_batches "
                "WHERE credits_left > 0 AND expires_at IS NOT NULL AND expires_at <= NOW()"
            )
            for r in rows:
                await conn.execute(
                    "UPDATE users SET credits = GREATEST(0, credits - $1) WHERE user_id=$2",
                    r["credits_left"], r["user_id"]
                )
                await conn.execute(
                    "UPDATE credit_batches SET credits_left = 0 WHERE id=$1", r["id"]
                )
                total_expired += r["credits_left"]
                await log_event(r["user_id"], "batch_expired", f"credits={r['credits_left']}")
    return total_expired


async def ensure_user(user_id: int, username: str = "", full_name: str = "", referred_by: int = None):
    """Создаёт юзера или обновляет last_active. При первом создании начисляет 
    приветственные/реферальные кредиты как партию со сроком 30 дней.

    ВАЖНО: детекция нового юзера через RETURNING (xmax=0 → INSERT, xmax>0 → UPDATE).
    Раньше использовался 'INSERT 0 1' в conn.execute(), но PostgreSQL возвращает
    его И при INSERT, И при ON CONFLICT DO UPDATE - из-за этого кредиты начислялись
    КАЖДЫЙ раз при /start. Бах!
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if referred_by and referred_by != user_id:
            row = await conn.fetchrow("""
                INSERT INTO users (user_id, credits, username, full_name, referred_by)
                VALUES ($1, 0, $2, $3, $4)
                ON CONFLICT (user_id) DO UPDATE
                SET username=EXCLUDED.username,
                    full_name=EXCLUDED.full_name,
                    last_active=NOW()
                RETURNING (xmax = 0) AS is_new
            """, user_id, username, full_name, referred_by)
            is_new = bool(row and row["is_new"])
            if is_new:
                # Пригашённый друг получает реф-бонус как партию (ТОЛЬКО при первой регистрации)
                await add_credits_batch(user_id, REF_BONUS, source="referral", days_valid=30)
                logging.info(f"✨ New user {user_id} with referrer {referred_by}: +{REF_BONUS} cr")
        else:
            row = await conn.fetchrow("""
                INSERT INTO users (user_id, credits, username, full_name)
                VALUES ($1, 0, $2, $3)
                ON CONFLICT (user_id) DO UPDATE
                SET username=EXCLUDED.username,
                    full_name=EXCLUDED.full_name,
                    last_active=NOW()
                RETURNING (xmax = 0) AS is_new
            """, user_id, username, full_name)
            is_new = bool(row and row["is_new"])
            if is_new:
                # Приветственные кредиты партией на 30 дней (ТОЛЬКО при первой регистрации)
                await add_credits_batch(user_id, FREE_CREDITS, source="free", days_valid=30)
                logging.info(f"✨ New user {user_id}: +{FREE_CREDITS} welcome cr")

async def get_setting(key: str, default: str = "") -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key=$1", key)
        return row["value"] if row else default

async def set_setting(key: str, value: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value=$2",
            key, value
        )

async def get_user(user_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        return dict(row) if row else None

async def get_credits(user_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT credits FROM users WHERE user_id=$1", user_id)
        return row["credits"] if row else 0

async def log_payment(user_id: int, credits: int, amount_rub: int, method: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO payments (user_id, credits, amount_rub, method) VALUES ($1,$2,$3,$4)",
            user_id, credits, amount_rub, method
        )
    await log_event(user_id, "payment", f"method={method} credits={credits} amount={amount_rub}")

async def deduct(user_id: int, amount: int) -> bool:
    """Списывает кредиты с баланса юзера по FIFO из партий (самые старые первыми).
    Атомарная операция: либо списали всю сумму, либо ничего (если не хватает).
    Возвращает True если успешно."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1) Проверяем общий баланс с блокировкой
            row = await conn.fetchrow(
                "SELECT credits FROM users WHERE user_id=$1 FOR UPDATE", user_id
            )
            if not row or row["credits"] < amount:
                return False

            # 2) Списываем из партий по FIFO - только из активных (не истёкших).
            # Берём активные партии с кредитами, сортируем по expires_at ASC (сперва скоро истекающие),
            # чтобы не терять кредиты. Партии без expires_at (NULL) идут в конец.
            batches = await conn.fetch(
                """SELECT id, credits_left FROM credit_batches
                   WHERE user_id = $1 AND credits_left > 0
                     AND (expires_at IS NULL OR expires_at > NOW())
                   ORDER BY expires_at ASC NULLS LAST, id ASC
                   FOR UPDATE""",
                user_id
            )

            remaining = amount
            for b in batches:
                if remaining <= 0:
                    break
                take = min(remaining, b["credits_left"])
                await conn.execute(
                    "UPDATE credit_batches SET credits_left = credits_left - $1 WHERE id = $2",
                    take, b["id"]
                )
                remaining -= take

            # 3) Обновляем общий баланс в users (для обратной совместимости)
            await conn.execute(
                "UPDATE users SET credits = credits - $1 WHERE user_id = $2",
                amount, user_id
            )

            # Если не хватило партий (что странно - значит где-то рассинхрон),
            # логируем для диагностики, но не откатываем - общий баланс уже проверен
            if remaining > 0:
                logging.warning(
                    f"deduct partial batch mismatch uid={user_id} amount={amount} "
                    f"unallocated={remaining} - баланс списан, но партии не покрывают сумму"
                )

    await log_event(user_id, "deduct", f"amount={amount}")
    return True

async def add_credits(user_id: int, amount: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET credits = credits + $1 WHERE user_id = $2",
            amount, user_id
        )
    await log_event(user_id, "refund_or_add", f"amount={amount}")

async def block_user(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_blocked=1 WHERE user_id=$1", user_id)

async def unblock_user(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_blocked=0 WHERE user_id=$1", user_id)

async def is_blocked(user_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_blocked FROM users WHERE user_id=$1", user_id)
        return bool(row and row["is_blocked"])

async def log_gen(user_id: int, gen_type: str, model: str, credits: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO generations (user_id, type, model, credits) VALUES ($1,$2,$3,$4)",
            user_id, gen_type, model, credits
        )

# ══════════════════════════════════════════════════════════
#  FREEKASSA - ГЕНЕРАЦИЯ ССЫЛОК И ВЕБХУК
# ══════════════════════════════════════════════════════════

async def fk_save_order(order_id: str, user_id: int, credits: int, amount: int,
                         pack: str, payment_method: str = "sbp", promo_code: str | None = None):
    """Сохраняем заказ в БД (защита от потери при перезапуске)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO fk_orders (order_id, user_id, credits, amount_rub, pack, payment_method, promo_code)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (order_id) DO NOTHING
        """, order_id, user_id, credits, amount, pack, payment_method, promo_code)


async def fk_get_order(order_id: str) -> dict | None:
    """Получаем заказ из БД."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM fk_orders WHERE order_id=$1", order_id
        )
        return dict(row) if row else None


async def fk_mark_paid(order_id: str) -> bool:
    """Атомарно помечает заказ как оплаченный.

    Returns:
        True если статус был успешно изменён с 'pending' на 'paid' (это первое зачисление)
        False если заказ уже был paid (защита от повторного зачисления)
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Атомарный UPDATE с условием - если уже paid, ничего не меняем
        # ROWCOUNT покажет 1 если изменили, 0 если уже было paid
        result = await conn.execute(
            "UPDATE fk_orders SET status='paid', paid_at=NOW() "
            "WHERE order_id=$1 AND status != 'paid'",
            order_id
        )
        # asyncpg возвращает строку вида "UPDATE 1" или "UPDATE 0"
        try:
            updated_count = int(result.split()[-1]) if result else 0
        except (ValueError, AttributeError):
            updated_count = 0
        return updated_count > 0


# ══════════════════════════════════════════════════════════
#  ОБРАБОТКА ОШИБОК
# ══════════════════════════════════════════════════════════

# ─── Альтернативы при перегрузке моделей ──────────────────
# Если модель перегружена (503), предлагаем клиенту альтернативу с похожим качеством

async def get_next_claude_code(plan: str = "pro"):
    """Резервирует и возвращает следующий свободный код нужного плана."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE claude_codes SET is_used=TRUE
               WHERE id=(SELECT id FROM claude_codes
                         WHERE plan=$1 AND is_used=FALSE
                         ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
               RETURNING code""",
            plan
        )
    return row["code"] if row else None


async def release_claude_code(code: str):
    """Возвращает код в пул при неудачной активации."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE claude_codes SET is_used=FALSE, used_by=NULL, "
            "used_at=NULL, order_id=NULL, org_id=NULL WHERE code=$1",
            code
        )


async def mark_claude_code_used(code: str, user_id: int, order_id: str, org_id: str = ""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE claude_codes "
            "SET used_by=$1, order_id=$2, org_id=$3, used_at=NOW() WHERE code=$4",
            user_id, order_id, org_id, code
        )


async def save_claude_pending_activation(
    user_id: int, code: str, order_id: str, plan: str, plan_name: str
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO claude_pending_activations
               (user_id, code, order_id, plan, plan_name)
               VALUES ($1,$2,$3,$4,$5)
               ON CONFLICT (user_id) DO UPDATE
               SET code=$2, order_id=$3, plan=$4, plan_name=$5,
                   org_id='', bpa_order_id=NULL,
                   created_at=NOW(), expires_at=NOW()+INTERVAL '2 hours'""",
            user_id, code, order_id, plan, plan_name
        )


async def get_claude_pending_activation(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM claude_pending_activations "
            "WHERE user_id=$1 AND expires_at > NOW()",
            user_id
        )
    return dict(row) if row else None


async def delete_claude_pending_activation(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM claude_pending_activations WHERE user_id=$1", user_id
        )


async def count_claude_codes_by_plan() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT plan,
                      COUNT(*) FILTER(WHERE NOT is_used)                      AS free,
                      COUNT(*) FILTER(WHERE is_used AND used_by IS NOT NULL)  AS activated,
                      COUNT(*) FILTER(WHERE is_used AND used_by IS NULL)      AS reserved
               FROM claude_codes GROUP BY plan ORDER BY plan"""
        )
        total_act = await conn.fetchval(
            "SELECT COUNT(*) FROM claude_codes WHERE is_used=TRUE AND used_by IS NOT NULL"
        ) or 0
        last_used = await conn.fetchrow(
            """SELECT code, plan, used_at, used_by
               FROM claude_codes WHERE is_used=TRUE AND used_by IS NOT NULL
               ORDER BY used_at DESC LIMIT 1"""
        )
    return {
        "by_plan": {r["plan"]: {"free": r["free"], "activated": r["activated"], "reserved": r["reserved"]} for r in rows},
        "total_activations": total_act,
        "last_used": dict(last_used) if last_used else None,
    }


# ─── Отправить WebApp клиенту ─────────────────────────────────────────────────

async def fk_save_order(order_id: str, user_id: int, credits: int,
                        amount_rub: int, pack: str = ""):
    """Сохраняет заказ в fk_orders (status=pending)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO fk_orders (order_id, user_id, credits, amount_rub, pack, status)
            VALUES ($1,$2,$3,$4,$5,'pending')
            ON CONFLICT (order_id) DO NOTHING
        """, order_id, user_id, credits, amount_rub, pack)


