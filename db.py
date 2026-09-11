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
    FREE_CREDITS, IMAGE_MODELS, REF_BONUS, REF_WELCOME_CREDITS, SHOP_CATALOG, VIDEO_MODELS, _pool,
    GPT_CODE_ROUTES,
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
            ("num",            "BIGSERIAL"), # человекочитаемый номер заказа (#N)
            ("coins_spent",    "INTEGER DEFAULT 0"), # монетки, списанные под доплату СБП (для возврата)
            ("client_msg_id",  "BIGINT"),   # ID сообщения оплаты у КЛИЕНТА (гасим кнопки после оплаты)
            ("fk_intid",       "TEXT"),     # номер платежа В САМОЙ FreeKassa (intid) — по нему ищется платёж
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
            ("ref_premium",     "BOOLEAN DEFAULT FALSE"),
            ("ref_premium_pct", "DOUBLE PRECISION DEFAULT NULL"),
            # Партнёрская программа (B2B): партнёр приводит клиентов по своей
            # реф-ссылке, его клиенты видят цены с наценкой, разница — доход
            # партнёра. partner_discount_pct — НАСКОЛЬКО дешевле розницы отдаём
            # товар партнёру (наша доля), partner_markup_pct — наценка к рознице
            # для ЕГО клиентов. Оба на весь каталог; по сервисам — отдельная
            # таблица partner_rates, она перекрывает эти значения.
            ("partner",              "BOOLEAN DEFAULT FALSE"),
            ("partner_discount_pct", "DOUBLE PRECISION DEFAULT 0"),
            ("partner_markup_pct",   "DOUBLE PRECISION DEFAULT 0"),
            # Клиент закреплён за партнёром НАВСЕГДА и только если пришёл новым.
            # Отдельная колонка, а не referred_by: обычная рефералка живёт своей
            # жизнью, и путать их нельзя.
            ("partner_id",           "BIGINT DEFAULT NULL"),
            # Скидка, которую партнёр даёт СВОИМ клиентам. Полная цена (с наценкой)
            # показывается зачёркнутой, платит клиент со скидкой. Скидка условная —
            # кто под условие не попал, платит полную: иначе зачёркнутая цена была бы
            # фикцией. mode: 'off' | 'days' (N дней с прихода) | 'first' (первая покупка).
            ("partner_promo_pct",  "DOUBLE PRECISION DEFAULT 0"),
            ("partner_promo_mode", "TEXT DEFAULT 'off'"),
            ("partner_promo_days", "INTEGER DEFAULT 7"),
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
        # Лог премиум-реферальных начислений (для месячного лимита и аудита)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ref_premium_log (
                id          SERIAL PRIMARY KEY,
                referrer_id BIGINT NOT NULL,
                referee_id  BIGINT,
                order_id    TEXT UNIQUE,
                amount_rub  NUMERIC(12,2),
                coins       NUMERIC(12,2),
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_refprem_referrer_time "
            "ON ref_premium_log(referrer_id, created_at)"
        )
        # ── Партнёрская программа ───────────────────────────────────────────
        # Ставки по конкретному сервису каталога. Перекрывают общие проценты
        # партнёра. Нет строки — действуют общие.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS partner_rates (
                partner_id   BIGINT NOT NULL,
                svc_key      TEXT   NOT NULL,
                discount_pct DOUBLE PRECISION,
                markup_pct   DOUBLE PRECISION,
                updated_at   TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (partner_id, svc_key)
            )
        """)
        # Начисления партнёру. order_id уникален — повторный вебхук по тому же
        # заказу не начислит дважды (как в ref_premium_log).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS partner_earnings (
                id           SERIAL PRIMARY KEY,
                partner_id   BIGINT NOT NULL,
                client_id    BIGINT,
                order_id     TEXT UNIQUE,
                svc_key      TEXT,
                plan_idx     INTEGER,
                base_price   NUMERIC(12,2),   -- розничная цена
                client_price NUMERIC(12,2),   -- цена с наценкой (что видел клиент)
                paid_amount  NUMERIC(12,2),   -- сколько реально оплачено (после скидок)
                partner_sum  NUMERIC(12,2),   -- доля партнёра
                owner_sum    NUMERIC(12,2),   -- осталось владельцу бота
                created_at   TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_partner_earn ON partner_earnings(partner_id, created_at)")
        # Выплаты партнёру (админ отмечает вручную).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS partner_payouts (
                id         SERIAL PRIMARY KEY,
                partner_id BIGINT NOT NULL,
                amount     NUMERIC(12,2) NOT NULL,
                note       TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_partner_payouts ON partner_payouts(partner_id, created_at)")
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
        # Привязка промокода к сервису магазина (NULL = для всех сервисов)
        try:
            await conn.execute("ALTER TABLE promocodes ADD COLUMN service_key TEXT")
        except Exception:
            pass
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
        # Миграция: купленные и начисленные админом кредиты не должны сгорать.
        # Снимаем срок с уже существующих таких партий (чтобы прежние покупки
        # тоже перестали гаснуть). Бонусные (promo/referral/free) не трогаем.
        await conn.execute(
            "UPDATE credit_batches SET expires_at = NULL "
            "WHERE expires_at IS NOT NULL AND credits_left > 0 "
            "AND COALESCE(source,'') IN ('purchase','admin_manual')"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS consultant_conv (
                user_id    BIGINT PRIMARY KEY,
                messages   TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Состояния диалогов (FSM). Раньше жили только в памяти процесса, и любой
        # деплой выбрасывал клиентов из середины сценария («Сессия сброшена»).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS fsm_storage (
                key        TEXT PRIMARY KEY,
                state      TEXT,
                data       JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fsm_updated ON fsm_storage(updated_at)")
        # Замок «одна активация на клиента» (переживает рестарт процесса).
        # Раньше защита от параллельного запуска цепочки Claude жила в словаре
        # в памяти: два одновременных запуска забирали ДВА кода из пула под одну
        # оплату — второй код сгорал впустую.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS activation_claims (
                key        TEXT PRIMARY KEY,
                claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS order_thread (
                id         SERIAL PRIMARY KEY,
                order_id   TEXT NOT NULL,
                sender     TEXT NOT NULL,
                text       TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_order_thread ON order_thread(order_id, id)")
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
        # Разовая миграция: возвращаем в пул свободные коды ChatGPT, которые речекер
        # мог ошибочно пометить check_status='used'/'invalid' (старые слишком широкие
        # маркеры на 987ai.vip). Сбрасываем статус в 'unchecked' — исправленный речекер
        # перепроверит и по-настоящему плохие пометит корректно. Выполняется один раз.
        try:
            _already = await conn.fetchval(
                "SELECT value FROM settings WHERE key='migr_gpt_recheck_reset_v1'")
            if _already != "1":
                _has_cs = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='gpt_codes' AND column_name='check_status')")
                if _has_cs:
                    _rr = await conn.execute(
                        "UPDATE gpt_codes SET check_status='unchecked', flagged_reason=NULL "
                        "WHERE is_used=FALSE AND check_status IN ('used','invalid')")
                    logging.info(f"migr_gpt_recheck_reset_v1: вернул в пул коды → {_rr}")
                await conn.execute(
                    "INSERT INTO settings (key, value) VALUES ('migr_gpt_recheck_reset_v1','1') "
                    "ON CONFLICT (key) DO UPDATE SET value='1'")
        except Exception as _mig_e:
            logging.warning(f"migr_gpt_recheck_reset_v1 skipped: {_mig_e}")
        # Синхронизация статусов: если заказ отменён в linkpay_orders, но в fk_orders
        # остался 'paid' — приводим в соответствие (иначе отменённые заказы всё ещё
        # учитывались в прибыли/статистике). Идемпотентно, выполняется при каждом старте.
        try:
            await conn.execute(
                "UPDATE fk_orders SET status='cancelled' WHERE status='paid' AND order_id IN "
                "(SELECT fk_order_id FROM linkpay_orders WHERE status='cancelled')")
        except Exception as _mig_c:
            logging.warning(f"cancel-sync migration skipped: {_mig_c}")
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
            ("provider",         "TEXT NOT NULL DEFAULT '987ai'"),  # сайт активации (987ai|aipro)
            # Маршрут активации: 'ios' кладётся на любой аккаунт, 'ph' — только
            # на free plan. NULL = префикс незнакомый, разметить вручную.
            ("route",            "TEXT"),
        ]:
            try:
                await conn.execute(f"ALTER TABLE gpt_codes ADD COLUMN {_col} {_def}")
            except Exception:
                pass
        # Разметка маршрута по префиксу. Кодов в базу добавляют из трёх мест
        # (команда, FSM-экран, мини-апп), поэтому вешаем ТРИГГЕР — так ни один
        # путь вставки не сможет её обойти, включая ручной SQL. Тело триггера
        # генерим из GPT_CODE_ROUTES, чтобы карта префиксов жила в одном месте.
        _branches = "\n".join(
            f"    {'IF' if _i == 0 else 'ELSIF'} UPPER(NEW.code) LIKE '{_pref.upper()}%' "
            f"THEN NEW.route := '{_route}';"
            for _i, (_pref, _route) in enumerate(GPT_CODE_ROUTES.items()))
        try:
            await conn.execute(f"""
                CREATE OR REPLACE FUNCTION gpt_codes_set_route() RETURNS trigger AS $fn$
                BEGIN
                  IF NEW.route IS NULL THEN
                {_branches}
                    END IF;
                  END IF;
                  RETURN NEW;
                END $fn$ LANGUAGE plpgsql""")
            await conn.execute("DROP TRIGGER IF EXISTS trg_gpt_codes_route ON gpt_codes")
            await conn.execute(
                "CREATE TRIGGER trg_gpt_codes_route BEFORE INSERT OR UPDATE OF code "
                "ON gpt_codes FOR EACH ROW EXECUTE FUNCTION gpt_codes_set_route()")
        except Exception as _e_trg:
            logging.warning(f"gpt_codes route-триггер не создался: {_e_trg}")
        # Разовый бэкофилл для кодов, залитых до появления колонки.
        for _pref, _route in GPT_CODE_ROUTES.items():
            try:
                await conn.execute(
                    "UPDATE gpt_codes SET route=$1 WHERE route IS NULL AND UPPER(code) LIKE $2",
                    _route, _pref.upper() + "%")
            except Exception:
                pass
        try:
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gpt_codes_free_route "
                "ON gpt_codes(route, plan, is_used) WHERE is_used = FALSE")
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
        # Мультипровайдер ChatGPT: сайт активации + сырой Session JSON клиента
        for _col2, _def2 in [
            ("provider",    "TEXT NOT NULL DEFAULT '987ai'"),
            ("session_raw", "TEXT"),
            # Метка «активация уже запущена» — переживает рестарт бота, в отличие
            # от прежней защиты в памяти процесса (_gpt_job_active).
            ("activating_at", "TIMESTAMPTZ"),
        ]:
            try:
                await conn.execute(f"ALTER TABLE gpt_pending_activations ADD COLUMN {_col2} {_def2}")
            except Exception:
                pass
        try:
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gpt_codes_free_prov "
                "ON gpt_codes(provider, plan, is_used) WHERE is_used = FALSE")
        except Exception:
            pass
        # Фикс: речекер (987ai) ошибочно метил invalid коды ДРУГИХ сайтов.
        # Возвращаем такие свободные коды в строй (снимаем ложный статус).
        try:
            await conn.execute(
                "UPDATE gpt_codes SET check_status='unchecked', flagged_reason=NULL "
                "WHERE provider <> '987ai' AND is_used=FALSE "
                "AND COALESCE(check_status,'unchecked') IN ('invalid','error')")
        except Exception:
            pass

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
        # ── Мультипровайдер Claude: у каждого сайта свой пул кодов ───────────────────
        # provider: 'bpa' (bypriceactivate.pro) | 'root' (rootchatgptplus.com) | ...
        await conn.execute(
            "ALTER TABLE claude_codes "
            "ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'bpa'"
        )
        await conn.execute(
            "ALTER TABLE claude_pending_activations "
            "ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'bpa'"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claude_codes_free_prov "
            "ON claude_codes(provider, plan, is_used) WHERE is_used = FALSE"
        )
        # ── Perplexity коды и pending активации ─────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS perplexity_codes (
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
            "CREATE INDEX IF NOT EXISTS idx_perplexity_codes_free "
            "ON perplexity_codes(plan, is_used) WHERE is_used = FALSE"
        )
        # Колонки для сверки пула с сайтом активации — такие же, как у ChatGPT.
        for _tbl_chk in ("claude_codes", "perplexity_codes"):
            for _col_chk, _def_chk in (
                ("check_status",    "TEXT DEFAULT 'unchecked'"),
                ("last_checked_at", "TIMESTAMPTZ"),
                ("flagged_reason",  "TEXT"),
            ):
                try:
                    await conn.execute(
                        f"ALTER TABLE {_tbl_chk} ADD COLUMN IF NOT EXISTS "
                        f"{_col_chk} {_def_chk}")
                except Exception:
                    pass
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS perplexity_pending_activations (
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
        # ── Заказы «оплата по ссылке» (HeyGen, Suno, Kling, Higgsfield и т.п.) ──
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS linkpay_orders (
                id            SERIAL PRIMARY KEY,
                user_id       BIGINT NOT NULL,
                username      TEXT DEFAULT '',
                fk_order_id   TEXT NOT NULL UNIQUE,
                service_key   TEXT NOT NULL DEFAULT '',
                service_name  TEXT NOT NULL DEFAULT '',
                plan_name     TEXT NOT NULL DEFAULT '',
                amount_rub    INTEGER DEFAULT 0,
                status        TEXT DEFAULT 'awaiting_link',
                payment_link  TEXT DEFAULT '',
                admin_msg_id  BIGINT,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_linkpay_uid ON linkpay_orders(user_id, status)"
        )
        for _lpc, _lpd in [("kind", "TEXT DEFAULT 'linkpay'"), ("account_email", "TEXT DEFAULT ''"), ("account_pass", "TEXT DEFAULT ''")]:
            try:
                await conn.execute(f"ALTER TABLE linkpay_orders ADD COLUMN IF NOT EXISTS {_lpc} {_lpd}")
            except Exception:
                pass
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

async def get_next_gpt_code(plan: str = "plus", provider: str = "987ai",
                            route: str | None = None):
    """Выдаёт следующий свободный код ИЗ ПУЛА КОНКРЕТНОГО САЙТА.

    Приоритет: 'ok' → 'unchecked' → помеченные. 'used'/'invalid' не выдаются.

    route ('ios' | 'ph') сужает выборку до нужного маршрута. Это ключевая
    защита: филиппинский код на аккаунте с активной подпиской сгорает впустую,
    поэтому его нельзя выдать «заодно». route=None — старое поведение
    (любой код), оставлено для прочих сайтов и ручных сценариев.
    """
    pool = await get_pool()
    _r = (route or "").strip().lower() or None
    async with pool.acquire() as conn:
        # Три очереди: подтверждённые сверкой → непроверенные → помеченные
        # подозрительными. Помеченные выдаются ПОСЛЕДНИМИ, но не блокируются:
        # сверка могла ошибиться, а без кода клиент останется ни с чем.
        for _status_cond in ("COALESCE(check_status,'unchecked') = 'ok'",
                             "COALESCE(check_status,'unchecked') = 'unchecked'",
                             "COALESCE(check_status,'unchecked') NOT IN ('used','invalid')"):
            if _r:
                row = await conn.fetchrow(
                    f"""UPDATE gpt_codes SET is_used=TRUE, reserved_at=NOW()
                        WHERE id=(SELECT id FROM gpt_codes
                                  WHERE plan=$1 AND provider=$2 AND is_used=FALSE
                                    AND route=$3 AND {_status_cond}
                                  ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
                        RETURNING code""", plan, provider, _r)
            else:
                row = await conn.fetchrow(
                    f"""UPDATE gpt_codes SET is_used=TRUE, reserved_at=NOW()
                        WHERE id=(SELECT id FROM gpt_codes
                                  WHERE plan=$1 AND provider=$2 AND is_used=FALSE
                                    AND {_status_cond}
                                  ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
                        RETURNING code""", plan, provider)
            if row:
                return row["code"]
    return None


async def count_gpt_free_by_route() -> list[dict]:
    """Свободные коды по маршруту и тарифу: [{route, plan, free}]."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT COALESCE(route,'') AS route, plan, COUNT(*) AS free "
            "FROM gpt_codes WHERE is_used=FALSE "
            "AND COALESCE(check_status,'unchecked') NOT IN ('used','invalid') "
            "GROUP BY COALESCE(route,''), plan ORDER BY route, plan")
    return [dict(r) for r in rows]


async def set_gpt_code_route(code: str, route: str) -> bool:
    """Ручная разметка маршрута для кода с незнакомым префиксом."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.execute("UPDATE gpt_codes SET route=$2 WHERE code=$1",
                               code, (route or "").strip().lower() or None)
    return str(r).split()[-1] == "1"


async def count_gpt_free_by_provider() -> dict:
    """Свободные коды ChatGPT по сайтам: {provider: count}."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT provider, COUNT(*) AS free FROM gpt_codes "
            "WHERE is_used=FALSE AND COALESCE(check_status,'unchecked') NOT IN ('used','invalid') "
            "GROUP BY provider")
    return {r["provider"]: int(r["free"]) for r in rows}

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

async def activation_hours(svc: str = None) -> int:
    """Срок жизни сессии активации в часах (настраивается админом, по сервисам).
    Приоритет: activation_hours:{svc} → общий activation_hours → 12."""
    try:
        _v = ""
        if svc:
            _v = await get_setting(f"activation_hours:{svc}", "") or ""
        if not _v:
            _v = await get_setting("activation_hours", "12") or "12"
        h = int(_v)
    except Exception:
        h = 12
    return max(1, min(168, h))


async def save_pending_activation(user_id: int, code: str, order_id: str, plan: str, plan_name: str,
                                  provider: str = "987ai"):
    _h = await activation_hours("chatgpt")
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Срок активации настраивается (activation_hours, по умолчанию 12ч) — и при
        # создании, и при обновлении. make_interval позволяет подставить часы параметром.
        await conn.execute(
            """INSERT INTO gpt_pending_activations
                   (user_id, code, order_id, plan, plan_name, provider, expires_at)
               VALUES ($1,$2,$3,$4,$5,$6, NOW()+make_interval(hours => $7))
               ON CONFLICT (user_id) DO UPDATE
               SET code=$2, order_id=$3, plan=$4, plan_name=$5, provider=$6, session_raw=NULL,
                   created_at=NOW(), expires_at=NOW()+make_interval(hours => $7)""",
            user_id, code, order_id, plan, plan_name, provider, _h)

async def claim_gpt_activation(user_id: int, stale_minutes: int = 10) -> bool:
    """Атомарно «занимает» активацию клиента. True — можно запускать.

    Раньше защита от параллельного запуска жила в словаре в памяти процесса и
    терялась при каждом деплое: два запуска брали ДВА кода из пула, первый
    активировал подписку, второй падал с «на аккаунте уже есть Plus» — минус один
    оплаченный код. Метка в БД переживает рестарт; через stale_minutes она
    считается протухшей (зависшая активация не блокирует клиента навсегда).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.execute(
            "UPDATE gpt_pending_activations SET activating_at = NOW() "
            "WHERE user_id = $1 AND (activating_at IS NULL "
            "      OR activating_at < NOW() - make_interval(mins => $2))",
            user_id, int(stale_minutes)
        )
    try:
        return str(r).split()[-1] == "1"
    except Exception:
        return True


async def claim_activation(key: str, stale_minutes: int = 20) -> bool:
    """Атомарно занимает замок с именем key. True — можно запускать.

    Работает как INSERT ... ON CONFLICT DO UPDATE с условием по возрасту: если
    замок свежий (кто-то уже работает) — обновления не будет и вернётся False.
    Через stale_minutes замок считается протухшим (зависшая задача не блокирует
    клиента навсегда). Не привязан к конкретной таблице — годится для любой
    длительной операции «по одной на клиента».
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.execute(
            "INSERT INTO activation_claims (key, claimed_at) VALUES ($1, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET claimed_at = NOW() "
            "WHERE activation_claims.claimed_at < NOW() - make_interval(mins => $2)",
            key, int(stale_minutes)
        )
    try:
        return str(r).split()[-1] == "1"
    except Exception:
        return True


async def activation_cooldown(key: str, seconds: int = 60) -> int:
    """Пропускает не чаще одной попытки в `seconds` секунд.

    Возвращает 0 — можно запускать (отметка времени обновлена), либо число
    секунд, которые осталось подождать. Отметка живёт в той же таблице
    activation_claims и переживает рестарт: раньше клиент мог долбить кнопку
    «Активировать» без конца, каждое нажатие поднимало полную цепочку по сайтам
    и заваливало админа алертами.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO activation_claims (key, claimed_at) VALUES ($1, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET claimed_at = NOW() "
            "WHERE activation_claims.claimed_at < NOW() - make_interval(secs => $2) "
            "RETURNING claimed_at",
            key, int(seconds)
        )
        if row is not None:
            return 0
        left = await conn.fetchval(
            "SELECT CEIL(EXTRACT(EPOCH FROM ("
            "  activation_claims.claimed_at + make_interval(secs => $2) - NOW()"
            "))) FROM activation_claims WHERE key = $1",
            key, int(seconds)
        )
    try:
        return max(1, int(left or 1))
    except Exception:
        return int(seconds)


async def release_activation(key: str):
    """Снимает замок (после завершения задачи — успешного или нет)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM activation_claims WHERE key = $1", key)


async def release_gpt_activation(user_id: int):
    """Снимает метку активации (после завершения задачи — успешного или нет)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE gpt_pending_activations SET activating_at = NULL WHERE user_id = $1",
            user_id)


async def get_pending_activation(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM gpt_pending_activations WHERE user_id=$1 AND expires_at>NOW()", user_id)
    return dict(row) if row else None

async def get_pending_activation_by_code(code: str):
    """Фолбэк-идентификация: найти pending по коду активации (когда initData не прошёл)."""
    if not code:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM gpt_pending_activations WHERE code=$1 AND expires_at>NOW() "
            "ORDER BY created_at DESC LIMIT 1", code)
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

def _apply_desc_override_to_memory(key: str, plan_name: str, descr: str):
    """Применяет один оверрайд описания к SHOP_CATALOG в памяти.
    plan_name='' → описание сервиса; иначе — описание конкретного тарифа (по имени)."""
    if not descr or key not in SHOP_CATALOG:
        return
    if not plan_name:
        SHOP_CATALOG[key]["desc"] = descr
    else:
        for _pl in SHOP_CATALOG[key].get("plans", []):
            if (_pl.get("name", "") or "").strip() == plan_name.strip():
                _pl["desc"] = descr
                return


async def save_shop_desc_override(key: str, plan_name: str, descr: str):
    """Сохраняет авто-обновлённое описание в БД (переживает рестарт, применяется на загрузке)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shop_desc_overrides (
                key        TEXT NOT NULL,
                plan_name  TEXT NOT NULL DEFAULT '',
                descr      TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (key, plan_name)
            )
        """)
        await conn.execute("""
            INSERT INTO shop_desc_overrides (key, plan_name, descr, updated_at)
            VALUES ($1,$2,$3,NOW())
            ON CONFLICT (key, plan_name) DO UPDATE SET descr=$3, updated_at=NOW()
        """, key, plan_name or "", descr or "")


async def get_shop_desc_overrides() -> dict:
    """Возвращает {key: {'': service_desc, '<план>': desc, ...}}."""
    pool = await get_pool()
    out: dict = {}
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch("SELECT key, plan_name, descr FROM shop_desc_overrides")
        except Exception:
            return out
    for r in rows:
        out.setdefault(r["key"], {})[r["plan_name"] or ""] = r["descr"] or ""
    return out


# ─── Черновики описаний (ждут подтверждения админа перед публикацией) ──────────
async def _ensure_desc_drafts_table(conn):
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS shop_desc_drafts (
            key        TEXT NOT NULL,
            plan_name  TEXT NOT NULL DEFAULT '',
            old_descr  TEXT DEFAULT '',
            new_descr  TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (key, plan_name)
        )
    """)


async def save_desc_drafts(items):
    """items: список (key, plan_name, old_descr, new_descr). Полностью заменяет черновики."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure_desc_drafts_table(conn)
        await conn.execute("DELETE FROM shop_desc_drafts")
        for k, pn, old, new in items:
            await conn.execute(
                "INSERT INTO shop_desc_drafts (key,plan_name,old_descr,new_descr) "
                "VALUES ($1,$2,$3,$4) ON CONFLICT (key,plan_name) "
                "DO UPDATE SET old_descr=$3, new_descr=$4, created_at=NOW()",
                k, pn or "", old or "", new or "")


async def get_desc_drafts() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                "SELECT key, plan_name, old_descr, new_descr FROM shop_desc_drafts "
                "ORDER BY key, plan_name")
        except Exception:
            return []
    return [dict(r) for r in rows]


async def update_desc_draft(key: str, plan_name: str, new_descr: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure_desc_drafts_table(conn)
        await conn.execute(
            "UPDATE shop_desc_drafts SET new_descr=$1 WHERE key=$2 AND plan_name=$3",
            new_descr, key, plan_name or "")


async def clear_desc_drafts():
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute("DELETE FROM shop_desc_drafts")
        except Exception:
            pass


async def apply_desc_drafts() -> int:
    """Публикует все черновики: переносит в оверрайды + применяет к SHOP_CATALOG. Возвращает кол-во."""
    rows = await get_desc_drafts()
    for r in rows:
        await save_shop_desc_override(r["key"], r["plan_name"] or "", r["new_descr"] or "")
        _apply_desc_override_to_memory(r["key"], r["plan_name"] or "", r["new_descr"] or "")
    await clear_desc_drafts()
    return len(rows)


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
                # Сопоставляем тариф из кода с тарифом из БД ПО НАЗВАНИЮ, а не по индексу —
                # иначе добавленный/переставленный тариф (напр. Go) подхватывает чужое имя/описание.
                _dbname = (r["plan_name"] or "").strip()
                code_plan = next(
                    (cp for cp in code_plans if (cp.get("name", "") or "").strip() == _dbname),
                    {}
                )
                SHOP_CATALOG[k]["plans"].append({
                    "name":  code_plan.get("name",  r["plan_name"]),
                    "price": r["price"],   # цена — из БД (сохраняет правки через /admin)
                    "stars": r["stars"],
                    "desc":  code_plan.get("desc",  r["plan_desc"]),
                })

            # _nsgifts-сервисы (App Store / NS Gifts) живут только в коде (без тарифов в БД),
            # поэтому при пересборке каталога из БД их нужно вернуть — иначе кнопка пропадает.
            for _k, _svc in _code_catalog.items():
                if _svc.get("_nsgifts"):
                    if _k not in SHOP_CATALOG:
                        SHOP_CATALOG[_k] = dict(_svc)
                        SHOP_CATALOG[_k].setdefault("_key", _k)
                    else:
                        SHOP_CATALOG[_k]["_nsgifts"] = True

            # Синхронизируем описания из кода обратно в БД (чтобы не устаревали)
            for key, svc in _code_catalog.items():
                await conn.execute(
                    "UPDATE bot_shop_items SET service_name=$1, emoji=$2, "
                    "service_desc=CASE WHEN service_desc IS NULL OR service_desc='' THEN $3 ELSE service_desc END "
                    "WHERE key=$4",
                    svc["name"], svc.get("emoji", ""), svc.get("desc", ""), key
                )
                # ВАЖНО: НЕ переименовываем тарифы по позиции (plan_idx) — иначе после деплоя
                # имя и цена разъезжаются. Сопоставляем по ИМЕНИ и только дозаполняем пустые описания.
                for plan in svc.get("plans", []):
                    await conn.execute(
                        "UPDATE bot_shop_items SET "
                        "plan_desc=CASE WHEN plan_desc IS NULL OR plan_desc='' THEN $1 ELSE plan_desc END "
                        "WHERE key=$2 AND plan_name=$3",
                        plan.get("desc", ""), key, plan["name"]
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

        # ── Оверрайды описаний (авто-обновление моделей) поверх кода ──
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS shop_desc_overrides (
                    key        TEXT NOT NULL,
                    plan_name  TEXT NOT NULL DEFAULT '',
                    descr      TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (key, plan_name)
                )
            """)
            _ovr = await conn.fetch("SELECT key, plan_name, descr FROM shop_desc_overrides")
            for _o in _ovr:
                _apply_desc_override_to_memory(_o["key"], _o["plan_name"] or "", _o["descr"] or "")
        except Exception as _e:
            logging.error(f"apply shop_desc_overrides: {_e}")

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

async def create_promo(code: str, kind: str, value: int, max_uses: int = 1, days_valid: int = 0,
                       service_key: str = None) -> tuple[bool, str]:
    """Создаёт промокод. kind: 'percent' или 'credits'. days_valid=0 - бессрочный.
    service_key: сервис магазина, на который действует скидка (None = на все)."""
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
            _svc = service_key or None
            if days_valid > 0:
                await conn.execute(
                    "INSERT INTO promocodes (code, kind, value, max_uses, expires_at, service_key) "
                    "VALUES ($1, $2, $3, $4, NOW() + ($5 || ' days')::INTERVAL, $6)",
                    code, kind, value, max_uses, str(days_valid), _svc
                )
            else:
                await conn.execute(
                    "INSERT INTO promocodes (code, kind, value, max_uses, service_key) VALUES ($1, $2, $3, $4, $5)",
                    code, kind, value, max_uses, _svc
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
    # Лимитированный промокод: учитываем «занятые» лимиты — заказы других клиентов,
    # созданные с этим кодом и ещё не оплаченные. Без этого один код с max_uses=1
    # можно было применить в неограниченном числе параллельных заказов: счётчик
    # растёт только при оплате, и скидку получали все.
    if p["max_uses"]:
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                _reserved = await conn.fetchval(
                    "SELECT COUNT(*) FROM fk_orders "
                    "WHERE promo_code=$1 AND status='pending' AND user_id <> $2 "
                    "AND created_at > NOW() - INTERVAL '30 minutes'",
                    code.strip().upper(), user_id
                ) or 0
            if p["used_count"] + int(_reserved) >= p["max_uses"]:
                return False, "Промокод сейчас занят — лимит применений исчерпан", None
        except Exception as _e_res:
            logging.warning(f"check_promo reserved count {code}: {_e_res}")
    # Безлимитный СКИДОЧНЫЙ промокод (kind='percent', max_uses=0) можно применять
    # одному и тому же юзеру многократно (это скидка на покупку, а не начисление
    # кредитов — фарма нет). Для кредитных и лимитированных — проверка «раз на юзера».
    _unlimited_discount = (p.get("kind") == "percent" and not p.get("max_uses"))
    if not _unlimited_discount:
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
        # Партия и баланс — в одной транзакции (чтобы не разъехались при сбое между ними)
        async with conn.transaction():
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
    """Списывает истёкшие партии. Возвращает сумму сгоревших кредитов.
    ВАЖНО: купленные и начисленные админом кредиты НЕ сгорают никогда —
    они принадлежат клиенту и должны быть потрачены им самим. Сгорать могут
    только бонусные партии (promo/referral/free)."""
    pool = await get_pool()
    total_expired = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT id, user_id, credits_left FROM credit_batches "
                "WHERE credits_left > 0 AND expires_at IS NOT NULL AND expires_at <= NOW() "
                "AND COALESCE(source,'') NOT IN ('purchase','admin_manual')"
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


async def save_consultant_conv(user_id: int, messages: list):
    """Сохраняет историю диалога с консультантом (переживает рестарт)."""
    import json as _j
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO consultant_conv (user_id, messages, updated_at) VALUES ($1,$2,NOW()) "
            "ON CONFLICT (user_id) DO UPDATE SET messages=$2, updated_at=NOW()",
            user_id, _j.dumps(messages, ensure_ascii=False))


async def load_consultant_conv(user_id: int) -> list:
    """Загружает историю диалога с консультантом."""
    import json as _j
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT messages FROM consultant_conv WHERE user_id=$1", user_id)
    if not row or not row["messages"]:
        return []
    try:
        v = row["messages"]
        return _j.loads(v) if isinstance(v, str) else (v or [])
    except Exception:
        return []


async def add_order_msg(order_id: str, sender: str, text: str):
    """Добавляет сообщение в тред заказа (sender: 'admin'|'client')."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO order_thread (order_id, sender, text) VALUES ($1,$2,$3)",
            order_id, sender, text)


async def get_order_thread(order_id: str, limit: int = 60) -> list:
    """Возвращает всю переписку по заказу."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT sender, text, created_at FROM order_thread WHERE order_id=$1 ORDER BY id LIMIT $2",
            order_id, limit)
    return [dict(r) for r in rows]


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
                await add_credits_batch(user_id, REF_WELCOME_CREDITS, source="referral", days_valid=30)
                logging.info(f"✨ New user {user_id} with referrer {referred_by}: +{REF_WELCOME_CREDITS} cr")
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

async def add_credits(user_id: int, amount: int, source: str = "refund"):
    """Начисление/возврат кредитов.

    ВАЖНО: положительное начисление теперь создаёт ПАРТИЮ (credit_batches), как и
    покупка. Раньше возвраты меняли только users.credits, и учёт разъезжался:
    сумма партий становилась меньше баланса, возвращённые кредиты не попадали ни
    в предупреждения о сгорании, ни в отчёты, а аудит балансов показывал ложные
    «лишние» кредиты. Возврат не сгорает (days_valid=0) — это деньги клиента.
    """
    if amount and int(amount) > 0:
        await add_credits_batch(user_id, int(amount), source=source, days_valid=0)
        await log_event(user_id, "refund_or_add", f"amount={amount}")
        return
    # Отрицательная сумма (админское списание) — прежнее поведение
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET credits = GREATEST(0, credits + $1) WHERE user_id = $2",
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

async def get_next_claude_code(plan: str = "pro", provider: str = "bpa"):
    """Резервирует и возвращает следующий свободный код нужного плана
    ИЗ ПУЛА КОНКРЕТНОГО ПРОВАЙДЕРА (у каждого сайта свои коды)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Три очереди: подтверждённые сверкой → непроверенные → помеченные
        # подозрительными. Помеченные выдаются ПОСЛЕДНИМИ, но не блокируются:
        # сверка могла ошибиться, а без кода клиент останется ни с чем.
        for _status_cond in ("COALESCE(check_status,'unchecked') = 'ok'",
                             "COALESCE(check_status,'unchecked') = 'unchecked'",
                             "COALESCE(check_status,'unchecked') NOT IN ('used','invalid')"):
            row = await conn.fetchrow(
                f"""UPDATE claude_codes SET is_used=TRUE
                    WHERE id=(SELECT id FROM claude_codes
                              WHERE plan=$1 AND provider=$2 AND is_used=FALSE
                                AND {_status_cond}
                              ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
                    RETURNING code""",
                plan, provider
            )
            if row:
                return row["code"]
    return None


async def count_claude_free_by_provider() -> dict:
    """Свободные (неиспользованные) коды Claude по провайдерам: {provider: count}."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT provider, COUNT(*) AS free FROM claude_codes "
            "WHERE is_used=FALSE GROUP BY provider"
        )
    return {r["provider"]: int(r["free"]) for r in rows}


async def count_claude_free_by_provider_plan(plan: str) -> dict:
    """Свободные коды Claude ПО ТАРИФУ, разбитые по провайдерам: {provider: count}.
    Используется для выбора сайта с наибольшим стоком именно этого тарифа."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT provider, COUNT(*) AS free FROM claude_codes "
            "WHERE is_used=FALSE AND plan=$1 GROUP BY provider",
            plan
        )
    return {r["provider"]: int(r["free"]) for r in rows}


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
    user_id: int, code: str, order_id: str, plan: str, plan_name: str,
    provider: str = "bpa"
):
    _h = await activation_hours("claude")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO claude_pending_activations
               (user_id, code, order_id, plan, plan_name, provider, expires_at)
               VALUES ($1,$2,$3,$4,$5,$6, NOW()+make_interval(hours => $7))
               ON CONFLICT (user_id) DO UPDATE
               SET code=$2, order_id=$3, plan=$4, plan_name=$5, provider=$6,
                   org_id='', bpa_order_id=NULL,
                   created_at=NOW(), expires_at=NOW()+make_interval(hours => $7)""",
            user_id, code, order_id, plan, plan_name, provider, _h
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


async def get_claude_pending_activation_by_code(code: str):
    """Фолбэк-идентификация по коду активации (когда initData не прошёл)."""
    if not code:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM claude_pending_activations WHERE code=$1 AND expires_at > NOW() "
            "ORDER BY created_at DESC LIMIT 1", code)
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

# fk_save_order — определён ВЫШЕ (с payment_method/promo_code). Дубликат удалён:
# раньше эта вторая версия перекрывала первую и не принимала payment_method/promo_code,
# из-за чего pay_fk падал с TypeError и заказ не сохранялся в БД (оплата «терялась").




# ── Премиум-рефералка ─────────────────────────────────────────────────────────

async def set_ref_premium(user_id: int, enabled: bool, pct: float | None = None):
    """Включает/выключает премиум-рефералку у пользователя и (опц.) индивид. %."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET ref_premium=$1, ref_premium_pct=$2 WHERE user_id=$3",
            enabled, pct, user_id
        )


# ══════════════════════════════════════════════════════════════════════════
#  ПАРТНЁРСКАЯ ПРОГРАММА (B2B)
# ══════════════════════════════════════════════════════════════════════════

def partner_prices(base: float, discount_pct: float, markup_pct: float,
                   promo_pct: float = 0.0) -> tuple[int, int, int]:
    """По розничной цене считает (полная_цена, цена_со_скидкой, наша_доля).

    discount_pct — насколько дешевле розницы мы отдаём товар партнёру (наша доля);
    markup_pct   — наценка к рознице: из неё складывается ПОЛНАЯ цена его клиента;
    promo_pct    — скидка, которую партнёр даёт своим клиентам (0 = скидки нет).

    Полная цена показывается зачёркнутой, платит клиент цену со скидкой.
    Скидку даёт партнёр — значит она урезает ЕГО долю, а наша остаётся прежней.

    Пример: розница 1990, уступка 5%, наценка 50%, скидка 20% →
            полная 2985, к оплате 2388, нам 1891, партнёру 497.
    """
    try:
        _b = float(base or 0)
    except Exception:
        _b = 0.0
    if _b <= 0:
        return 0, 0, 0
    _d = max(0.0, min(100.0, float(discount_pct or 0)))
    _m = max(0.0, float(markup_pct or 0))
    _p = max(0.0, min(95.0, float(promo_pct or 0)))
    full = max(1, int(round(_b * (100.0 + _m) / 100.0)))
    pay = max(1, int(round(full * (100.0 - _p) / 100.0)))
    owner = int(round(_b * (100.0 - _d) / 100.0))
    # Наша доля не может превышать то, что клиент реально заплатил
    owner = max(1, min(owner, pay))
    return full, pay, owner


def partner_promo_max(discount_pct: float, markup_pct: float) -> int:
    """Максимальная скидка партнёра, при которой цена не падает ниже НАШЕЙ доли.

    Наценка 50% и уступка 5% → полная 150%, наша доля 95% от розницы,
    значит скидывать можно не больше 36%. Иначе партнёр своей акцией
    продавал бы товар дешевле, чем мы согласились его отдать.
    """
    _d = max(0.0, min(100.0, float(discount_pct or 0)))
    _m = max(0.0, float(markup_pct or 0))
    _full = 100.0 + _m
    _owner = 100.0 - _d
    if _full <= 0:
        return 0
    return max(0, int((1.0 - _owner / _full) * 100))


def partner_promo_active(mode: str, days: int, joined_at, orders_done: int) -> tuple[bool, int]:
    """Действует ли сейчас скидка партнёра для этого клиента.

    Возвращает (действует, осталось_дней). Скидка ВСЕГДА условная — иначе полную
    цену не платит никто и зачёркнутая цифра превращается в фикцию.
    """
    _mode = (mode or "off").strip().lower()
    if _mode == "first":
        return (int(orders_done or 0) == 0), 0
    if _mode == "days":
        if not joined_at:
            return False, 0
        try:
            import datetime as _dt
            _now = _dt.datetime.now(joined_at.tzinfo) if joined_at.tzinfo else _dt.datetime.now()
            _left = int(days or 0) - (_now - joined_at).days
            return (_left > 0), max(0, _left)
        except Exception:
            return False, 0
    return False, 0


async def partner_promo_ctx(client_id: int, partner_id: int) -> dict:
    """Данные для проверки скидки: когда клиент пришёл и сколько покупок сделал."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT c.created_at,
                      (SELECT COUNT(*) FROM partner_earnings e
                        WHERE e.partner_id = $2 AND e.client_id = $1) AS orders
               FROM users c WHERE c.user_id = $1""",
            client_id, partner_id)
    if not row:
        return {"joined": None, "orders": 0}
    return {"joined": row["created_at"], "orders": int(row["orders"] or 0)}


async def get_partner_of(user_id: int) -> dict | None:
    """Партнёр, за которым закреплён клиент, вместе с его общими процентами.
    None — клиент обычный (партнёрских цен не видит)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT p.user_id AS partner_id, p.partner_discount_pct, p.partner_markup_pct,
                      p.partner_promo_pct, p.partner_promo_mode, p.partner_promo_days
               FROM users c JOIN users p ON p.user_id = c.partner_id
               WHERE c.user_id = $1 AND COALESCE(p.partner, FALSE) = TRUE""",
            user_id
        )
    return dict(row) if row else None


async def get_partner_rate(partner_id: int, svc_key: str) -> tuple[float, float] | None:
    """Индивидуальные проценты партнёра по конкретному сервису (или None)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT discount_pct, markup_pct FROM partner_rates "
            "WHERE partner_id=$1 AND svc_key=$2", partner_id, svc_key
        )
    if not row:
        return None
    return (float(row["discount_pct"] or 0), float(row["markup_pct"] or 0))


async def set_partner_rate(partner_id: int, svc_key: str,
                           discount_pct: float | None, markup_pct: float | None):
    """Ставки по сервису. Обе None — строка удаляется (вернутся общие проценты)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if discount_pct is None and markup_pct is None:
            await conn.execute(
                "DELETE FROM partner_rates WHERE partner_id=$1 AND svc_key=$2",
                partner_id, svc_key)
            return
        await conn.execute(
            "INSERT INTO partner_rates (partner_id, svc_key, discount_pct, markup_pct) "
            "VALUES ($1,$2,$3,$4) ON CONFLICT (partner_id, svc_key) DO UPDATE "
            "SET discount_pct=$3, markup_pct=$4, updated_at=NOW()",
            partner_id, svc_key, discount_pct, markup_pct)


async def list_partner_rates(partner_id: int) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT svc_key, discount_pct, markup_pct FROM partner_rates "
            "WHERE partner_id=$1 ORDER BY svc_key", partner_id)
    return [dict(r) for r in rows]


async def set_partner(user_id: int, enabled: bool,
                      discount_pct: float = 0, markup_pct: float = 0):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET partner=$2, partner_discount_pct=$3, partner_markup_pct=$4 "
            "WHERE user_id=$1", user_id, bool(enabled), float(discount_pct), float(markup_pct))


def partner_markup_for_target(target_pct: float, promo_pct: float) -> float:
    """Наценка, при которой клиент в итоге платит ровно +target_pct% к рознице.

    Наценка и скидка перемножаются: итог = (1+нац)·(1−скидка) − 1.
    Отсюда нац = (1+итог)/(1−скидка) − 1. Целых процентов не хватает
    (35% даёт +14.7%, а не +15%), поэтому держим два знака после запятой.
    """
    _t = float(target_pct or 0) / 100.0
    _p = float(promo_pct or 0) / 100.0
    if _p >= 1.0:
        return 0.0
    return round(((1.0 + _t) / (1.0 - _p) - 1.0) * 100.0, 2)


async def set_partner_promo(user_id: int, pct: float, mode: str, days: int):
    """Скидка партнёра своим клиентам: процент, условие и срок."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET partner_promo_pct=$2, partner_promo_mode=$3, "
            "partner_promo_days=$4 WHERE user_id=$1",
            user_id, float(pct or 0), (mode or "off"), int(days or 0))


async def list_partners() -> list[dict]:
    """Партнёры с числом приведённых клиентов и заработком."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT u.user_id, u.username, u.full_name,
                      u.partner_discount_pct, u.partner_markup_pct,
                      u.partner_promo_pct, u.partner_promo_mode, u.partner_promo_days,
                      (SELECT COUNT(*) FROM users c WHERE c.partner_id = u.user_id) AS clients,
                      (SELECT COALESCE(SUM(partner_sum),0) FROM partner_earnings e
                        WHERE e.partner_id = u.user_id) AS earned,
                      (SELECT COALESCE(SUM(amount),0) FROM partner_payouts p
                        WHERE p.partner_id = u.user_id) AS paid
               FROM users u WHERE COALESCE(u.partner, FALSE) = TRUE
               ORDER BY earned DESC"""
        )
    return [dict(r) for r in rows]


async def attach_partner_client(client_id: int, partner_id: int) -> bool:
    """Закрепляет НОВОГО клиента за партнёром. True — закрепили.

    Закрепляем только тех, кто ещё ни за кем не числится: по договорённости
    партнёрскими становятся лишь новые клиенты, старые остаются с обычными
    ценами, даже если кликнут партнёрскую ссылку.
    """
    if not client_id or not partner_id or client_id == partner_id:
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.execute(
            "UPDATE users SET partner_id=$2 WHERE user_id=$1 AND partner_id IS NULL",
            client_id, partner_id)
    try:
        return str(r).split()[-1] == "1"
    except Exception:
        return False


async def log_partner_earning(partner_id: int, client_id: int, order_id: str,
                              svc_key: str, plan_idx: int, base_price: float,
                              client_price: float, paid_amount: float,
                              partner_sum: float, owner_sum: float) -> bool:
    """Пишет начисление партнёру. False — по этому заказу уже писали."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            r = await conn.execute(
                "INSERT INTO partner_earnings (partner_id, client_id, order_id, svc_key, "
                "plan_idx, base_price, client_price, paid_amount, partner_sum, owner_sum) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (order_id) DO NOTHING",
                partner_id, client_id, order_id, svc_key, int(plan_idx or 0),
                float(base_price), float(client_price), float(paid_amount),
                float(partner_sum), float(owner_sum))
        except Exception as e:
            logging.error(f"log_partner_earning {order_id}: {e}")
            return False
    try:
        return str(r).split()[-1] == "1"
    except Exception:
        return True


async def partner_stats(partner_id: int) -> dict:
    """Полная сводка по партнёру: клиенты, заказы, деньги, конверсия."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT
                 (SELECT COUNT(*) FROM users c WHERE c.partner_id=$1) AS clients,
                 (SELECT COUNT(DISTINCT client_id) FROM partner_earnings e
                   WHERE e.partner_id=$1) AS clients_paying,
                 (SELECT COUNT(*) FROM partner_earnings e WHERE e.partner_id=$1) AS orders,
                 (SELECT COUNT(*) FROM partner_earnings e
                   WHERE e.partner_id=$1 AND e.created_at >= date_trunc('month', NOW())
                 ) AS orders_month,
                 (SELECT COALESCE(SUM(partner_sum),0) FROM partner_earnings e
                   WHERE e.partner_id=$1) AS earned,
                 (SELECT COALESCE(SUM(partner_sum),0) FROM partner_earnings e
                   WHERE e.partner_id=$1 AND e.created_at >= date_trunc('month', NOW())
                 ) AS earned_month,
                 (SELECT COALESCE(SUM(paid_amount),0) FROM partner_earnings e
                   WHERE e.partner_id=$1) AS turnover,
                 (SELECT COALESCE(SUM(paid_amount),0) FROM partner_earnings e
                   WHERE e.partner_id=$1 AND e.created_at >= date_trunc('month', NOW())
                 ) AS turnover_month,
                 (SELECT COALESCE(SUM(owner_sum),0) FROM partner_earnings e
                   WHERE e.partner_id=$1) AS owner_sum,
                 (SELECT MIN(created_at) FROM partner_earnings e WHERE e.partner_id=$1) AS first_order,
                 (SELECT MAX(created_at) FROM partner_earnings e WHERE e.partner_id=$1) AS last_order,
                 (SELECT COALESCE(SUM(amount),0) FROM partner_payouts p
                   WHERE p.partner_id=$1) AS paid""",
            partner_id)
    d = dict(row) if row else {}
    d["balance"] = float(d.get("earned") or 0) - float(d.get("paid") or 0)
    _o = int(d.get("orders") or 0)
    d["avg_check"] = round(float(d.get("turnover") or 0) / _o, 2) if _o else 0.0
    _c = int(d.get("clients") or 0)
    d["conversion"] = round(int(d.get("clients_paying") or 0) / _c * 100, 1) if _c else 0.0
    return d


async def partner_payouts_list(partner_id: int, limit: int = 20) -> list[dict]:
    """История выплат партнёру."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT amount, note, created_at FROM partner_payouts "
            "WHERE partner_id=$1 ORDER BY created_at DESC LIMIT $2",
            partner_id, int(limit))
    return [dict(r) for r in rows]


async def partners_overview() -> dict:
    """Сводка по ВСЕЙ партнёрской программе — для главного экрана раздела."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT
                 (SELECT COUNT(*) FROM users WHERE COALESCE(partner,FALSE)=TRUE) AS partners,
                 (SELECT COUNT(*) FROM users WHERE partner_id IS NOT NULL) AS clients,
                 (SELECT COUNT(*) FROM partner_earnings) AS orders,
                 (SELECT COALESCE(SUM(paid_amount),0) FROM partner_earnings) AS turnover,
                 (SELECT COALESCE(SUM(partner_sum),0) FROM partner_earnings) AS partners_sum,
                 (SELECT COALESCE(SUM(owner_sum),0) FROM partner_earnings) AS owner_sum,
                 (SELECT COALESCE(SUM(paid_amount),0) FROM partner_earnings
                   WHERE created_at >= date_trunc('month', NOW())) AS turnover_month,
                 (SELECT COALESCE(SUM(partner_sum),0) FROM partner_earnings
                   WHERE created_at >= date_trunc('month', NOW())) AS partners_month,
                 (SELECT COALESCE(SUM(amount),0) FROM partner_payouts) AS payouts""")
    return dict(row) if row else {}


async def partner_all_orders(partner_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
    """Все заказы партнёра постранично."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT e.order_id, e.client_id, e.svc_key, e.plan_idx, e.base_price,
                      e.client_price, e.paid_amount, e.partner_sum, e.owner_sum,
                      e.created_at, u.username
               FROM partner_earnings e LEFT JOIN users u ON u.user_id = e.client_id
               WHERE e.partner_id=$1 ORDER BY e.created_at DESC LIMIT $2 OFFSET $3""",
            partner_id, int(limit), int(offset))
    return [dict(r) for r in rows]


async def partner_clients(partner_id: int, limit: int = 200) -> list[dict]:
    """Клиенты партнёра: ник, дата прихода, число покупок и суммы.

    Дату берём из users.created_at — клиент закрепляется за партнёром в момент
    первого /start по его ссылке, так что это и есть дата прихода.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.user_id, c.username, c.full_name, c.created_at,
                      COALESCE(e.cnt, 0)      AS orders,
                      COALESCE(e.paid, 0)     AS paid,
                      COALESCE(e.psum, 0)     AS partner_sum,
                      e.last_at
               FROM users c
               LEFT JOIN (
                   SELECT client_id,
                          COUNT(*)            AS cnt,
                          SUM(paid_amount)    AS paid,
                          SUM(partner_sum)    AS psum,
                          MAX(created_at)     AS last_at
                   FROM partner_earnings WHERE partner_id = $1 GROUP BY client_id
               ) e ON e.client_id = c.user_id
               WHERE c.partner_id = $1
               ORDER BY COALESCE(e.paid, 0) DESC, c.created_at DESC
               LIMIT $2""",
            partner_id, int(limit))
    return [dict(r) for r in rows]


async def partner_client_orders(partner_id: int, client_id: int, limit: int = 20) -> list[dict]:
    """Покупки конкретного клиента партнёра."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT order_id, svc_key, plan_idx, paid_amount, partner_sum, created_at "
            "FROM partner_earnings WHERE partner_id=$1 AND client_id=$2 "
            "ORDER BY created_at DESC LIMIT $3",
            partner_id, client_id, int(limit))
    return [dict(r) for r in rows]


async def get_partner_for_client(client_id: int) -> dict | None:
    """Партнёр, приведший клиента — для пометки в сообщениях о заказе.
    Возвращает {partner_id, username, full_name} либо None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT p.user_id AS partner_id, p.username, p.full_name
               FROM users c JOIN users p ON p.user_id = c.partner_id
               WHERE c.user_id = $1 AND COALESCE(p.partner, FALSE) = TRUE""",
            client_id)
    return dict(row) if row else None


async def partner_recent_orders(partner_id: int, limit: int = 10) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT order_id, svc_key, paid_amount, partner_sum, created_at "
            "FROM partner_earnings WHERE partner_id=$1 ORDER BY created_at DESC LIMIT $2",
            partner_id, int(limit))
    return [dict(r) for r in rows]


async def add_partner_payout(partner_id: int, amount: float, note: str = "") -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO partner_payouts (partner_id, amount, note) VALUES ($1,$2,$3)",
            partner_id, float(amount), note or "")


async def get_ref_premium(user_id: int) -> dict | None:
    """Возвращает {ref_premium, ref_premium_pct} или None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ref_premium, ref_premium_pct FROM users WHERE user_id=$1", user_id
        )
    return dict(row) if row else None


async def list_ref_premium() -> list[dict]:
    """Список премиум-партнёров с их индивид. % и числом рефералов."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT u.user_id, u.username, u.ref_premium_pct,
                      (SELECT COUNT(*) FROM users r WHERE r.referred_by=u.user_id) AS refs
               FROM users u WHERE u.ref_premium=TRUE ORDER BY u.user_id"""
        )
    return [dict(r) for r in rows]


async def premium_ref_earned_this_month(referrer_id: int) -> float:
    """Сумма начисленных премиум-монеток за текущий календарный месяц."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        v = await conn.fetchval(
            """SELECT COALESCE(SUM(coins),0) FROM ref_premium_log
               WHERE referrer_id=$1 AND created_at >= date_trunc('month', NOW())""",
            referrer_id
        )
    return float(v or 0)


async def log_premium_ref(referrer_id: int, referee_id: int, order_id: str,
                          amount_rub: float, coins: float, cap: float = 0.0) -> float:
    """Пишет начисление в лог и возвращает СУММУ, которую реально можно начислить.

    0.0 означает «не начислять» (уже было по этому order_id, либо месячный лимит
    исчерпан). Лимит проверяется ВНУТРИ транзакции с блокировкой строк месяца:
    раньше проверка шла отдельным запросом, и две одновременные оплаты рефералов
    пробивали месячный cap.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                if cap and cap > 0:
                    _earned = await conn.fetchval(
                        """SELECT COALESCE(SUM(coins),0) FROM ref_premium_log
                           WHERE referrer_id=$1 AND created_at >= date_trunc('month', NOW())
                           FOR UPDATE""",
                        referrer_id
                    ) or 0
                    _remaining = float(cap) - float(_earned)
                    if _remaining <= 0:
                        return 0.0
                    if coins > _remaining:
                        coins = round(_remaining, 2)
                await conn.execute(
                    "INSERT INTO ref_premium_log (referrer_id, referee_id, order_id, amount_rub, coins) "
                    "VALUES ($1,$2,$3,$4,$5)",
                    referrer_id, referee_id, order_id, round(amount_rub, 2), round(coins, 2)
                )
                return round(float(coins), 2)
            except Exception:
                return 0.0  # UNIQUE(order_id) — уже начисляли


# ── Perplexity коды (копия Claude) ───────────────────────────────────────────

async def get_next_perplexity_code(plan: str = "pro"):
    """Резервирует и возвращает следующий свободный код нужного плана."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Три очереди: подтверждённые сверкой → непроверенные → помеченные
        # подозрительными. Помеченные выдаются ПОСЛЕДНИМИ, но не блокируются:
        # сверка могла ошибиться, а без кода клиент останется ни с чем.
        for _status_cond in ("COALESCE(check_status,'unchecked') = 'ok'",
                             "COALESCE(check_status,'unchecked') = 'unchecked'",
                             "COALESCE(check_status,'unchecked') NOT IN ('used','invalid')"):
            row = await conn.fetchrow(
                f"""UPDATE perplexity_codes SET is_used=TRUE
                    WHERE id=(SELECT id FROM perplexity_codes
                              WHERE plan=$1 AND is_used=FALSE
                                AND {_status_cond}
                              ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
                    RETURNING code""",
                plan
            )
            if row:
                return row["code"]
    return None


async def release_perplexity_code(code: str):
    """Возвращает код в пул при неудачной активации."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE perplexity_codes SET is_used=FALSE, used_by=NULL, "
            "used_at=NULL, order_id=NULL, org_id=NULL WHERE code=$1",
            code
        )


async def mark_perplexity_code_used(code: str, user_id: int, order_id: str, org_id: str = ""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE perplexity_codes "
            "SET used_by=$1, order_id=$2, org_id=$3, used_at=NOW() WHERE code=$4",
            user_id, order_id, org_id, code
        )


async def save_perplexity_pending_activation(
    user_id: int, code: str, order_id: str, plan: str, plan_name: str
):
    _h = await activation_hours("perplexity")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO perplexity_pending_activations
               (user_id, code, order_id, plan, plan_name, expires_at)
               VALUES ($1,$2,$3,$4,$5, NOW()+make_interval(hours => $6))
               ON CONFLICT (user_id) DO UPDATE
               SET code=$2, order_id=$3, plan=$4, plan_name=$5,
                   org_id='', bpa_order_id=NULL,
                   created_at=NOW(), expires_at=NOW()+make_interval(hours => $6)""",
            user_id, code, order_id, plan, plan_name, _h
        )


async def get_perplexity_pending_activation(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM perplexity_pending_activations "
            "WHERE user_id=$1 AND expires_at > NOW()",
            user_id
        )
    return dict(row) if row else None


async def get_perplexity_pending_activation_by_code(code: str):
    """Фолбэк-идентификация по коду активации (когда initData не прошёл)."""
    if not code:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM perplexity_pending_activations WHERE code=$1 AND expires_at > NOW() "
            "ORDER BY created_at DESC LIMIT 1", code)
    return dict(row) if row else None


async def delete_perplexity_pending_activation(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM perplexity_pending_activations WHERE user_id=$1", user_id
        )


async def count_perplexity_codes_by_plan() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT plan,
                      COUNT(*) FILTER(WHERE NOT is_used)                      AS free,
                      COUNT(*) FILTER(WHERE is_used AND used_by IS NOT NULL)  AS activated,
                      COUNT(*) FILTER(WHERE is_used AND used_by IS NULL)      AS reserved
               FROM perplexity_codes GROUP BY plan ORDER BY plan"""
        )
        total_act = await conn.fetchval(
            "SELECT COUNT(*) FROM perplexity_codes WHERE is_used=TRUE AND used_by IS NOT NULL"
        ) or 0
        last_used = await conn.fetchrow(
            """SELECT code, plan, used_at, used_by
               FROM perplexity_codes WHERE is_used=TRUE AND used_by IS NOT NULL
               ORDER BY used_at DESC LIMIT 1"""
        )
    return {
        "by_plan": {r["plan"]: {"free": r["free"], "activated": r["activated"], "reserved": r["reserved"]} for r in rows},
        "total_activations": total_act,
        "last_used": dict(last_used) if last_used else None,
    }


# ── Link-pay заказы (оплата по ссылке) ───────────────────────────────────────

async def create_linkpay_order(user_id, username, fk_order_id, service_key,
                               service_name, plan_name, amount_rub,
                               kind="linkpay", status="awaiting_link"):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO linkpay_orders
               (user_id, username, fk_order_id, service_key, service_name, plan_name, amount_rub, status, kind)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
               ON CONFLICT (fk_order_id) DO NOTHING""",
            user_id, username or "", fk_order_id, service_key,
            service_name, plan_name, int(amount_rub or 0), status, kind
        )


async def set_linkpay_email(fk_order_id, email):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE linkpay_orders SET account_email=$1, status='awaiting_setup' WHERE fk_order_id=$2",
            email, fk_order_id
        )


async def set_linkpay_creds(fk_order_id, email, password):
    """Сохраняет email и пароль аккаунта клиента (вход в аккаунт)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE linkpay_orders SET account_email=$1, account_pass=$2, status='awaiting_setup' "
            "WHERE fk_order_id=$3",
            email, password, fk_order_id
        )


async def get_fk_order_admin_msg(order_id):
    """ID канонического сообщения заказа админу (fk_orders.admin_msg_id)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT admin_msg_id FROM fk_orders WHERE order_id=$1", order_id)


async def get_order_num(order_id):
    """Человекочитаемый номер заказа (#N). Возвращает int или None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            return await conn.fetchval("SELECT num FROM fk_orders WHERE order_id=$1", order_id)
        except Exception:
            return None


async def get_linkpay_order(fk_order_id):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM linkpay_orders WHERE fk_order_id=$1", fk_order_id)
    return dict(row) if row else None


async def set_linkpay_link(fk_order_id, link):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE linkpay_orders SET payment_link=$1, status='awaiting_payment' WHERE fk_order_id=$2",
            link, fk_order_id
        )


async def set_linkpay_status(fk_order_id, status):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE linkpay_orders SET status=$1 WHERE fk_order_id=$2", status, fk_order_id
        )
        # Синхронизация с fk_orders: отменённый заказ должен уйти из прибыли/статистики
        # (они читают fk_orders WHERE status='paid'). Иначе отмена в чате не отражалась
        # в админ-панели. order_id в fk_orders == fk_order_id.
        if status == "cancelled":
            await conn.execute(
                "UPDATE fk_orders SET status='cancelled' WHERE order_id=$1", fk_order_id)
        # Заказ закрыт — пароль клиента от его аккаунта больше не нужен.
        # Держать чужие пароли в БД бессрочно нельзя: при утечке дампа это прямой
        # ущерб клиенту. Email оставляем — он нужен для идентификации подписки.
        if status in ("done", "cancelled"):
            try:
                await conn.execute(
                    "UPDATE linkpay_orders SET account_pass='' "
                    "WHERE fk_order_id=$1 AND account_pass <> ''", fk_order_id)
            except Exception as _e_pw:
                logging.warning(f"clear account_pass {fk_order_id}: {_e_pw}")


async def set_linkpay_admin_msg(fk_order_id, admin_msg_id):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE linkpay_orders SET admin_msg_id=$1 WHERE fk_order_id=$2",
            admin_msg_id, fk_order_id
        )
        # Ведём ЦЕПОЧКУ всех админских сообщений заказа, чтобы при «Выполнен»/«Отменён»
        # пометить их ВСЕ (а не только последнее).
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS linkpay_admin_msgs (
                    fk_order_id TEXT NOT NULL,
                    msg_id      BIGINT NOT NULL,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (fk_order_id, msg_id)
                )""")
            await conn.execute(
                "INSERT INTO linkpay_admin_msgs (fk_order_id, msg_id) VALUES ($1,$2) "
                "ON CONFLICT DO NOTHING", fk_order_id, admin_msg_id)
        except Exception:
            pass


async def get_linkpay_admin_msgs(fk_order_id):
    """Все id админских сообщений по заказу (для массовой пометки статуса)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                "SELECT msg_id FROM linkpay_admin_msgs WHERE fk_order_id=$1 ORDER BY msg_id",
                fk_order_id)
            return [r["msg_id"] for r in rows]
        except Exception:
            return []


async def list_linkpay_pending(limit=30):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM linkpay_orders "
            "WHERE status IN ('awaiting_link','awaiting_payment','awaiting_creds','awaiting_setup') "
            "ORDER BY created_at DESC LIMIT $1", limit
        )
    return [dict(r) for r in rows]
