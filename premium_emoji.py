# -*- coding: utf-8 -*-
"""Premium-эмодзи middleware.

Автоматически заменяет известные обычные эмодзи на премиум (custom) эмодзи
во ВСЕХ исходящих сообщениях — без правок в сотнях мест:

* в HTML-тексте и подписях  → оборачивает эмодзи в <tg-emoji emoji-id="...">…</tg-emoji>
  (только при parse_mode="HTML", иначе тег показался бы как текст);
* на инлайн-кнопках          → если текст кнопки начинается с такого эмодзи,
  переносит его в icon_custom_emoji_id и убирает из текста.

Уже существующие <tg-emoji> теги (из tg_emoji_ui / model_title_n) не трогаются.
Чтобы добавить новый эмодзи — просто допиши пару «эмодзи: id» в PREMIUM_EMOJI.
"""
import re
import logging

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from config import UI_EMOJI_IDS

# ── Обычный эмодзи (БЕЗ variation selector) → id премиум-эмодзи ───────────────
PREMIUM_EMOJI = {
    "❌": "5271934564699226262",
    "✅": "5429501538806548545",
    "⚠": "5447644880824181073",
    "💵": "5300787605237931515",
    "📦": "5298900838989701082",
    "👇": "5470177992950946662",
    "👎": "5294010026585777874",
    "🎁": "5449800250032143374",
    "👋": "5440431182602842059",
    "✨": "5251676977785483690",
    "🎉": "5461151367559141950",
    "💸": "5316709465616031741",
    "➕": "5397916757333654639",
    "🔑": "5420094143089111506",
    "🎟": "5431742115870700579",
    "🔧": "5348239232852836489",
    "⬅": "5816895683256390576",
    "◀": "5816895683256390576",
    "1️⃣": "5426872181302771169",
    "1⃣": "5426872181302771169",
    "2️⃣": "5427054987995791430",
    "2⃣": "5427054987995791430",
    "3️⃣": "5427160442327808993",
    "3⃣": "5427160442327808993",
    "4️⃣": "5429229156275598979",
    "4⃣": "5429229156275598979",
}

# ── Вариант A: переиспользуем уже имеющиеся кастомные иконки ──────────────────
_VARIANT_A = {
    "📷": "menu_image", "📸": "menu_image",
    "🎬": "menu_video", "🎥": "menu_video",
    "👤": "menu_profile",
    "🛍": "menu_shop",
    "💳": "menu_buy", "💰": "menu_buy",
    "🏠": "back_main", "🏡": "back_main",
    "❤": "menu_favorites",
    "🔍": "menu_upscale",
    "🏃": "menu_anim",
    "🖌": "menu_edit",
    "💬": "menu_chat",
}
for _ch, _key in _VARIANT_A.items():
    _eid = UI_EMOJI_IDS.get(_key, "")
    if _eid:
        PREMIUM_EMOJI.setdefault(_ch, _eid)

_VS16 = "️"
_keys = sorted(PREMIUM_EMOJI.keys(), key=len, reverse=True)
_emoji_re = re.compile("(" + "|".join(re.escape(k) for k in _keys) + ")" + _VS16 + "?")
_protect_re = re.compile(r"<tg-emoji\b[^>]*>.*?</tg-emoji>", re.S)


def _convert(text: str) -> str:
    def _repl(m):
        ch = m.group(1)
        return f'<tg-emoji emoji-id="{PREMIUM_EMOJI[ch]}">{m.group(0)}</tg-emoji>'
    return _emoji_re.sub(_repl, text)


def premiumize_text(text: str) -> str:
    """Заменяет эмодзи на <tg-emoji>, НЕ трогая уже существующие <tg-emoji> теги."""
    if not text:
        return text
    if "<tg-emoji" not in text:
        return _convert(text)
    out = []
    last = 0
    for m in _protect_re.finditer(text):
        out.append(_convert(text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_convert(text[last:]))
    return "".join(out)


def _premiumize_buttons(markup) -> None:
    rows = getattr(markup, "inline_keyboard", None)  # только инлайн-кнопки
    if not rows:
        return
    for row in rows:
        for btn in row:
            if getattr(btn, "icon_custom_emoji_id", None):
                continue  # иконка уже задана (например через _eib)
            t = getattr(btn, "text", "") or ""
            m = _emoji_re.match(t)
            if not m:
                continue
            eid = PREMIUM_EMOJI.get(m.group(1))
            if not eid:
                continue
            rest = t[m.end():].lstrip()
            if not rest:
                continue  # не оставляем кнопку без текста
            try:
                btn.text = rest
                object.__setattr__(btn, "icon_custom_emoji_id", eid)
            except Exception:
                pass


class PremiumEmojiMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        try:
            pm = getattr(method, "parse_mode", None)
            if isinstance(pm, str) and pm.lower() == "html":
                if getattr(method, "text", None):
                    method.text = premiumize_text(method.text)
                if getattr(method, "caption", None):
                    method.caption = premiumize_text(method.caption)
            markup = getattr(method, "reply_markup", None)
            if markup is not None:
                _premiumize_buttons(markup)
        except Exception as e:
            logging.warning(f"premium emoji middleware: {e}")
        return await make_request(bot, method)
