# ══════════════════════════════════════════════════════════════════════════════
#  CLAUDE MINI APP — вставить в bot.py ПЕРЕД функцией async def main()
#  Файл: claude_activation_backend.py  →  скопировать содержимое в bot.py
# ══════════════════════════════════════════════════════════════════════════════

# ─── Путь к HTML и флаг включения ────────────────────────────────────────────
_CLAUDE_WEBAPP_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "claude_webapp.html"
)
CLAUDE_WEBAPP_ENABLED = os.getenv("CLAUDE_WEBAPP_ENABLED", "1") == "1"

# In-memory статусы активаций: {bpa_order_id: {"status": ..., ...}}
_claude_job_results: dict = {}


# ─── Хелперы БД ──────────────────────────────────────────────────────────────

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
                      COUNT(*) FILTER(WHERE NOT is_used)                     AS free,
                      COUNT(*) FILTER(WHERE is_used AND used_by IS NOT NULL) AS activated
               FROM claude_codes GROUP BY plan ORDER BY plan"""
        )
    return {r["plan"]: {"free": r["free"], "activated": r["activated"]} for r in rows}


# ─── Отправить WebApp клиенту ─────────────────────────────────────────────────

async def _send_claude_webapp_to_user(
    user_id: int, code: str, order_id: str,
    plan: str, plan_name: str, delayed_note: str = ""
) -> bool:
    """Сохраняет pending и отправляет клиенту кнопку WebApp."""
    import urllib.parse as _up
    from aiogram.types import WebAppInfo as _WAI

    await save_claude_pending_activation(user_id, code, order_id, plan, plan_name)

    webapp_url = (
        f"{WEBAPP_BASE_URL}/webapp/claude"
        f"?plan={_up.quote(plan_name)}&code={_up.quote(code)}"
    )
    try:
        await bot.send_message(
            user_id,
            f"🎉 <b>Оплата прошла!</b>\n\n"
            f"📦 <b>Claude {plan_name}</b>\n\n"
            f"Осталось активировать подписку — нажми кнопку ниже, "
            f"введи Organization ID из настроек Claude, и подписка "
            f"активируется автоматически за 1–2 минуты 👇"
            f"{delayed_note}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="⚡ Активировать Claude",
                    web_app=_WAI(url=webapp_url)
                )],
                [InlineKeyboardButton(
                    text="❓ Нужна помощь",
                    url=f"https://t.me/{PERSONAL_USERNAME}"
                )],
            ])
        )
        await log_event(user_id, "claude_webapp_sent",
                        f"code={code} plan={plan_name} order={order_id}")
        return True
    except Exception as _e:
        logging.error(f"_send_claude_webapp_to_user uid={user_id}: {_e}")
        return False


# ─── Фоновый polling job ──────────────────────────────────────────────────────

async def _claude_activation_polling_job(
    bpa_order_id: int, code: str, user_id: int,
    order_id: str, plan_name: str, org_id: str
):
    """
    Опрашивает bypriceactivate.pro каждые 5 сек.
    При done — помечает код, уведомляет тебя и клиента.
    При failed — возвращает код, пишет обоим.
    """
    _claude_job_results[bpa_order_id] = {"status": "queued"}
    logging.info(f"Claude polling bpa={bpa_order_id} user={user_id} code={code}")

    for attempt in range(120):          # макс 10 минут (120 × 5 сек)
        await asyncio.sleep(5)
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as _s:
                async with _s.get(
                    f"https://bypriceactivate.pro/api/activate/{bpa_order_id}"
                ) as _r:
                    if _r.status != 200:
                        continue
                    _d = await _r.json()

            status = _d.get("status")
            _claude_job_results[bpa_order_id] = {"status": status}

            # ── Успех ──────────────────────────────────────────
            if status == "done":
                await mark_claude_code_used(code, user_id, order_id, org_id)
                await delete_claude_pending_activation(user_id)
                _claude_job_results[bpa_order_id] = {"status": "done", "success": True}

                import datetime as _dt2
                _ts = _dt2.datetime.now(_BOT_TZ).strftime("%d.%m.%Y %H:%M")
                try:
                    _pool2 = await get_pool()
                    async with _pool2.acquire() as _c2:
                        _ur = await _c2.fetchrow(
                            "SELECT username, full_name FROM users WHERE user_id=$1",
                            user_id
                        )
                    _un = (_ur["username"] if _ur else "") or ""
                    _fn = (_ur["full_name"] if _ur else "") or ""
                except Exception:
                    _un = _fn = ""
                _tg = (f"@{_un}" if _un else _fn) or f"id{user_id}"

                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"✅ <b>Claude авто-активация OK</b>\n\n"
                        f"👤 <b>{_tg}</b>  (<code>{user_id}</code>)\n"
                        f"🔑 Код: <code>{code}</code>\n"
                        f"📦 Тариф: <b>{plan_name}</b>\n"
                        f"🆔 Org ID: <code>{org_id}</code>\n"
                        f"🔢 BPA: <code>{bpa_order_id}</code>\n"
                        f"⏱ {_ts}",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                await log_event(user_id, "claude_activation_ok",
                                f"code={code} bpa={bpa_order_id} plan={plan_name}")
                return

            # ── Ошибка ─────────────────────────────────────────
            elif status == "failed":
                _err = _d.get("error") or "Ошибка активации"
                _claude_job_results[bpa_order_id] = {
                    "status": "done", "success": False, "error": _err
                }
                if "out of stock" in _err.lower():
                    await release_claude_code(code)
                await delete_claude_pending_activation(user_id)
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"❌ <b>Claude FAILED</b>\n"
                        f"👤 <code>{user_id}</code>  📦 {plan_name}\n"
                        f"🔑 <code>{code}</code>  🔢 BPA: <code>{bpa_order_id}</code>\n"
                        f"❌ {_err[:300]}",
                        parse_mode="HTML"
                    )
                    await bot.send_message(
                        user_id,
                        f"😔 <b>Активация Claude не прошла</b>\n\n"
                        f"{_err[:300]}\n\n"
                        f"Напиши Александру — активирую вручную за 15–30 мин!",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(
                                text="💬 Написать Александру",
                                url=f"https://t.me/{PERSONAL_USERNAME}"
                            )],
                        ])
                    )
                except Exception:
                    pass
                await log_event(user_id, "claude_activation_fail",
                                f"code={code} bpa={bpa_order_id} err={_err[:150]}")
                return

        except Exception as _e:
            logging.error(f"Claude poll #{attempt} bpa={bpa_order_id}: {_e}")

    # ── Таймаут ────────────────────────────────────────────────
    _claude_job_results[bpa_order_id] = {
        "status": "done", "success": False,
        "error": "Активация затянулась. Напиши Александру — он поможет!"
    }
    try:
        await bot.send_message(
            ADMIN_ID,
            f"⏰ <b>Claude TIMEOUT</b>\n"
            f"👤 <code>{user_id}</code>  🔢 BPA: <code>{bpa_order_id}</code>\n"
            f"10 минут — проверь вручную на bypriceactivate.pro",
            parse_mode="HTML"
        )
    except Exception:
        pass


# ─── Веб-хэндлеры ────────────────────────────────────────────────────────────

async def webapp_claude_handler(request: web.Request) -> web.Response:
    """GET /webapp/claude — отдаёт claude_webapp.html"""
    try:
        with open(_CLAUDE_WEBAPP_HTML_PATH, "r", encoding="utf-8") as _f:
            return web.Response(text=_f.read(), content_type="text/html", charset="utf-8")
    except FileNotFoundError:
        return web.Response(text="Claude Mini App not found", status=404)


async def api_activate_claude_handler(request: web.Request) -> web.Response:
    """POST /api/activate-claude"""
    import json as _j, re as _re

    def _resp(data, status=200):
        return web.Response(
            text=_j.dumps(data, ensure_ascii=False),
            content_type="application/json", status=status
        )

    if not CLAUDE_WEBAPP_ENABLED:
        return _resp({"error": f"Временно недоступно. Напиши @{PERSONAL_USERNAME}"}, 503)

    try:
        body = await request.json()
    except Exception:
        return _resp({"error": "Неверный формат запроса"}, 400)

    org_id    = (body.get("org_id") or "").strip().lower()
    init_data = (body.get("init_data") or "").strip()

    user_id = _verify_tg_init_data(init_data)
    if not user_id:
        return _resp({"error": "Ошибка авторизации. Перезапусти мини-приложение."}, 403)

    if not _re.match(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', org_id
    ):
        return _resp({"error": "Неверный формат Organization ID. Должен быть UUID."})

    pending = await get_claude_pending_activation(user_id)
    if not pending:
        return _resp({
            "error": f"Время сессии истекло. Напиши @{PERSONAL_USERNAME} для нового кода."
        })

    code      = pending["code"]
    order_id  = pending["order_id"]
    plan_name = pending["plan_name"]

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as _s:
            async with _s.post(
                "https://bypriceactivate.pro/api/activate",
                json={"code": code, "org_id": org_id},
                headers={"Content-Type": "application/json"},
            ) as _r:
                try:
                    _rd = await _r.json()
                except Exception:
                    _rd = {}

                if _r.status == 201:
                    bpa_order_id = _rd.get("order_id")
                    if not bpa_order_id:
                        return _resp({"error": "Сервис не вернул order_id."})

                    _pool3 = await get_pool()
                    async with _pool3.acquire() as _c3:
                        await _c3.execute(
                            "UPDATE claude_pending_activations "
                            "SET org_id=$1, bpa_order_id=$2 WHERE user_id=$3",
                            org_id, bpa_order_id, user_id
                        )
                    asyncio.create_task(_claude_activation_polling_job(
                        bpa_order_id, code, user_id, order_id, plan_name, org_id
                    ))
                    logging.info(
                        f"Claude activation: bpa={bpa_order_id} user={user_id} code={code}"
                    )
                    return _resp({"order_id": bpa_order_id, "status": "queued"})

                elif _r.status == 409:
                    _detail = _rd.get("detail", "")
                    if "already claimed" in _detail:
                        _msg = "Код уже активирован. Напиши Александру."
                    elif "out of stock" in _detail:
                        _msg = "Временно нет активаций. Александр активирует вручную."
                        try:
                            await bot.send_message(
                                ADMIN_ID,
                                f"🚨 <b>Claude — нет стока!</b>\n"
                                f"👤 <code>{user_id}</code> ({plan_name})\n"
                                f"Пополни коды на bypriceactivate.pro",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                    else:
                        _msg = _detail or "Ошибка кода."
                    return _resp({"error": _msg})

                elif _r.status == 404:
                    return _resp({"error": "Код не найден. Напиши Александру."})

                else:
                    logging.error(f"bypriceactivate.pro HTTP {_r.status}: {str(_rd)[:200]}")
                    return _resp({"error": f"Ошибка ({_r.status}). Попробуй ещё раз."})

    except aiohttp.ClientError as _e:
        logging.error(f"Claude activate network: {_e}")
        return _resp({"error": "Нет связи с сервисом. Попробуй ещё раз."})
    except Exception as _e:
        logging.error(f"Claude activate error: {_e}", exc_info=True)
        return _resp({"error": "Внутренняя ошибка. Напиши Александру."})


async def api_activate_claude_status_handler(request: web.Request) -> web.Response:
    """GET /api/activate-claude-status/{order_id}"""
    import json as _j2
    try:
        bpa_order_id = int(request.match_info.get("order_id", ""))
    except ValueError:
        return web.Response(
            text=_j2.dumps({"status": "not_found"}),
            content_type="application/json", status=400
        )
    result = _claude_job_results.get(bpa_order_id)
    if result is None:
        return web.Response(
            text=_j2.dumps({"status": "not_found"}),
            content_type="application/json", status=404
        )
    return web.Response(
        text=_j2.dumps(result, ensure_ascii=False),
        content_type="application/json"
    )


# ─── Callback: клиент переоткрывает WebApp сам ───────────────────────────────

@dp.callback_query(F.data == "claude_reopen_webapp")
async def claude_reopen_webapp(cb: CallbackQuery):
    """Клиент нажал кнопку повторно — переотправляем WebApp если есть pending."""
    uid = cb.from_user.id
    pending = await get_claude_pending_activation(uid)
    if not pending:
        await cb.answer(
            "⚠️ Сессия истекла. Напиши Александру для нового кода.",
            show_alert=True
        )
        return
    import urllib.parse as _up3
    from aiogram.types import WebAppInfo as _WAI3
    webapp_url = (
        f"{WEBAPP_BASE_URL}/webapp/claude"
        f"?plan={_up3.quote(pending['plan_name'])}"
        f"&code={_up3.quote(pending['code'])}"
    )
    await cb.message.answer(
        f"⚡ <b>Активация Claude {pending['plan_name']}</b>\n\n"
        f"Нажми кнопку, введи Organization ID из настроек Claude — "
        f"подписка активируется автоматически за 1–2 минуты.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"⚡ Активировать Claude {pending['plan_name']}",
                web_app=_WAI3(url=webapp_url)
            )],
            [InlineKeyboardButton(
                text="❓ Нужна помощь",
                url=f"https://t.me/{PERSONAL_USERNAME}"
            )],
        ])
    )
    await cb.answer()


# ─── Администрирование Claude Mini App ───────────────────────────────────────

class ClaudeAdminState(StatesGroup):
    waiting_codes = State()
    waiting_plan  = State()


@dp.callback_query(F.data == "adm_claude_webapp")
async def adm_claude_webapp_menu(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True)
        return
    stats = await count_claude_codes_by_plan()
    LABELS = {"pro": "Claude Pro", "max_5x": "Claude Max 5×", "max_20x": "Claude Max 20×"}
    codes_text = ""
    for key, label in LABELS.items():
        s = stats.get(key, {"free": 0, "activated": 0})
        icon = "✅" if s["free"] > 2 else ("⚠️" if s["free"] > 0 else "🚨")
        codes_text += f"\n{icon} <b>{label}</b>: {s['free']} своб / {s['activated']} активировано"
    status_str = "✅ ВКЛЮЧЁН" if CLAUDE_WEBAPP_ENABLED else "🔴 ВЫКЛЮЧЕН"
    text = (
        f"⚡ <b>Claude Mini App</b>\n\n"
        f"Статус: <b>{status_str}</b>\n\n"
        f"<b>Коды:</b>{codes_text or chr(10) + '📭 Кодов нет'}\n\n"
        f"<i>Коды покупай на bypriceactivate.pro</i>"
    )
    toggle_txt = "🔴 Выключить" if CLAUDE_WEBAPP_ENABLED else "✅ Включить"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_txt, callback_data="adm_claude_toggle")],
        [InlineKeyboardButton(text="➕ Pro",    callback_data="adm_claude_add:pro"),
         InlineKeyboardButton(text="➕ Max 5×", callback_data="adm_claude_add:max_5x"),
         InlineKeyboardButton(text="➕ Max 20×",callback_data="adm_claude_add:max_20x")],
        [InlineKeyboardButton(text="📤 Отправить WebApp юзеру", callback_data="adm_claude_send")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_admin")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "adm_claude_toggle")
async def adm_claude_toggle(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return
    global CLAUDE_WEBAPP_ENABLED
    CLAUDE_WEBAPP_ENABLED = not CLAUDE_WEBAPP_ENABLED
    await cb.answer(
        f"Claude Mini App {'включён ✅' if CLAUDE_WEBAPP_ENABLED else 'выключен 🔴'}",
        show_alert=True
    )
    await adm_claude_webapp_menu(cb)


@dp.callback_query(F.data.startswith("adm_claude_add:"))
async def adm_claude_add_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return
    plan = cb.data.split(":")[1]
    LABELS2 = {"pro": "Pro", "max_5x": "Max 5×", "max_20x": "Max 20×"}
    await state.set_state(ClaudeAdminState.waiting_codes)
    await state.update_data(claude_plan=plan)
    await cb.message.answer(
        f"📥 <b>Коды Claude {LABELS2.get(plan, plan)}</b>\n\n"
        f"Отправь коды — каждый с новой строки.\n"
        f"Пример: <code>XXXX-XXXX-XXXX-XXXX</code>\n\n/cancel — отмена",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(ClaudeAdminState.waiting_codes, StateFilter("*"))
async def adm_claude_receive_codes(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    data = await state.get_data()
    plan = data.get("claude_plan", "pro")
    codes = [l.strip() for l in (message.text or "").splitlines() if l.strip()]
    added = 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        for code in codes:
            try:
                await conn.execute(
                    "INSERT INTO claude_codes (code, plan) VALUES ($1,$2) "
                    "ON CONFLICT (code) DO NOTHING",
                    code, plan
                )
                added += 1
            except Exception:
                pass
    await state.clear()
    LABELS3 = {"pro": "Pro", "max_5x": "Max 5×", "max_20x": "Max 20×"}
    await message.answer(
        f"✅ Добавлено <b>{added}</b> кодов Claude {LABELS3.get(plan, plan)}",
        parse_mode="HTML"
    )
    await log_event(message.from_user.id, "claude_codes_added", f"plan={plan} n={added}")


@dp.callback_query(F.data == "adm_claude_send")
async def adm_claude_send_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return
    await state.set_state(ClaudeAdminState.waiting_plan)
    await cb.message.answer(
        "📤 <b>Отправить Claude WebApp юзеру</b>\n\n"
        "Формат: <code>USER_ID ПЛАН</code>\n"
        "Планы: <code>pro</code> / <code>max_5x</code> / <code>max_20x</code>\n"
        "Пример: <code>123456789 pro</code>\n\n/cancel — отмена",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(ClaudeAdminState.waiting_plan, StateFilter("*"))
async def adm_claude_do_send(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    parts = (message.text or "").strip().split()
    LABELS4 = {"pro": "Pro", "max_5x": "Max 5×", "max_20x": "Max 20×"}
    if len(parts) < 2 or parts[1] not in LABELS4:
        await message.answer(f"Формат: USER_ID ПЛАН\nПланы: {', '.join(LABELS4)}")
        return
    try:
        target = int(parts[0])
    except ValueError:
        await message.answer("Неверный USER_ID")
        return
    plan = parts[1]
    plan_name = LABELS4[plan]
    code = await get_next_claude_code(plan)
    if not code:
        await message.answer(f"🚨 Нет кодов для {plan_name}! Добавь через меню.")
        await state.clear()
        return
    import time as _t2
    order_id = f"claude_{target}_{int(_t2.time())}"
    ok = await _send_claude_webapp_to_user(target, code, order_id, plan, plan_name)
    if ok:
        await message.answer(
            f"✅ WebApp отправлен <code>{target}</code>\n"
            f"Код: <code>{code}</code>  Тариф: {plan_name}",
            parse_mode="HTML"
        )
    else:
        await message.answer("❌ Не удалось отправить — проверь user_id")
        await release_claude_code(code)
        await delete_claude_pending_activation(target)
    await state.clear()


# ─── Тест-команда (только для тебя) ──────────────────────────────────────────

@dp.message(F.text.startswith("/test_claude_webapp"), StateFilter("*"))
async def test_claude_webapp(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    import urllib.parse as _up4, time as _t3
    from aiogram.types import WebAppInfo as _WAI4
    code = await get_next_claude_code("pro")
    if not code:
        await message.answer("🚨 Нет свободных кодов Pro для теста.")
        return
    order_id = f"claude_test_{message.from_user.id}_{int(_t3.time())}"
    await save_claude_pending_activation(message.from_user.id, code, order_id, "pro", "Pro")
    webapp_url = (
        f"{WEBAPP_BASE_URL}/webapp/claude"
        f"?plan={_up4.quote('Pro')}&code={_up4.quote(code)}"
    )
    await message.answer(
        f"🧪 <b>Тест Claude WebApp</b>\nКод: <code>{code}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="⚡ Открыть Claude WebApp",
                web_app=_WAI4(url=webapp_url)
            )],
        ])
    )
