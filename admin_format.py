# -*- coding: utf-8 -*-
"""Единое оформление сообщений Александру (админ-чат).

Александр 09.10.2026: «Такое оформление нужно использовать на всех
сообщениях по заказам — жирный, курсив и так далее». Сообщений Александру в
боте больше двухсот, и каждое собиралось на месте своим видом. Вместо правки
каждого — одно правило на выходе, для ВСЕХ сообщений в админ-чат (и новых
тоже), только с parse_mode=HTML:

  • первая строка (заголовок) — жирная, если ещё не оформлена;
  • «Подпись: значение» в начале строки → <b>Подпись:</b> значение
    (значение, целиком обёрнутое в <b>, разжирняется — иначе двойной жир);
  • значения «Ошибка / Причина / Ответ сайта …» — курсивом;
  • строка-пояснение обычным текстом (без разметки и эмодзи в начале,
    от 40 символов) — курсивом;
  • строки «❗ …» без разметки — курсивом.

Внутри <code>, <pre>, <blockquote>, <a> ничего не трогаем (там токены,
коды, ссылки). Если результат не прошёл проверку вложенности тегов —
отправляется исходный текст: оформление никогда не может сорвать отправку.
"""
import logging
import re
from html.parser import HTMLParser

from aiogram.client.session.middlewares.base import BaseRequestMiddleware

# Ведущий значок строки: обычный эмодзи (с вариантами/ZWJ) или уже
# превращённый в премиум <tg-emoji>…</tg-emoji>.
_EMOJI = (r"(?:<tg-emoji[^>]*>[^<]*</tg-emoji>|"
          r"[←-⯿☀-➿\U0001F000-\U0001FAFF©®‼⁉™ℹ]"
          r"[️‍⃣\U0001F3FB-\U0001F3FF\U0001F000-\U0001FAFF☀-➿]*)")
_LEAD = re.compile(r"^(\s*(?:" + _EMOJI + r"\s*)?)(.*)$", re.S)
# Подпись: буква в начале, без тегов, двоеточия и цифр-времени, до 32 символов.
_LABEL = re.compile(r"^([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9 ./()«»\-]{0,31}):(\s+|$)(.*)$", re.S)
_REASON = re.compile(r"^(ошибка|причина|ответ сайта|сайт ответил|сайт|почему)$", re.I)
_SITE_REASON = re.compile(r"^(ошибка|причина|ответ сайта|сайт ответил|почему)$", re.I)
_PROTECT = ("code", "pre", "blockquote", "a")


class _Check(HTMLParser):
    """Проверка: теги закрыты в правильном порядке."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.st, self.bad = [], False

    def handle_starttag(self, t, a):
        self.st.append(t)

    def handle_endtag(self, t):
        if not self.st or self.st[-1] != t:
            self.bad = True
        else:
            self.st.pop()


def _balanced(html_text: str) -> bool:
    p = _Check()
    try:
        p.feed(html_text)
        p.close()
    except Exception:
        return False
    return not p.bad and not p.st


def _open_delta(line: str, depth: dict) -> None:
    for m in re.finditer(r"<(/?)(code|pre|blockquote|a)\b[^>]*>", line):
        k = m.group(2)
        depth[k] = depth.get(k, 0) + (-1 if m.group(1) else 1)


def _has_tag(s: str) -> bool:
    return bool(re.search(r"<[a-zA-Z/][^>]*>", s))


def _unbold(v: str) -> str:
    """Значение сразу после жирной подписи: снимаем жир с его начала, иначе
    «Подпись: значение» читается сплошным жирным."""
    return re.sub(r"^(\s*)<b>([^<]*)</b>", r"\1\2", v, count=1)


def format_admin_html(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    if "<tg-spoiler" in text:
        return text
    lines = text.split("\n")
    depth = {}
    out = []
    first_done = False
    for ln in lines:
        inside = any(depth.get(k, 0) > 0 for k in _PROTECT)
        new = ln
        if not inside and ln.strip():
            m = _LEAD.match(ln)
            lead, rest = (m.group(1), m.group(2)) if m else ("", ln)
            if not first_done:
                # Заголовок: первая непустая строка.
                if not _has_tag(rest) and len(rest) <= 120:
                    new = f"{lead}<b>{rest}</b>"
            else:
                lm = _LABEL.match(rest)
                if lead.strip() and lead.strip()[:1] in ("❗", "‼") and rest and not _has_tag(rest):
                    new = f"{lead}<i>{rest}</i>"
                elif lm and not rest.lstrip().startswith("<"):
                    label, sp, val = lm.group(1), lm.group(2), lm.group(3)
                    val2 = _unbold(val)
                    if _SITE_REASON.match(label.strip()) and val2 and not _has_tag(val2):
                        val2 = f"<i>{val2}</i>"
                    new = f"{lead}<b>{label}:</b>{sp}{val2}"
                elif (not lead.strip() and not _has_tag(rest) and len(rest.strip()) >= 40
                      and not re.match(r"^\s*([•\-–—*\d]|@|https?://)", rest)):
                    new = f"<i>{rest}</i>"
        if ln.strip():
            first_done = True
        out.append(new)
        _open_delta(ln, depth)
    res = "\n".join(out)
    if res != text and not _balanced(res):
        return text
    return res


class AdminFormatMiddleware(BaseRequestMiddleware):
    """Оформляет HTML-сообщения в админ-чат (см. описание модуля)."""

    def __init__(self, admin_id: int):
        self.admin_id = int(admin_id)

    async def __call__(self, make_request, bot, method):
        try:
            pm = getattr(method, "parse_mode", None)
            cid = getattr(method, "chat_id", None)
            if (isinstance(pm, str) and pm.lower() == "html"
                    and cid is not None and str(cid) == str(self.admin_id)):
                if getattr(method, "text", None):
                    method.text = format_admin_html(method.text)
                if getattr(method, "caption", None):
                    method.caption = format_admin_html(method.caption)
        except Exception as e:
            logging.warning(f"admin format middleware: {e}")
        return await make_request(bot, method)
