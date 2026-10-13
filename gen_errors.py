# -*- coding: utf-8 -*-
"""Понятные ошибки генерации: что именно не прошло и как исправить.

Александр 13.10.2026: «если генерация не проходит из-за промта — бот должен
указывать мне, что конкретно не прошло и как исправить».

Раньше каждый поставщик (fal.ai, Google) сообщал отказ по-своему, а бот
превращал всё в одну фразу «Контент заблокирован моделью» — без причины.
Сам ответ поставщика (категория модерации, текст причины) выбрасывался.

GenError несёт:
  • kind      — вид сбоя: safety / input / params / no_output / overload /
                rate / timeout / provider / auth / balance;
  • category  — для safety: person / minor / sexual / violence / hate /
                dangerous / copyright / audio / unknown;
  • raw       — ответ поставщика как есть (для Александра);
  • labels    — метки модерации поставщика, если он их дал;
  • str(e)    — готовый текст для клиента (HTML), поэтому старый код,
                который делает str(e), продолжает работать.
"""
import html as _html
import json as _json
import re as _re


class GenError(Exception):
    def __init__(self, kind: str, client: str, *, category: str = "", raw: str = "",
                 labels=None, provider: str = "", model: str = "", hint: str = "",
                 found=None):
        super().__init__(client)
        self.kind = kind
        self.client = client
        self.category = category
        self.raw = str(raw or "")[:1500]
        self.labels = list(labels or [])
        self.provider = provider
        self.model = model
        self.hint = hint
        self.found = list(found or [])


# ── Категории отказа по содержанию ──────────────────────────────────────
# (подпись для людей, совет «как исправить», маркеры в ответе поставщика)
SAFETY_CATS = {
    "person": ("реальный или известный человек",
               "Убери имена и узнаваемых людей (актёров, политиков, блогеров) — опиши "
               "персонажа своими словами: возраст, внешность, одежда. Для фото с реальным "
               "человеком попробуй другую модель — у них разные правила.",
               ("celebrit", "public figure", "real person", "real people", "likeness",
                "identifiable", "prominent people", "famous", "person_generation",
                "photorealistic depiction", "people generation", "personality rights")),
    "minor": ("дети", "Сцены с детьми модели пропускают только полностью нейтральные. "
              "Проверь, что в промте нет ничего откровенного, жестокого или опасного.",
              ("child", "minor", "underage", "kids", "csam")),
    "sexual": ("откровенный контент", "Модели не создают откровенный контент — убери "
               "описания наготы, белья и интимных сцен.",
               ("sexual", "nsfw", "nudity", "nude", "explicit", "porn", "erotic",
                "suggestive", "adult content")),
    "violence": ("насилие или оружие", "Убери кровь, оружие и жестокие детали или опиши "
                 "сцену мягче, без натурализма.",
                 ("violen", "gore", "blood", "weapon", "graphic", "gun", "kill")),
    "hate": ("оскорбления или ненависть", "Убери оскорбления и выпады в адрес людей или групп.",
             ("hate", "harass", "offensive", "slur", "discriminat")),
    "dangerous": ("опасный контент", "Убери сцены и детали про наркотики, самоповреждение и "
                  "опасные действия.",
                  ("dangerous", "self-harm", "self_harm", "drug", "illegal", "terror")),
    "copyright": ("бренды, логотипы или чужие персонажи", "Убери названия брендов, логотипы, "
                  "известных персонажей и цитаты — опиши своими словами.",
                  ("copyright", "trademark", "recitation", "intellectual property",
                   "brand", "logo", "third-party content", "third party content")),
    "audio": ("звук в видео", "Видео не прошло проверку звука. Убери из промта реплики и песни "
              "или просто повтори — такое бывает случайно.",
              ("audio",)),
    "unknown": ("модель не назвала причину", "Переформулируй промт проще и нейтральнее или "
                "выбери другую модель — у них разные правила.", ()),
}

# Слова в промте клиента, которые чаще всего и вызывают отказ. Не «запрещёнка»
# сама по себе — подсказка Александру и клиенту, ЧТО именно могло не понравиться.
_PROMPT_STEMS = {
    "sexual": ("голая", "голый", "голые", "обнаж", "секс", "эрот", "порн", "интим", "нижн",
               "белье", "бельё", "грудь", "сиськ", "nude", "naked", "sexy", "lingerie",
               "bikini", "бикини", "nsfw", "18+"),
    "violence": ("кров", "убий", "убит", "оруж", "пистолет", "автомат", "винтов", "нож ", "ножом", "ножа ",
                 "труп", "расстрел", "взрыв", "война", "blood", "gun", "weapon", "kill", "gore",
                 "dead body"),
    "minor": ("ребён", "ребен", "дети", "детск", "девочк", "мальчик", "школьн", "подрост",
              "малыш", "младен", "child", "kid", "girl", "boy", "teen"),
    "person": ("путин", "трамп", "маск", "байден", "зеленск", "знаменит", "звезд", "актёр",
               "актер", "актрис", "певиц", "певец", "блогер", "президент", "celebrity",
               "elon", "trump", "putin", "taylor swift", "как у реального", "похож на"),
    "copyright": ("дисней", "disney", "марвел", "marvel", "пикачу", "pikachu", "микки",
                  "mickey", "гарри поттер", "harry potter", "spider-man", "человек-паук",
                  "бэтмен", "batman", "логотип", "logo", "nike", "adidas", "coca", "apple",
                  "мстител", "покемон", "pokemon", "симпсон", "simpsons"),
    "dangerous": ("наркот", "кокаин", "героин", "суицид", "самоубий", "бомб", "drug",
                  "cocaine", "suicide"),
    "hate": ("нацист", "свастик", "nazi", "swastika"),
}


def prompt_flags(prompt: str, category: str = "") -> list:
    """Слова из промта, похожие на причину отказа. Сначала — по категории
    отказа, если она известна; иначе — по всем."""
    _p = (prompt or "").lower()
    if not _p:
        return []
    _cats = [category] if category in _PROMPT_STEMS else list(_PROMPT_STEMS)
    out = []
    for _c in _cats:
        for _s in _PROMPT_STEMS[_c]:
            # Только с начала слова: «нож» не должен ловить «ножки», «boy» — «cowboy».
            _m = _re.search(r"(?<![\w])" + _re.escape(_s) + r"[\w\-+]*", _p)
            if _m:
                _w = _m.group(0).strip()
                if _w and _w not in out:
                    out.append(_w)
        if out and category:
            break
    return out[:6]


def guess_category(*texts) -> str:
    _t = " ".join(str(x or "") for x in texts).lower()
    # Порядок важен: «дети» и «реальные люди» важнее общих слов вроде graphic.
    for _c in ("minor", "person", "sexual", "violence", "hate", "dangerous", "copyright", "audio"):
        if any(_k in _t for _k in SAFETY_CATS[_c][2]):
            return _c
    return "unknown"


def safety_error(model_name: str, raw: str = "", labels=None, provider: str = "",
                 model: str = "", prompt: str = "", category: str = "") -> GenError:
    _cat = category or guess_category(raw, " ".join(str(x) for x in (labels or [])))
    _label, _advice, _ = SAFETY_CATS.get(_cat) or SAFETY_CATS["unknown"]
    _found = prompt_flags(prompt, _cat if _cat != "unknown" else "")
    _who = _html.escape(model_name or "Модель")
    if _found and _cat == "unknown":
        # Модель причину не назвала — честно говорим, что слова ниже лишь догадка.
        _why = (f"Причина: <b>{_label}</b>. Чаще всего отказ вызывают такие слова в "
                f"промте: «{_html.escape(', '.join(_found))}»")
    else:
        _why = (f"Причина: <b>{_label}</b>"
                + (f" — в промте: «{_html.escape(', '.join(_found))}»" if _found else ""))
    _client = (f"🛡 <b>{_who} отклонила запрос</b>\n{_why}"
               f"\n\n💡 <b>Как исправить:</b> {_advice}")
    return GenError("safety", _client, category=_cat, raw=raw, labels=labels,
                    provider=provider, model=model, hint=_advice, found=_found)


def strip_ids(text: str) -> str:
    """Убирает из текста для клиента технические хвосты (Request ID и т.п.)."""
    _t = _re.sub(r"\s*Request ID:\s*[\w\-]+\.?", "", str(text or ""))
    _t = _re.sub(r"\bfal\.ai\b", "сервиса генерации", _t)
    return _t.strip()


# ── fal.ai ──────────────────────────────────────────────────────────────
_FAL_INPUT_TYPES = {
    "image_too_small": "фото слишком маленькое",
    "image_too_large": "фото слишком большое",
    "image_load_error": "фото не удалось открыть (повреждено или неподдерживаемый формат)",
    "unsupported_image_format": "формат фото не поддерживается",
    "file_too_large": "файл слишком большой",
    "file_download_error": "модель не смогла загрузить файл",
    "face_detection_error": "на фото не найдено лицо",
    "video_duration_too_short": "видео слишком короткое",
    "video_duration_too_long": "видео слишком длинное",
    "unsupported_video_format": "формат видео не поддерживается",
}


def parse_fal_body(text: str):
    """(list[dict] details, str flat) — разбор тела ошибки fal.ai."""
    try:
        _j = _json.loads(text)
    except Exception:
        return [], str(text or "")
    _d = _j.get("detail") if isinstance(_j, dict) else None
    if isinstance(_d, list):
        return [x for x in _d if isinstance(x, dict)], str(text)
    if isinstance(_d, str):
        return [{"type": str(_j.get("error_type") or ""), "msg": _d}], str(text)
    if isinstance(_j, dict) and (_j.get("error") or _j.get("message")):
        return [{"type": str(_j.get("error_type") or ""),
                 "msg": str(_j.get("error") or _j.get("message"))}], str(text)
    return [], str(text)


def fal_error(status: int, body: str, model_name: str, model_id: str,
              prompt: str = "", stage: str = "") -> GenError:
    """GenError по ответу fal.ai с ошибкой (HTTP 4xx/5xx или тело с detail)."""
    _det, _flat = parse_fal_body(body)
    _types = [str(d.get("type") or "") for d in _det]
    _labels = []
    for d in _det:
        _ctx = d.get("ctx") if isinstance(d.get("ctx"), dict) else {}
        for _c in (d.get("categories") or _ctx.get("categories") or []):
            _labels.append(str(_c))
    _low = (_flat or "").lower()
    _raw = (f"HTTP {status} {stage} " if status else "") + (_flat or "")[:1200]
    _who = _html.escape(model_name or "Модель")
    if ("content_policy_violation" in _types or "content_policy_violation" in _low
            or "flagged by a content" in _low or "safety" in _low and status == 422
            or "moderation" in _low or "nsfw" in _low):
        return safety_error(model_name, raw=_raw, labels=_labels, provider="fal.ai",
                            model=model_id, prompt=prompt)
    for _t in _types:
        if _t in _FAL_INPUT_TYPES:
            _why = _FAL_INPUT_TYPES[_t]
            return GenError("input", f"📎 <b>{_who}</b>: {_why}.\n\n💡 Попробуй другое фото "
                            f"(JPG или PNG, обычного размера) или другую модель.",
                            raw=_raw, provider="fal.ai", model=model_id, hint=_why)
    if "no_media_generated" in _types:
        return GenError("no_output", f"🤷 <b>{_who}</b> ничего не создала по этому запросу.\n\n"
                        "💡 Переформулируй промт — опиши, что должно быть в кадре — или "
                        "выбери другую модель.", raw=_raw, provider="fal.ai", model=model_id)
    if status in (401, 403):
        return GenError("auth", "⚙️ Сервис генерации временно недоступен — уже разбираемся. "
                        "Попробуй другую модель.", raw=_raw, provider="fal.ai", model=model_id)
    if status == 402 or "exhausted balance" in _low or "insufficient" in _low and "balance" in _low:
        return GenError("balance", "⚙️ Сервис генерации временно недоступен — уже разбираемся. "
                        "Попробуй модели Nano Banana.", raw=_raw, provider="fal.ai", model=model_id)
    if status == 429 or "rate limit" in _low:
        return GenError("rate", "⏳ Сейчас слишком много запросов к модели. Подожди минуту "
                        "и попробуй снова.", raw=_raw, provider="fal.ai", model=model_id)
    if "generation_timeout" in _types or status == 504:
        return GenError("timeout", f"⏱ <b>{_who}</b> не успела за отведённое время.\n\n"
                        "💡 Попробуй ещё раз или выбери модель побыстрее.",
                        raw=_raw, provider="fal.ai", model=model_id)
    if (status >= 500 or "downstream_service" in _low or "internal_server_error" in _types
            or "unavailable" in _low):
        return GenError("overload", f"⚠️ <b>{_who}</b> временно недоступна у провайдера.\n\n"
                        "💡 Обычно проходит за пару минут. Попробуй ещё раз или выбери другую "
                        "модель.", raw=_raw, provider="fal.ai", model=model_id)
    if status == 422 or _det:
        # Ошибка параметров — это НАША ошибка (формат, длительность), не клиента.
        _where = "; ".join(f"{'.'.join(str(x) for x in (d.get('loc') or [])[1:])}: {d.get('msg')}"
                           for d in _det)[:300]
        return GenError("params", f"⚠️ <b>{_who}</b> не приняла параметры запроса "
                        "(формат или длительность).\n\n💡 Выбери другой формат, длительность "
                        "или другую модель — Александр уже получил отчёт.",
                        raw=(_where + " | " + _raw)[:1500], provider="fal.ai", model=model_id)
    return GenError("provider", f"⚠️ <b>{_who}</b> не смогла выполнить запрос.\n\n"
                    "💡 Попробуй ещё раз или другую модель.",
                    raw=_raw, provider="fal.ai", model=model_id)


# ── Google (Gemini / Veo) ───────────────────────────────────────────────
# "OTHER" сюда НЕ входит: это общий сбой, а не обязательно модерация
_GEMINI_BLOCK = {"SAFETY", "IMAGE_SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII",
                 "IMAGE_PROHIBITED_CONTENT"}
_GEMINI_CAT = {
    "HARM_CATEGORY_SEXUALLY_EXPLICIT": "sexual",
    "HARM_CATEGORY_HATE_SPEECH": "hate",
    "HARM_CATEGORY_HARASSMENT": "hate",
    "HARM_CATEGORY_DANGEROUS_CONTENT": "dangerous",
    "HARM_CATEGORY_CIVIC_INTEGRITY": "person",
}


def gemini_block_error(data: dict, model_name: str, model_id: str, prompt: str = ""):
    """GenError, если ответ Gemini — отказ по содержанию; иначе None."""
    if not isinstance(data, dict):
        return None
    _pf = data.get("promptFeedback") or {}
    _cands = data.get("candidates") or []
    _reason, _msg, _labels, _cat = "", "", [], ""
    if not _cands and _pf.get("blockReason"):
        _reason = str(_pf.get("blockReason"))
        _msg = str(_pf.get("blockReasonMessage") or "")
        _ratings = _pf.get("safetyRatings") or []
    elif _cands:
        _c0 = _cands[0] or {}
        _reason = str(_c0.get("finishReason") or "")
        _msg = str(_c0.get("finishMessage") or "")
        _ratings = _c0.get("safetyRatings") or []
        if _reason == "RECITATION":
            _cat = "copyright"
        elif _reason not in _GEMINI_BLOCK:
            return None
    else:
        return None
    for _r in _ratings or []:
        if not isinstance(_r, dict):
            continue
        if _r.get("blocked") or str(_r.get("probability", "")).upper() in ("HIGH", "MEDIUM"):
            _labels.append(f"{_r.get('category')}={_r.get('probability')}")
            _cat = _cat or _GEMINI_CAT.get(str(_r.get("category")), "")
    _raw = f"{_reason} {_msg}".strip() + (f" | ratings: {', '.join(_labels)}" if _labels else "")
    return safety_error(model_name, raw=_raw, labels=_labels, provider="Google",
                        model=model_id, prompt=prompt,
                        category=_cat or guess_category(_msg))


def google_http_error(status: int, body: str, model_name: str, model_id: str,
                      prompt: str = "") -> GenError:
    _low = (body or "").lower()
    _raw = f"HTTP {status} {(body or '')[:1200]}"
    _who = _html.escape(model_name or "Модель")
    if status == 404 or "not found" in _low and "model" in _low:
        return GenError("provider", f"⚠️ <b>{_who}</b> сейчас недоступна у Google.\n\n"
                        "💡 Выбери другую модель — Александр уже получил отчёт.",
                        raw=_raw, provider="Google", model=model_id)
    if status == 429 or "resource_exhausted" in _low or "quota" in _low:
        return GenError("rate", "⏳ Сейчас слишком много запросов к модели. Подожди минуту и "
                        "попробуй снова.", raw=_raw, provider="Google", model=model_id)
    if status in (500, 503) or "unavailable" in _low or "overloaded" in _low:
        return GenError("overload", f"⚠️ <b>{_who}</b> сейчас перегружена у Google.\n\n"
                        "💡 Обычно проходит за 1–3 минуты. Попробуй ещё раз или выбери другую "
                        "модель.", raw=_raw, provider="Google", model=model_id)
    if status == 400 and ("safety" in _low or "sensitive" in _low or "blocked" in _low
                          or "policy" in _low or "person" in _low or "celebrit" in _low):
        return safety_error(model_name, raw=_raw, provider="Google", model=model_id,
                            prompt=prompt)
    if status in (401, 403):
        return GenError("auth", "⚙️ Модель временно недоступна — уже разбираемся. Попробуй "
                        "другую модель.", raw=_raw, provider="Google", model=model_id)
    return GenError("params" if status == 400 else "provider",
                    f"⚠️ <b>{_who}</b> не приняла запрос.\n\n💡 Попробуй другой формат или "
                    "модель — Александр уже получил отчёт.",
                    raw=_raw, provider="Google", model=model_id)


def admin_lines(e: GenError) -> str:
    """Блок для сообщения Александру: что именно не прошло и как исправить."""
    _esc = lambda s: _html.escape(str(s or ""))
    _k = {"safety": "отказ по содержанию", "input": "не подошёл файл клиента",
          "params": "НАША ошибка параметров (формат/длительность) — нужно чинить",
          "no_output": "модель ничего не вернула", "overload": "перегрузка/сбой провайдера",
          "rate": "лимит запросов", "timeout": "таймаут", "provider": "ошибка провайдера",
          "auth": "КЛЮЧ API не принят — проверь переменные Railway",
          "lost": "задача у провайдера в неизвестном состоянии (могла выполниться) — повтор и резерв НЕ делались",
          "balance": "ЗАКОНЧИЛСЯ БАЛАНС у провайдера — пополни"}.get(e.kind, e.kind)
    _lines = [f"🔎 <b>Что случилось:</b> {_esc(_k)}"]
    if e.model:
        _lines.append(f"🤖 <b>Модель:</b> <code>{_esc(e.model)}</code>"
                      + (f" ({_esc(e.provider)})" if e.provider else ""))
    if e.kind == "safety":
        _lab = SAFETY_CATS.get(e.category, SAFETY_CATS["unknown"])[0]
        _lines.append(f"🛡 <b>Категория:</b> {_esc(_lab)}")
        if e.labels:
            _lines.append(f"🏷 <b>Метки модерации:</b> <code>{_esc(', '.join(e.labels)[:300])}</code>")
        if e.found:
            _lines.append(f"🔤 <b>Слова в промте:</b> {_esc(', '.join(e.found))}")
    if e.hint:
        _lines.append(f"💡 <b>Как исправить:</b> {_esc(e.hint)}")
    if e.raw:
        _lines.append(f"📄 <b>Ответ провайдера:</b> <code>{_esc(e.raw[:700])}</code>")
    return "\n".join(_lines)
