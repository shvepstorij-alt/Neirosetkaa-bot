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
    giveaway_active, giveaway_add_comment, giveaway_refs,
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
    _refs = await giveaway_refs(user.id, _gw["starts_at"])
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
    _t += (f"🔗 <b>Твоя ссылка:</b>\n<code>{_link}</code>\n"
           f"<i>Друг должен открыть её и запустить бота — и подписаться на канал.</i>\n\n")
    _t += ("🎉 <b>Все условия выполнены — ты в списке!</b>"
           if _all else "Осталось закрыть пункты выше 👆")

    _rows = []
    _post = (_gw.get("post_url") or "").strip()
    if _post.lower().startswith(("http://", "https://")):
        _rows.append([InlineKeyboardButton(text="📢 Открыть пост", url=_post)])
    _rows.append([InlineKeyboardButton(text="🔄 Проверить ещё раз",
                                       callback_data="gw_recheck")])
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
    # Правим то же сообщение, если можем; если нет — присылаем новое.
    for _send in (
        lambda: cb.message.edit_text(_t, parse_mode="HTML",
                                     disable_web_page_preview=True,
                                     reply_markup=_kb),
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


@dp.message(F.text.startswith("/giveaway_where"), StateFilter("*"))
async def giveaway_where(message: Message):
    """Диагностика для админа: видит ли бот группу обсуждения."""
    if not is_admin(message.from_user.id):
        return
    _link = await giveaway_discussion_chat_id(force=True)
    if not _link:
        await message.answer(
            "❌ <b>Группа обсуждения не найдена</b>\n\n"
            "Либо она не привязана к каналу, либо бот не может её увидеть.\n"
            "Комментарии собираться НЕ будут.\n\n"
            "Проверь: в настройках канала → «Обсуждение» должна быть группа, "
            "и бот должен быть в неё добавлен.", parse_mode="HTML")
        return
    _me = "?"
    try:
        _m = await bot.get_chat_member(_link, (await bot.get_me()).id)
        _me = str(getattr(_m, "status", "?"))
    except Exception as _e:
        _me = f"не вижу ({_e})"
    await message.answer(
        f"✅ <b>Группа обсуждения найдена</b>\n"
        f"id: <code>{_link}</code>\n"
        f"бот в ней: <b>{_me}</b>\n\n"
        f"<i>Бот видит только сообщения, отправленные ПОСЛЕ его вступления "
        f"в группу. Если добавил его позже поста — ранние комментарии "
        f"не засчитаны.</i>", parse_mode="HTML")
