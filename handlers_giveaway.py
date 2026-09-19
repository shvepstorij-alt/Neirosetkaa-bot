# -*- coding: utf-8 -*-
# Розыгрыши в канале: сбор комментариев и статус участия.
#
# Три условия розыгрыша проверяются по-разному, и это стоит держать в голове:
#   подписка на канал — запросом к Telegram, в момент проверки (не хранится:
#                       иначе «отписался перед итогами» прошёл бы незамеченным);
#   приглашённые      — из users.referred_by, бот и так пишет, кто кого привёл;
#   комментарий       — вот его хранить негде, поэтому ловим здесь и пишем в базу.
#
# ВАЖНО про комментарии: они живут не в канале, а в привязанной к нему группе
# обсуждения, и бот видит только те, что отправлены ПОСЛЕ его вступления в
# группу. Добавлять бота нужно ДО публикации поста — задним числом никак.
import asyncio, logging, re

from aiogram import F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import StateFilter

from config import ADMIN_ID, CHANNEL_ID, bot, dp, is_admin
from db import (
    ensure_user, get_pool,
    giveaway_active, giveaway_add_comment,
)

# id группы обсуждения узнаём у самого Telegram и держим в памяти: он не
# меняется, а лишний запрос на каждое сообщение группы ни к чему.
_LINKED_CHAT_ID = None
_LINKED_CHECKED_AT = 0.0


async def giveaway_discussion_chat_id(force: bool = False) -> int:
    """id группы обсуждения канала (0 — не привязана или бот её не видит)."""
    global _LINKED_CHAT_ID, _LINKED_CHECKED_AT
    import time as _t
    if _LINKED_CHAT_ID is not None and not force and _t.time() - _LINKED_CHECKED_AT < 3600:
        return _LINKED_CHAT_ID or 0
    try:
        _chat = await bot.get_chat(CHANNEL_ID)
        _LINKED_CHAT_ID = int(getattr(_chat, "linked_chat_id", 0) or 0)
    except Exception as _e:
        logging.warning(f"giveaway: не смог узнать группу обсуждения: {_e}")
        _LINKED_CHAT_ID = 0
    _LINKED_CHECKED_AT = _t.time()
    return _LINKED_CHAT_ID or 0


_GW_CACHE = {"at": 0.0, "gw": None}


async def _gw_active_cached(ttl: float = 20.0) -> dict:
    """Активный розыгрыш с коротким кэшем.

    Хендлер срабатывает на КАЖДОЕ сообщение в группе обсуждения, а розыгрыш
    меняется раз в неделю — ходить за ним в базу каждый раз незачем.
    Двадцати секунд хватает, чтобы изменения из панели подхватились быстро.
    """
    import time as _t
    _now = _t.time()
    if _now - _GW_CACHE["at"] > ttl:
        _GW_CACHE["gw"] = await giveaway_active()
        _GW_CACHE["at"] = _now
    return _GW_CACHE["gw"] or {}


def _norm(text: str) -> str:
    """Приводим текст к виду, в котором ищем ключевое слово.

    Люди пишут «Участвую!», «участвую 🔥», «Участвую.» — всё это должно
    засчитываться, поэтому оставляем только буквы и пробелы.
    """
    _t = (text or "").lower().replace("ё", "е")
    _t = re.sub(r"[^a-zа-я0-9\s]+", " ", _t)
    return " ".join(_t.split())


_SUB_OK = ("member", "administrator", "creator", "owner")


async def giveaway_is_subscribed(user_id: int):
    """Подписан ли человек на канал ПРЯМО СЕЙЧАС.

    Три исхода, и это важно: True — подписан, False — точно не подписан,
    None — СПРОСИТЬ НЕ УДАЛОСЬ. Раньше здесь было только True/False, и любой
    сбой (лимит Telegram, моргнувшая сеть) превращал честного подписчика в
    «не подписан» — то есть человек молча терял приз, а в отчёте это выглядело
    как невыполненное условие. Теперь неизвестность так и остаётся
    неизвестностью, а розыгрыш по непроверенным участникам не запускается.

    Не кэшируем: смысл проверки в том, чтобы поймать отписавшихся.
    """
    for _try in range(3):
        try:
            _m = await bot.get_chat_member(CHANNEL_ID, user_id)
        except Exception as _e:
            _wait = getattr(_e, "retry_after", 0)
            if _wait:                       # упёрлись в лимит — обождём
                await asyncio.sleep(min(int(_wait) + 1, 30))
                continue
            _txt = str(_e).lower()
            # «Пользователь не найден» — это ответ, а не сбой: не подписан.
            if ("not found" in _txt or "user_id_invalid" in _txt
                    or "participant" in _txt):
                return False
            if _try < 2:
                await asyncio.sleep(1 + _try)
                continue
            logging.warning(f"giveaway: не смог проверить подписку {user_id}: {_e}")
            return None
        _st = getattr(_m, "status", "")
        # В разных версиях aiogram статус — то строка, то элемент перечисления;
        # берём хвост после точки и сравниваем в нижнем регистре.
        _st = str(getattr(_st, "value", _st)).split(".")[-1].lower()
        if _st in _SUB_OK:
            return True
        if _st == "restricted":
            # Ограничен в правах, но из канала не вышел — подписка есть.
            return bool(getattr(_m, "is_member", False))
        return False
    return None


async def giveaway_refs_split(user_id: int, since, limit: int = 20) -> tuple:
    """(засчитанные, пришли_но_не_подписаны, было_неясно).

    Правило зачёта теперь ОДНО и для экрана участника, и для пересчёта
    в админке: друг идёт в зачёт, только если подписан на канал СЕЙЧАС.
    Раньше экран считал всех, кто пришёл по ссылке: человек видел «2 из 2»,
    а в панели у него был 0 — и на итогах это выглядело как обман.
    """
    from db import giveaway_refs
    _refs = await giveaway_refs(user_id, since)
    _ok, _no, _murky = [], [], False
    for _r in _refs[:limit]:
        _st = await giveaway_is_subscribed(int(_r["user_id"]))
        if _st is True:
            _ok.append(_r)
        elif _st is False:
            _no.append(_r)
        else:
            _murky = True
    return _ok, _no, _murky


@dp.message(F.chat.type.in_({"group", "supergroup"}), StateFilter("*"))
async def giveaway_catch_comment(message: Message):
    """Ловит комментарии под постами канала — только в группе обсуждения."""
    _u = message.from_user
    if not _u or _u.is_bot:
        return                       # автопересылка поста и служебные сообщения
    _link = await giveaway_discussion_chat_id()
    if not _link or int(message.chat.id) != _link:
        return                       # чужая группа — не наше дело
    _gw = await _gw_active_cached()
    if not _gw:
        return
    _kw = _norm(_gw.get("keyword") or "участвую")
    if not _kw or _kw not in _norm(message.text or message.caption or ""):
        return
    # Человек может писать комментарии, ни разу не открыв бота. Заводим его
    # сразу: иначе в списке участников будет «id без имени», и связать его
    # с приглашениями будет нечем.
    try:
        await ensure_user(_u.id, _u.username or "", _u.full_name or "")
    except Exception:
        pass
    _first = await giveaway_add_comment(
        _gw["id"], _u.id, _u.username or "", _u.full_name or "",
        message.chat.id, message.message_id,
        getattr(message, "message_thread_id", 0) or 0)
    if _first:
        logging.info(f"giveaway: комментарий засчитан uid={_u.id} @{_u.username or '-'}")


async def giveaway_status_text(user) -> tuple:
    """Текст «Моё участие» и кнопки к нему. Отдельно от хендлера — чтобы и
    команда, и кнопка «Проверить ещё раз» шли одним путём.

    Раньше кнопка подделывала объект Message через model_copy: работало, но
    держалось на внутреннем устройстве aiogram и падало бы там, где сообщение
    боту недоступно.
    """
    _gw = await giveaway_active()
    if not _gw:
        return "Сейчас розыгрыш не идёт. Следи за каналом 🙂", None
    await ensure_user(user.id, user.username or "", user.full_name or "")

    _sub = await giveaway_is_subscribed(user.id)
    _nr = _gw.get("need_refs")
    _need = 2 if _nr is None else int(_nr)
    _refs, _no_sub, _murky_ref = await giveaway_refs_split(user.id, _gw["starts_at"])
    pool = await get_pool()
    async with pool.acquire() as conn:
        _commented = await conn.fetchval(
            "SELECT 1 FROM giveaway_comments WHERE giveaway_id=$1 AND user_id=$2",
            _gw["id"], user.id)

    _me = await bot.get_me()
    _link = f"https://t.me/{_me.username}?start=ref_{user.id}"

    def _mark(ok):
        # None — про подписку спросить не удалось; не врём, что не выполнено.
        return "✅" if ok else ("❓" if ok is None else "⬜️")

    _ok_refs = len(_refs) >= _need
    _all = (_sub is True) and _ok_refs and bool(_commented)
    _t = (f"🎁 <b>{_gw.get('title') or 'Розыгрыш'}</b>\n\n"
          f"{_mark(_sub)} Подписка на канал"
          + (" <i>(не смог проверить, попробуй ещё раз)</i>" if _sub is None else "")
          + f"\n{_mark(_ok_refs)} Приглашено друзей: <b>{len(_refs)}</b> из {_need}\n"
          f"{_mark(bool(_commented))} Комментарий под постом\n\n")
    if _refs:
        _t += ("👥 <b>Засчитаны:</b>\n" + "\n".join(
            f"• {('@' + r['username']) if r.get('username') else (r.get('full_name') or 'без имени')}"
            for r in _refs[:10]) + "\n\n")
    # Главная причина жалоб «друг пришёл, а не засчиталось»: пришёл, но
    # на канал не подписался. Говорим прямо и сразу, кого подтолкнуть.
    if _no_sub:
        _t += ("⏳ <b>Пришли по ссылке, но не подписаны на канал:</b>\n" + "\n".join(
            f"• {('@' + r['username']) if r.get('username') else (r.get('full_name') or 'без имени')}"
            for r in _no_sub[:10])
            + "\n<i>Попроси их подписаться — тогда засчитается.</i>\n\n")
    if _murky_ref:
        _t += ("❓ <i>Про кого-то из друзей Telegram не ответил — нажми "
               "«Проверить ещё раз» через минуту.</i>\n\n")
    _t += (f"🔗 <b>Твоя ссылка:</b>\n<code>{_link}</code>\n"
           f"<i>Друг должен открыть её и запустить бота — и подписаться на канал.\nЗасчитываются только те, кто раньше бота не запускал.</i>\n\n")
    _t += ("🎉 <b>Все условия выполнены — ты в списке!</b>"
           if _all else "Осталось закрыть пункты выше 👆")

    _rows = []
    _post = (_gw.get("post_url") or "").strip()
    if _post.lower().startswith(("http://", "https://")):
        _rows.append([InlineKeyboardButton(text="📢 Открыть пост", url=_post)])
    _rows.append([InlineKeyboardButton(text="🔄 Проверить ещё раз",
                                       callback_data="gw_recheck")])
    # Выхода с экрана не было вовсе: человек заходил сюда из профиля и
    # оставался в тупике — только свернуть чат или искать меню заново.
    _rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_profile")])
    return _t, InlineKeyboardMarkup(inline_keyboard=_rows)


@dp.message(F.text.regexp(r"^/giveaway(@\S+)?$"), StateFilter("*"))
async def giveaway_my_status(message: Message):
    """«Моё участие» — показывает, что уже выполнено, а что нет."""
    _t, _kb = await giveaway_status_text(message.from_user)
    await message.answer(_t, parse_mode="HTML", disable_web_page_preview=True,
                         reply_markup=_kb)


@dp.callback_query(F.data == "gw_recheck")
async def giveaway_recheck(cb):
    try:
        await cb.answer("Проверяю…")
    except Exception:
        pass
    try:
        _t, _kb = await giveaway_status_text(cb.from_user)
    except Exception as _e:
        logging.warning(f"giveaway recheck: {_e}")
        return
    # Правим ТО ЖЕ сообщение. Новое шлём только если правка невозможна
    # по-настоящему (сообщение удалено, слишком старое).
    try:
        await cb.message.edit_text(_t, parse_mode="HTML",
                                   disable_web_page_preview=True,
                                   reply_markup=_kb)
        return
    except Exception as _e_ed:
        # «message is not modified» — это НЕ ошибка: с прошлой проверки ничего
        # не изменилось, и на экране уже ровно то, что нужно. Прежний код
        # считал это сбоем и слал новое сообщение — поэтому каждое нажатие
        # «Проверить ещё раз» добавляло в чат копию экрана.
        if "not modified" in str(_e_ed).lower():
            return
        logging.info(f"giveaway recheck: правка не вышла ({_e_ed}) — шлю новое")
    for _send in (
        lambda: cb.message.answer(_t, parse_mode="HTML",
                                  disable_web_page_preview=True,
                                  reply_markup=_kb),
        lambda: bot.send_message(cb.from_user.id, _t, parse_mode="HTML",
                                 disable_web_page_preview=True,
                                 reply_markup=_kb),
    ):
        try:
            await _send()
            return
        except Exception:
            continue


@dp.message(F.text.startswith("/gw_why"), StateFilter("*"))
async def giveaway_why(message: Message):
    """/gw_why <id> — почему у этого человека такие галочки.

    Ответ на жалобу «друг пришёл, а не засчиталось» должен быть за одну
    команду, а не за поход в базу. Показывает три условия и КАЖДОГО
    приглашённого с причиной, почему он в зачёте или нет.
    """
    if not is_admin(message.from_user.id):
        return
    _parts = (message.text or "").split()
    if len(_parts) < 2 or not _parts[1].lstrip("-").isdigit():
        await message.answer("Формат: <code>/gw_why 862690780</code>", parse_mode="HTML")
        return
    _uid = int(_parts[1])

    _gw = await giveaway_active()
    if not _gw:
        await message.answer("Сейчас нет активного розыгрыша.")
        return
    _need = 2 if _gw.get("need_refs") is None else int(_gw["need_refs"])
    _since = _gw["starts_at"]

    pool = await get_pool()
    async with pool.acquire() as conn:
        _me_row = await conn.fetchrow(
            "SELECT username, full_name, created_at, started_at, referred_by, partner_id "
            "FROM users WHERE user_id=$1", _uid)
        _commented = await conn.fetchval(
            "SELECT created_at FROM giveaway_comments WHERE giveaway_id=$1 AND user_id=$2",
            _gw["id"], _uid)
        # ВСЕ приглашённые, без фильтра по дате: иначе не видно тех,
        # кто пришёл ДО старта розыгрыша — а это частая причина спора.
        _all_refs = await conn.fetch(
            "SELECT user_id, username, full_name, created_at FROM users "
            "WHERE referred_by=$1 ORDER BY created_at", _uid)

    if not _me_row:
        await message.answer(f"⚠️ <code>{_uid}</code> вообще нет в базе бота.",
                             parse_mode="HTML")
        return

    import datetime as _dt_w
    _since_n = _since
    if getattr(_since_n, "tzinfo", None) is not None:
        _since_n = _since_n.astimezone(_dt_w.timezone.utc).replace(tzinfo=None)

    _sub = await giveaway_is_subscribed(_uid)
    _mk = lambda v: "✅" if v is True else ("❓" if v is None else "❌")

    _who = ("@" + (_me_row["username"] or "")) if _me_row["username"] else (
        _me_row["full_name"] or f"id{_uid}")
    _t = [f"🔎 <b>{_who}</b> — <code>{_uid}</code>",
          f"🎁 Розыгрыш: <b>{_gw.get('title') or '—'}</b> (старт {_since_n:%d.%m %H:%M} UTC)",
          "",
          f"{_mk(_sub)} Подписка на канал",
          f"{_mk(bool(_commented))} Комментарий"
          + (f" ({_commented:%d.%m %H:%M})" if _commented else ""),
          ""]

    _ok = 0
    _lines = []
    for _r in _all_refs:
        _rn = ("@" + (_r["username"] or "")) if _r["username"] else (
            _r["full_name"] or f"id{_r['user_id']}")
        if _r["created_at"] and _r["created_at"] < _since_n:
            _lines.append(f"• {_rn} — ⛔ пришёл ДО старта ({_r['created_at']:%d.%m %H:%M})")
            continue
        _rs = await giveaway_is_subscribed(int(_r["user_id"]))
        if _rs is True:
            _ok += 1
            _lines.append(f"• {_rn} — ✅ в зачёте")
        elif _rs is False:
            _lines.append(f"• {_rn} — ❌ не подписан на канал")
        else:
            _lines.append(f"• {_rn} — ❓ Telegram не ответил")

    _t.append(f"{_mk(_ok >= _need)} Приглашено: <b>{_ok}</b> из {_need}")
    _t += _lines if _lines else ["<i>По его ссылке не пришёл никто.</i>"]
    _t.append("")
    _t.append(f"<i>В боте с {_me_row['created_at']:%d.%m.%Y %H:%M}"
              + (f", /start {_me_row['started_at']:%d.%m %H:%M}" if _me_row["started_at"]
                 else ", <b>/start не зафиксирован</b>")
              + (f", пригласил {_me_row['referred_by']}" if _me_row["referred_by"] else "")
              + (f", партнёр {_me_row['partner_id']}" if _me_row["partner_id"] else "")
              + "</i>")
    await message.answer("\n".join(_t), parse_mode="HTML",
                         disable_web_page_preview=True)


_ADMIN_ST = ("administrator", "creator", "owner")


async def _gw_bot_status(chat_id) -> tuple:
    """Статус самого бота в чате: (строка_статуса, админ_ли, текст_ошибки)."""
    try:
        _m = await bot.get_chat_member(chat_id, (await bot.get_me()).id)
        _raw = getattr(_m, "status", "")
        _st = str(getattr(_raw, "value", _raw)).split(".")[-1].lower()
        return _st or "?", _st in _ADMIN_ST, ""
    except Exception as _e:
        return "?", False, str(_e)[:120]


@dp.message(F.text.startswith("/giveaway_where"), StateFilter("*"))
async def giveaway_where(message: Message):
    """Диагностика для админа: всё ли готово к сбору участников.

    Мест, где бот должен быть админом, ДВА, и это неочевидно:
      канал — иначе Telegram не даст спросить, подписан ли человек;
      группа обсуждения — иначе бот вообще не получит комментарии
                          (у ботов-участников включён режим приватности).
    Проверяем оба и говорим прямо, чего не хватает.
    """
    if not is_admin(message.from_user.id):
        return

    # ── 1. Канал: нужен для проверки подписок ────────────────────────────
    _ch_st, _ch_adm, _ch_err = await _gw_bot_status(CHANNEL_ID)
    _ch_line = (f"✅ <b>Канал</b> — бот администратор ({_ch_st}).\n"
                f"<i>Проверка подписок работает.</i>"
                if _ch_adm else
                f"🚨 <b>Канал</b> — бот НЕ администратор"
                + (f" ({_ch_st})" if not _ch_err else f": {_ch_err}") + ".\n"
                f"<i>Без админки в канале бот не может спросить у Telegram, "
                f"подписан ли человек — все участники останутся "
                f"непроверенными, и розыгрыш не запустится.</i>")

    # ── 2. Группа обсуждения: нужна для сбора комментариев ───────────────
    _link = await giveaway_discussion_chat_id(force=True)
    if not _link:
        _gr_line = ("🚨 <b>Группа обсуждения не найдена.</b>\n"
                    "<i>Либо она не привязана к каналу, либо бот её не видит. "
                    "Комментарии собираться НЕ будут. Проверь: настройки "
                    "канала → «Обсуждение» → группа должна быть, и бот "
                    "добавлен в неё.</i>")
    else:
        _gr_st, _gr_adm, _gr_err = await _gw_bot_status(_link)
        _gr_line = (f"✅ <b>Группа обсуждения</b> (<code>{_link}</code>) — "
                    f"бот администратор ({_gr_st}).\n"
                    f"<i>Комментарии собираются.</i>"
                    if _gr_adm else
                    f"🚨 <b>Группа обсуждения</b> (<code>{_link}</code>) — "
                    f"бот НЕ администратор"
                    + (f" ({_gr_st})" if not _gr_err else f": {_gr_err}") + ".\n"
                    f"<i>У ботов включён режим приватности: обычному участнику "
                    f"группы чужие сообщения просто не приходят, и ни одно "
                    f"«Участвую» не засчитается. Дай боту права администратора "
                    f"в этой группе — годятся любые, важен сам статус.</i>")

    _all_ok = _ch_adm and bool(_link) and "✅" in _gr_line
    await message.answer(
        ("✅ <b>Всё готово к розыгрышу</b>\n\n" if _all_ok
         else "⚠️ <b>Готово не всё</b>\n\n")
        + _ch_line + "\n\n" + _gr_line
        + "\n\n<i>Бот видит только сообщения, отправленные ПОСЛЕ его "
          "вступления в группу. Если добавил его позже поста — ранние "
          "комментарии не засчитаны, задним числом их не собрать.</i>",
        parse_mode="HTML")
