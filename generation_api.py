# -*- coding: utf-8 -*-
# Auto-split module "generation_api" — part of Neirosetkaa-bot (refactored from bot.py).
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
    BOT_TOKEN, EVOLINK_API_KEY, FAL_API_KEY, GEMINI_API_KEY, MOTION_MODEL_ID, _RETRY_STATUSES,
    bot,
)
import html as _html
from gen_errors import (
    GenError, fal_error, gemini_block_error, google_http_error, safety_error,
)

# Таймауты для «долгих» сессий (polling генерации, скачивание результата).
# Раньше эти сессии создавались вообще без таймаута: зависший сокет держал
# генерацию бесконечно — клиент видел «Генерирую…» без конца, кредиты уже списаны.
# total=None, потому что polling законно идёт минутами; sock_connect/sock_read
# страхуют именно от «мёртвого» соединения.
_LONG_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=180)


def _is_retryable_error(exc: Exception) -> bool:
    """Проверяет, стоит ли повторять запрос при этой ошибке.

    Часть 35: таймаут больше НЕ повод повторять. Синхронный запрос к fal.ai или
    Google, оборвавшийся по таймауту, мог уже выполниться у провайдера — повтор
    означал вторую оплату той же генерации за наш счёт. Повторяем только явную
    перегрузку и лимит запросов: такие запросы провайдер не выполнял и не списывал.
    """
    if isinstance(exc, GenError):
        return exc.kind in ("overload", "rate")
    msg = str(exc).lower()
    # Явные HTTP-статусы в тексте
    for code in _RETRY_STATUSES:
        if f" {code}:" in msg or f" {code} " in msg:
            return True
    # Ключевые слова временных ошибок (без timeout — см. выше)
    triggers = [
        "rate limit", "temporarily",
        "unavailable", "try again", "internal error",
        "connection reset", "connection aborted",
    ]
    return any(t in msg for t in triggers)


async def _with_retry(coro_factory, max_attempts: int = 3, base_delay: float = 2.0,
                      op_name: str = "API", on_retry=None):
    """Запускает coroutine с автоматическими повторами при транзиентных ошибках.
    
    coro_factory - функция которая создаёт НОВЫЙ coroutine при каждой попытке
    (нельзя повторно await'ить тот же coroutine).
    on_retry - опциональный async callback (attempt, delay, error) для UI-уведомлений.
    
    Для 503 (перегрузка модели) - увеличенные паузы, т.к. короткие ретраи бесполезны.
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except Exception as e:
            last_exc = e
            if attempt == max_attempts or not _is_retryable_error(e):
                raise

            # Адаптивная пауза: для 503 (перегрузка) ждём дольше
            err_str = str(e).lower()
            is_overload = ("503" in err_str or "unavailable" in err_str
                           or "high demand" in err_str or "overloaded" in err_str)
            if is_overload:
                # Для перегрузки: 15с, 30с, 60с - даём времени стихнуть пику
                delay = 15.0 * (2 ** (attempt - 1))
            else:
                # Для остальных (rate limit, timeout): обычный бэкофф 2с, 4с, 8с
                delay = base_delay * (2 ** (attempt - 1))
            logging.warning(f"{op_name} attempt {attempt}/{max_attempts} failed: {str(e)[:150]} - retrying in {delay}s")

            # Уведомляем UI если передан callback
            if on_retry:
                try:
                    await on_retry(attempt, delay, e)
                except Exception as cb_err:
                    logging.debug(f"on_retry callback failed: {cb_err}")

            await asyncio.sleep(delay)
    if last_exc:
        raise last_exc


# ─── Retry helper для Google API ──────────────────────────

async def compress_video(vid_bytes: bytes, target_mb: float = 45.0) -> bytes:
    """Сжимает видео через ffmpeg чтобы уложиться в лимит Telegram (50 МБ).
    Уменьшает битрейт пропорционально целевому размеру.
    Возвращает сжатые байты или оригинал если ffmpeg недоступен.
    """
    import tempfile, subprocess, os as _os
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fin:
        fin.write(vid_bytes)
        fin_path = fin.name
    fout_path = fin_path.replace(".mp4", "_compressed.mp4")
    try:
        # Получаем длительность через ffprobe
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", fin_path],
            capture_output=True, text=True, timeout=30
        )
        duration = float(probe.stdout.strip()) if probe.stdout.strip() else 60.0
        # Целевой битрейт в kbps (оставляем 128k на аудио)
        target_kbps = int((target_mb * 8 * 1024) / duration) - 128
        target_kbps = max(500, target_kbps)  # минимум 500 kbps для приемлемого качества
        result = subprocess.run([
            "ffmpeg", "-y", "-i", fin_path,
            "-c:v", "libx264", "-b:v", f"{target_kbps}k",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            fout_path
        ], capture_output=True, timeout=120)
        if result.returncode == 0 and _os.path.exists(fout_path):
            with open(fout_path, "rb") as f:
                compressed = f.read()
            logging.info(f"compress_video: {len(vid_bytes)/1024/1024:.1f}MB → {len(compressed)/1024/1024:.1f}MB")
            return compressed
        else:
            logging.error(f"ffmpeg error: {result.stderr[:300]}")
            return vid_bytes
    except Exception as e:
        logging.error(f"compress_video exception: {e}")
        return vid_bytes
    finally:
        for p in [fin_path, fout_path]:
            try:
                _os.unlink(p)
            except Exception:
                pass


async def upload_large_file(file_bytes: bytes, filename: str = "video.mp4") -> str | None:
    """Загружает большой файл на 0x0.st и возвращает ссылку для скачивания.
    Файл живёт 24 часа. Используется когда видео > 48 МБ и не влезает в Telegram.
    Возвращает URL или None при ошибке.
    """
    try:
        import aiohttp as _aiohttp
        timeout = _aiohttp.ClientTimeout(total=120)
        async with _aiohttp.ClientSession(timeout=timeout) as s:
            data = _aiohttp.FormData()
            data.add_field("file", file_bytes, filename=filename, content_type="video/mp4")
            async with s.post("https://0x0.st", data=data) as r:
                if r.status == 200:
                    url = (await r.text()).strip()
                    if url.startswith("http"):
                        logging.info(f"upload_large_file: {len(file_bytes)/1024/1024:.1f}MB → {url}")
                        return url
        logging.warning(f"upload_large_file: bad response {r.status}")
        return None
    except Exception as e:
        logging.error(f"upload_large_file failed: {e}")
        return None


# ══════════════════════════════════════════════════════════
#  Общие помощники (часть 35)
# ══════════════════════════════════════════════════════════

# Veo 3.1 (preview) Google отключает 22.10.2026 — после этой даты резерв на Veo
# не имеет смысла, запрос гарантированно получит 404.
_VEO_SHUTDOWN = datetime.date(2026, 10, 22)


def _veo_alive() -> bool:
    return datetime.date.today() < _VEO_SHUTDOWN


def _model_name(model_id: str, fallback: str = "") -> str:
    """Человеческое имя модели по model_id (для текста ошибки клиенту)."""
    if fallback:
        return re.sub(r"^[^\w]+\s*", "", fallback).strip() or fallback
    try:
        from config import IMAGE_MODELS, VIDEO_MODELS, EDIT_MODELS, ANIM_MODELS
        for _d in (IMAGE_MODELS, VIDEO_MODELS, EDIT_MODELS, ANIM_MODELS):
            for _m in _d.values():
                if _m.get("model_id") == model_id and _m.get("name"):
                    return re.sub(r"^[^\w]+\s*", "", _m["name"]).strip()
    except Exception:
        pass
    return "Модель"


def _mime_of(data: bytes) -> str:
    """MIME по сигнатуре файла. Раньше любое фото отправлялось как image/jpeg —
    PNG/WebP из мини-аппа часть моделей отклоняла как «повреждённый файл»."""
    _b = bytes(data[:12] or b"")
    if _b.startswith(b"\x89PNG"):
        return "image/png"
    if _b[:4] == b"RIFF" and _b[8:12] == b"WEBP":
        return "image/webp"
    if _b[:3] == b"GIF":
        return "image/gif"
    return "image/jpeg"


def _data_uri(data: bytes) -> str:
    return f"data:{_mime_of(data)};base64,{base64.b64encode(data).decode('utf-8')}"


async def _fal_download(url: str, *, model_name: str, model_id: str, kind: str = "image",
                        request_id: str = "", min_bytes: int = 1000,
                        attempts: int = 3, timeout_total: int = 180) -> bytes:
    """Скачивание готового результата fal.ai с проверкой статуса.

    Раньше ответ скачивался без проверки HTTP-статуса: при 403/404 клиенту
    уходил «файл» из текста ошибки (несколько сотен байт), Telegram его
    отклонял, а кредиты при этом считались потраченными.
    """
    _last = ""
    _to = aiohttp.ClientTimeout(total=timeout_total, sock_read=120)
    for _a in range(1, attempts + 1):
        try:
            async with aiohttp.ClientSession(timeout=_to) as _s:
                async with _s.get(url) as _r:
                    if _r.status != 200:
                        _last = f"HTTP {_r.status}"
                    else:
                        _b = await _r.read()
                        if len(_b) >= min_bytes:
                            return _b
                        _last = f"слишком маленький файл ({len(_b)} байт)"
        except asyncio.TimeoutError:
            _last = "timeout"
        except Exception as _e:
            _last = f"{type(_e).__name__}: {str(_e)[:150]}"
        logging.warning(f"fal download {kind} attempt {_a}/{attempts} failed: {_last} url={url[:120]}")
        if _a < attempts:
            await asyncio.sleep(2 * _a)
    _what = "Видео" if kind == "video" else "Картинка"
    # "lost": результат уже создан и оплачен — повтор/резерв означали бы вторую оплату.
    raise GenError(
        "lost",
        f"⚠️ {_what} создано, но не скачалось с сервера генерации.\n\n"
        "💡 Попробуй ещё раз — Александр уже получил отчёт и может восстановить файл.",
        raw=f"download failed: {_last} | request_id={request_id} | url={url[:300]}",
        provider="fal.ai", model=model_id,
        hint=(f"Восстановить: /recover {request_id} <user_id>" if request_id and kind == "video"
              else ""))


def _fal_extract_url(result: dict, kind: str) -> str:
    if not isinstance(result, dict):
        return ""
    if kind == "video":
        _v = result.get("video")
        if isinstance(_v, dict) and _v.get("url"):
            return _v["url"]
        if isinstance(_v, str) and _v.startswith("http"):
            return _v
        if result.get("video_url"):
            return str(result["video_url"])
        _o = result.get("output")
        if isinstance(_o, dict):
            _ov = _o.get("video")
            if isinstance(_ov, dict) and _ov.get("url"):
                return _ov["url"]
            if _o.get("video_url"):
                return str(_o["video_url"])
        return ""
    _imgs = result.get("images")
    if isinstance(_imgs, list) and _imgs:
        _i0 = _imgs[0]
        if isinstance(_i0, dict) and _i0.get("url"):
            return _i0["url"]
        if isinstance(_i0, str):
            return _i0
    _im = result.get("image")
    if isinstance(_im, dict) and _im.get("url"):
        return _im["url"]
    return ""


def _fal_no_result_error(result, kind: str, model_name: str, model_id: str,
                         prompt: str, request_id: str = "") -> GenError:
    """Ответ пришёл без картинки/видео — разбираемся, почему."""
    _txt = json.dumps(result, ensure_ascii=False)[:1500] if isinstance(result, (dict, list)) else str(result)
    _low = _txt.lower()
    if isinstance(result, dict) and (result.get("detail") or "content_policy_violation" in _low
                                     or "flagged by a content" in _low):
        return fal_error(0, _txt, model_name, model_id, prompt=prompt, stage="result")
    # Flux и др.: картинка «зацензурена» чёрным кадром, флаг has_nsfw_concepts
    _nsfw = result.get("has_nsfw_concepts") if isinstance(result, dict) else None
    if isinstance(_nsfw, list) and any(_nsfw):
        return safety_error(model_name, raw=_txt, labels=["has_nsfw_concepts"],
                            provider="fal.ai", model=model_id, prompt=prompt, category="sexual")
    _what = "видео" if kind == "video" else "картинку"
    return GenError("no_output",
                    f"🤷 <b>{_html.escape(model_name)}</b> не вернула {_what} по этому запросу.\n\n"
                    "💡 Переформулируй промт — опиши, что должно быть в кадре, — или выбери "
                    "другую модель.",
                    raw=f"request_id={request_id} | {_txt}", provider="fal.ai", model=model_id)


async def _fal_queue_run(model_id: str, payload: dict, poll_max: int = 90, *,
                         model_name: str = "", prompt: str = "", interval: float = 5.0,
                         first_wait: float = 0.0, queue_limit_sec: int = 360,
                         log_tag: str = "fal") -> dict:
    """Универсальный запуск fal.ai queue: submit → poll → result (JSON).

    Часть 35. Раньше:
      • ответ submit не проверялся по статусу — ошибка 422 (модерация или
        неверный параметр) читалась как «нет request_id»;
      • когда задача упала у провайдера, fal всё равно отдаёт статус COMPLETED,
        а причина лежит в ответе result с кодом 4xx/5xx. Этот ответ читался как
        «результат», и клиент получал «нет изображения: {detail…}» без причины.
    Теперь любая ошибка разбирается gen_errors.fal_error: клиент видит, что именно
    не прошло и как исправить, Александр — сырой ответ провайдера.
    """
    _name = model_name or _model_name(model_id)
    if not FAL_API_KEY:
        raise GenError("auth", "⚙️ Сервис генерации временно недоступен — уже разбираемся.",
                       raw="FAL_API_KEY не задан в Railway", provider="fal.ai", model=model_id)
    headers = {"Authorization": f"Key {FAL_API_KEY}", "Content-Type": "application/json",
               "Accept": "application/json"}
    poll_headers = {"Authorization": f"Key {FAL_API_KEY}", "Accept": "application/json"}
    queue_url = f"https://queue.fal.run/{model_id}"
    async with aiohttp.ClientSession(timeout=_LONG_TIMEOUT) as s:
        # 1. Постановка в очередь
        try:
            async with s.post(queue_url, headers=headers, json=payload,
                              timeout=aiohttp.ClientTimeout(total=90)) as r:
                _body = await r.text()
                if r.status not in (200, 201, 202):
                    logging.error(f"{log_tag} submit HTTP {r.status} model={model_id} "
                                  f"keys={list(payload)} body={_body[:500]}")
                    raise fal_error(r.status, _body, _name, model_id, prompt=prompt, stage="submit")
                try:
                    submit = json.loads(_body)
                except Exception:
                    raise GenError("provider", f"⚠️ <b>{_html.escape(_name)}</b> сейчас не отвечает.\n\n"
                                   "💡 Попробуй ещё раз через минуту или выбери другую модель.",
                                   raw=f"submit non-JSON HTTP {r.status}: {_body[:400]}",
                                   provider="fal.ai", model=model_id)
        except aiohttp.ClientConnectorError as _ne:
            # Соединение даже не установилось — задача точно не создана, денег не стоила.
            raise GenError("overload", f"⚠️ <b>{_html.escape(_name)}</b> сейчас недоступна "
                           "(нет связи с сервером генерации).\n\n💡 Попробуй ещё раз через минуту.",
                           raw=f"submit network: {type(_ne).__name__}: {_ne}", provider="fal.ai",
                           model=model_id)
        except (aiohttp.ClientError, asyncio.TimeoutError) as _ne:
            # Запрос ушёл, ответа нет: fal МОГ принять задачу. Не повторяем и не
            # уходим на резерв — иначе возможна двойная оплата у провайдера.
            raise GenError("lost", f"⚠️ <b>{_html.escape(_name)}</b>: сервер генерации не ответил.\n\n"
                           "💡 Попробуй ещё раз через пару минут.",
                           raw=f"submit no response: {type(_ne).__name__}: {_ne}", provider="fal.ai",
                           model=model_id)
        request_id = submit.get("request_id") if isinstance(submit, dict) else None
        if not request_id:
            raise fal_error(0, json.dumps(submit, ensure_ascii=False)[:1200], _name, model_id,
                            prompt=prompt, stage="submit")
        status_url = submit.get("status_url") or f"{queue_url}/requests/{request_id}/status"
        response_url = submit.get("response_url") or f"{queue_url}/requests/{request_id}"
        logging.info(f"{log_tag} submitted: {request_id} model={model_id} keys={list(payload)}")
        cancel_url = submit.get("cancel_url") or f"{queue_url}/requests/{request_id}/cancel"

        async def _cancel() -> bool:
            """Отмена задачи в очереди fal (PUT …/cancel). True — fal подтвердил отмену:
            такую задачу он не выполнит и не спишет."""
            try:
                async with s.put(cancel_url, headers=poll_headers,
                                 timeout=aiohttp.ClientTimeout(total=20)) as cr:
                    _cb = await cr.text()
                    logging.info(f"{log_tag} cancel {request_id}: HTTP {cr.status} {_cb[:120]}")
                    return cr.status in (200, 202) and "already" not in _cb.lower()
            except Exception as _ce:
                logging.warning(f"{log_tag} cancel {request_id} failed: {_ce}")
                return False

        async def _fetch_result():
            """(status, dict|None, body) — результат или ошибка из response_url."""
            _urls = [response_url]
            if "queue.fal.run" in response_url:
                _urls.append(response_url.replace("queue.fal.run", "fal.run"))
            _last = (0, None, "")
            for _u in _urls:
                for _a in range(5):
                    try:
                        async with s.get(_u, headers=poll_headers,
                                         timeout=aiohttp.ClientTimeout(total=60)) as rr:
                            _b = await rr.text()
                            _j = None
                            try:
                                _j = json.loads(_b)
                            except Exception:
                                pass
                            if rr.status == 200 and isinstance(_j, dict):
                                return 200, _j, _b
                            if rr.status >= 400 and rr.status != 404:
                                # Задача завершилась ошибкой — это её причина.
                                return rr.status, _j, _b
                            _last = (rr.status, _j, _b)
                    except (aiohttp.ClientError, asyncio.TimeoutError) as _fe:
                        _last = (0, None, f"{type(_fe).__name__}: {_fe}")
                    await asyncio.sleep(4)
            return _last

        # 2. Ожидание
        if first_wait:
            await asyncio.sleep(first_wait)
        _in_queue_since = None
        _errors = 0
        _done = False
        for _i in range(poll_max):
            await asyncio.sleep(interval)
            try:
                async with s.get(status_url, headers=poll_headers,
                                 timeout=aiohttp.ClientTimeout(total=20)) as sr:
                    _sb = await sr.text()
                    if sr.status in (401, 403):
                        raise fal_error(sr.status, _sb, _name, model_id, prompt=prompt, stage="status")
                    if sr.status == 405:
                        logging.warning(f"{log_tag} status 405 → читаю результат напрямую {request_id}")
                        _st, _j, _b = await _fetch_result()
                        if _st == 200 and _j is not None:
                            _j["_request_id"] = request_id
                            return _j
                        continue
                    if sr.status not in (200, 202):
                        _errors += 1
                        if _errors >= 12:
                            raise GenError("lost", f"⚠️ <b>{_html.escape(_name)}</b>: сервер "
                                           "генерации перестал отвечать.\n\n💡 Попробуй ещё раз позже.",
                                           raw=f"status HTTP {sr.status} x{_errors}: {_sb[:300]} "
                                               f"request_id={request_id}",
                                           provider="fal.ai", model=model_id)
                        continue
                    try:
                        st = json.loads(_sb)
                    except Exception:
                        _errors += 1
                        continue
            except (aiohttp.ClientError, asyncio.TimeoutError) as _pe:
                _errors += 1
                logging.warning(f"{log_tag} poll error {request_id}: {_pe}")
                if _errors >= 12:
                    raise GenError("lost", "⚠️ Связь с сервером генерации потеряна.\n\n"
                                   "💡 Попробуй ещё раз позже — если списались кредиты, они вернутся.",
                                   raw=f"poll network x{_errors}: {_pe} request_id={request_id}",
                                   provider="fal.ai", model=model_id)
                continue
            _errors = 0
            _status = str(st.get("status") or "").upper()
            if _status == "COMPLETED":
                _done = True
                break
            if _status in ("FAILED", "ERROR", "CANCELLED"):
                # Подробности лежат в response_url — берём их оттуда.
                _st, _j, _b = await _fetch_result()
                raise fal_error(_st or 500, _b or json.dumps(st, ensure_ascii=False),
                                _name, model_id, prompt=prompt, stage="status")
            if _status == "IN_QUEUE":
                _in_queue_since = _in_queue_since or time.time()
                if time.time() - _in_queue_since > queue_limit_sec:
                    # Задачу снимаем с очереди. Подтвердил fal отмену — она не
                    # выполнится и не спишется, можно повторить/уйти на резерв
                    # ("overload"). Не подтвердил — "lost": повторять нельзя.
                    _cancelled = await _cancel()
                    raise GenError("overload" if _cancelled else "lost",
                                   f"⏱ <b>{_html.escape(_name)}</b>: очередь у провайдера не двигается "
                                   f"{queue_limit_sec // 60}+ мин.\n\n💡 Попробуй ещё раз позже или выбери "
                                   "другую модель.",
                                   raw=f"IN_QUEUE > {queue_limit_sec}s request_id={request_id} "
                                       f"cancelled={_cancelled}",
                                   provider="fal.ai", model=model_id)
            else:
                _in_queue_since = None
            if (_i + 1) % 12 == 0:
                logging.info(f"{log_tag} still {_status} {request_id} ({model_id})")

        # 3. Результат (или последняя попытка после истечения ожидания)
        _st, _j, _b = await _fetch_result()
        if _st == 200 and isinstance(_j, dict):
            if not _done:
                logging.info(f"{log_tag} rescued after poll timeout: {request_id}")
            if _j.get("detail") and not (_fal_extract_url(_j, "video") or _fal_extract_url(_j, "image")):
                raise fal_error(0, _b, _name, model_id, prompt=prompt, stage="result")
            _j["_request_id"] = request_id
            return _j
        if not _done:
            # Ждать дальше нельзя, а задача ещё идёт: fal на её result может
            # отвечать и 4xx («still in progress») — это НЕ ошибка параметров.
            # Пытаемся отменить; повтора и резерва нет (kind="timeout").
            await _cancel()
            raise GenError("timeout", f"⏱ <b>{_html.escape(_name)}</b> не успела за отведённое "
                           "время.\n\n💡 Попробуй ещё раз или выбери модель побыстрее.",
                           raw=f"poll timeout request_id={request_id} last_result={_st} {(_b or '')[:200]}",
                           provider="fal.ai", model=model_id,
                           hint=f"Если видео всё же появится: /recover {request_id} <user_id>")
        if _st and _st >= 400:
            logging.error(f"{log_tag} result HTTP {_st} {request_id} model={model_id}: {(_b or '')[:600]}")
            raise fal_error(_st, _b or "", _name, model_id, prompt=prompt, stage="result")
        raise GenError("lost", f"⚠️ <b>{_html.escape(_name)}</b>: не удалось получить результат.\n\n"
                       "💡 Попробуй ещё раз.", raw=f"result {_st}: {(_b or '')[:400]} request_id={request_id}",
                       provider="fal.ai", model=model_id)


def _fal_content_blocked(result) -> bool:
    _rs = str(result).lower()
    return "content_policy_violation" in _rs or "flagged by a content" in _rs


# ══════════════════════════════════════════════════════════
#  ИЗОБРАЖЕНИЯ — fal.ai (Flux 2 Pro, Ideogram V3, GPT Image 2, Grok)
# ══════════════════════════════════════════════════════════

async def api_generate_fal_image(prompt: str, model_id: str, aspect_ratio: str = "1:1",
                                  quality: str = "medium", _retry_count: int = 0,
                                  model_name: str = "") -> bytes:
    """Генерация изображений через fal.ai (sync endpoint — результат приходит сразу).

    quality: для GPT Image 2 — 'low' / 'medium' / 'high'; для Grok — 'quality'.
    _retry_count: внутренний счётчик повторов при downstream_service_error.
    """
    MAX_DOWNSTREAM_RETRIES = 2  # Всего 3 попытки (0, 1, 2)
    _name = model_name or _model_name(model_id)
    if not FAL_API_KEY:
        raise GenError("auth", "⚙️ Сервис генерации временно недоступен — уже разбираемся.",
                       raw="FAL_API_KEY не задан в Railway", provider="fal.ai", model=model_id)

    _orig_prompt = prompt
    # Санитизация промта для GPT Image 2 — OpenAI плохо обрабатывает
    # ёлочки «», фигурные кавычки и множественные переносы.
    if "gpt-image-2" in model_id:
        prompt = prompt.replace('«', '"').replace('»', '"')
        prompt = (prompt.replace('“', '"').replace('”', '"')
                  .replace('‘', "'").replace('’', "'"))
        prompt = re.sub(r'\n{2,}', '\n', prompt)
        prompt = '\n'.join(line.strip() for line in prompt.split('\n'))
        prompt = re.sub(r' {2,}', ' ', prompt).strip()

    aspect_map_flux = {
        "1:1": "square_hd",
        "16:9": "landscape_16_9",
        "9:16": "portrait_16_9",
        "4:3": "landscape_4_3",
        "3:4": "portrait_4_3",
    }

    url = f"https://fal.run/{model_id}"
    headers = {"Authorization": f"Key {FAL_API_KEY}", "Content-Type": "application/json"}

    if "flux-2" in model_id:
        payload = {
            "prompt": prompt,
            "image_size": aspect_map_flux.get(aspect_ratio, "square_hd"),
            "num_images": 1,
            "enable_safety_checker": True,
        }
    elif "ideogram" in model_id:
        payload = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "rendering_speed": "BALANCED",
            "num_images": 1,
        }
    elif "gpt-image-2" in model_id:
        payload = {
            "prompt": prompt,
            "image_size": aspect_map_flux.get(aspect_ratio, "square_hd"),
            "quality": quality if quality in ("low", "medium", "high") else "medium",
            "num_images": 1,
        }
        logging.info(f"GPT Image 2 payload: model={model_id} quality={payload['quality']} "
                     f"aspect={aspect_ratio} size={payload['image_size']}")
    elif "grok-imagine-image" in model_id:
        payload = {"prompt": prompt, "aspect_ratio": aspect_ratio, "num_images": 1}
        if quality == "quality":
            payload["mode"] = "quality"
    else:
        payload = {"prompt": prompt}

    timeout_sec = 300 if "gpt-image-2" in model_id and quality == "high" else 180
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as s:
            async with s.post(url, json=payload, headers=headers) as r:
                _body = await r.text()
                if r.status != 200:
                    logging.error(f"fal.ai image HTTP {r.status} model={model_id} "
                                  f"retry={_retry_count} response={_body[:500]}")
                    _err = fal_error(r.status, _body, _name, model_id, prompt=_orig_prompt,
                                     stage="image")
                    # Сбой у самого OpenAI (downstream) — короткий повтор: такие
                    # запросы провайдер не выполнил, второй оплаты нет.
                    if (_err.kind == "overload" and "downstream" in _body.lower()
                            and _retry_count < MAX_DOWNSTREAM_RETRIES):
                        await asyncio.sleep(3 * (_retry_count + 1))
                        return await api_generate_fal_image(
                            _orig_prompt, model_id, aspect_ratio, quality=quality,
                            _retry_count=_retry_count + 1, model_name=model_name)
                    raise _err
                try:
                    data = json.loads(_body)
                except Exception:
                    raise GenError("provider", f"⚠️ <b>{_html.escape(_name)}</b> вернула непонятный "
                                   "ответ.\n\n💡 Попробуй ещё раз или выбери другую модель.",
                                   raw=f"non-JSON: {_body[:400]}", provider="fal.ai", model=model_id)
    except asyncio.TimeoutError:
        raise GenError("timeout", f"⏱ <b>{_html.escape(_name)}</b> не успела за {timeout_sec // 60} мин.\n\n"
                       "💡 Попробуй ещё раз или выбери модель побыстрее.",
                       raw=f"sync timeout {timeout_sec}s", provider="fal.ai", model=model_id)
    except aiohttp.ClientError as _ce:
        raise GenError("overload", f"⚠️ <b>{_html.escape(_name)}</b> сейчас недоступна "
                       "(нет связи с сервером генерации).\n\n💡 Попробуй ещё раз через минуту.",
                       raw=f"{type(_ce).__name__}: {_ce}", provider="fal.ai", model=model_id)

    img_url = _fal_extract_url(data, "image")
    _nsfw = data.get("has_nsfw_concepts") if isinstance(data, dict) else None
    if not img_url or (isinstance(_nsfw, list) and any(_nsfw)):
        logging.warning(f"fal.ai no image. model={model_id} response={str(data)[:500]}")
        raise _fal_no_result_error(data, "image", _name, model_id, _orig_prompt)
    return await _fal_download(img_url, model_name=_name, model_id=model_id, kind="image",
                               timeout_total=120)


# ══════════════════════════════════════════════════════════
#  ВИДЕО — fal.ai (Kling, Seedance, Wan, Grok, Gemini Omni)
# ══════════════════════════════════════════════════════════

def _fal_video_payload(model_id: str, prompt: str, aspect_ratio: str, duration: int,
                       resolution: str = "") -> dict:
    """Payload text-to-video под конкретную модель (по их схемам на fal.ai)."""
    if "kling" in model_id:
        if "v3" in model_id:
            return {"prompt": prompt, "duration": str(duration), "aspect_ratio": aspect_ratio,
                    "generate_audio": True, "negative_prompt": "blur, distort, and low quality",
                    "cfg_scale": 0.5}
        return {"prompt": prompt, "duration": "10" if duration >= 10 else "5",
                "aspect_ratio": aspect_ratio, "negative_prompt": "blur, distort, and low quality",
                "cfg_scale": 0.5}
    if "seedance" in model_id:
        return {"prompt": prompt, "duration": str(duration), "aspect_ratio": aspect_ratio,
                "resolution": resolution or "720p"}
    if "wan" in model_id:
        # Wan 2.2: num_frames 17–161 при 16 кадрах/с. Раньше уходило duration*16
        # (80/160) — схема ждёт 4n+1, поэтому 81 и 161.
        return {"prompt": prompt, "num_frames": 161 if duration >= 10 else 81,
                "aspect_ratio": aspect_ratio, "resolution": resolution or "720p"}
    if "grok-imagine-video" in model_id:
        # duration у Grok — ЧИСЛО (1–15). Раньше уходила строка.
        return {"prompt": prompt, "duration": int(duration), "aspect_ratio": aspect_ratio,
                "resolution": resolution or "720p"}
    if "gemini-omni" in model_id:
        # Gemini Omni Flash: только 16:9 и 9:16, длительность 3–10 сек.
        _ar = aspect_ratio if aspect_ratio in ("16:9", "9:16") else "16:9"
        return {"prompt": prompt, "aspect_ratio": _ar, "resolution": resolution or "720p",
                "duration": max(3, min(10, int(duration or 8)))}
    return {"prompt": prompt, "aspect_ratio": aspect_ratio}


async def api_generate_fal_video(prompt: str, model_id: str, aspect_ratio: str = "16:9",
                                  duration: int = 5, resolution: str = "",
                                  model_name: str = "") -> bytes:
    """Генерация видео через fal.ai queue API (submit → status → result → скачивание)."""
    _name = model_name or _model_name(model_id)
    payload = _fal_video_payload(model_id, prompt, aspect_ratio, duration, resolution)
    logging.info(f"fal.ai video START: model={model_id} duration={duration}s aspect={aspect_ratio} "
                 f"res={resolution or '-'} prompt_len={len(prompt)}")
    # Kling Pro реально долгий — до 25 минут на 10 секунд видео.
    result = await _fal_queue_run(model_id, payload, poll_max=150, model_name=_name, prompt=prompt,
                                  interval=10, first_wait=15, queue_limit_sec=360,
                                  log_tag="fal video")
    request_id = result.get("_request_id", "")
    vid_url = _fal_extract_url(result, "video")
    if not vid_url:
        logging.error(f"fal.ai no video url. keys={list(result.keys())} full={str(result)[:800]}")
        raise _fal_no_result_error(result, "video", _name, model_id, prompt, request_id)
    logging.info(f"fal.ai video URL obtained: {vid_url[:100]} (request_id={request_id})")
    return await _fal_download(vid_url, model_name=_name, model_id=model_id, kind="video",
                               request_id=request_id, min_bytes=10000, timeout_total=300)


# ══════════════════════════════════════════════════════════
#  ИЗОБРАЖЕНИЯ — Google (Nano Banana / Gemini Image)
# ══════════════════════════════════════════════════════════

# Сколько «пикселей» просить у Gemini Image. У Nano Banana Pro 1K и 2K стоят
# одинаково ($0.134), поэтому берём 2K. 4K — $0.24, дороже цены в боте.
_GEMINI_IMAGE_SIZE = {"gemini-3-pro-image": "2K"}


def _gemini_text_parts(data: dict) -> str:
    try:
        _parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", []) or []
        return " ".join(p.get("text", "") for p in _parts if isinstance(p, dict) and p.get("text")).strip()
    except Exception:
        return ""


def _gemini_image_or_error(data: dict, model_name: str, model_id: str, prompt: str) -> bytes:
    """Картинка из ответа generateContent или понятная GenError."""
    for _c in (data.get("candidates") or [])[:1]:
        for _p in (_c.get("content", {}) or {}).get("parts", []) or []:
            if isinstance(_p, dict) and "inlineData" in _p:
                return base64.b64decode(_p["inlineData"]["data"])
    _blk = gemini_block_error(data, model_name, model_id, prompt)
    if _blk:
        raise _blk
    _txt = _gemini_text_parts(data)
    _fin = ""
    try:
        _fin = str((data.get("candidates") or [{}])[0].get("finishReason") or "")
    except Exception:
        pass
    if _txt:
        # Модель ответила текстом вместо картинки — чаще всего промт выглядит
        # как вопрос/инструкция, а не описание сцены. Показываем её ответ.
        raise GenError("no_output",
                       f"🤷 <b>{_html.escape(model_name)}</b> ответила текстом вместо картинки:\n"
                       f"<i>{_html.escape(_txt[:300])}</i>\n\n"
                       "💡 <b>Как исправить:</b> опиши, ЧТО должно быть на изображении — объект, "
                       "стиль, фон, свет. Например: <i>рыжий кот в очках сидит в кафе, тёплый "
                       "свет, реализм</i>.",
                       raw=f"finishReason={_fin} text={_txt[:500]}", provider="Google", model=model_id,
                       hint="Промт похож на вопрос/инструкцию, а не на описание картинки")
    raise GenError("no_output",
                   f"🤷 <b>{_html.escape(model_name)}</b> не вернула картинку (причина: "
                   f"{_html.escape(_fin or 'не указана')}).\n\n💡 Переформулируй промт или выбери "
                   "другую модель.",
                   raw=json.dumps(data, ensure_ascii=False)[:1200], provider="Google", model=model_id)


async def _gemini_generate(s, model_id: str, parts: list, aspect_ratio: str = "",
                           model_name: str = "", prompt: str = "") -> bytes:
    """Один вызов generateContent с картинкой на выходе."""
    _name = model_name or _model_name(model_id)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    _img_cfg = {}
    if aspect_ratio:
        _img_cfg["aspectRatio"] = aspect_ratio
    if _GEMINI_IMAGE_SIZE.get(model_id):
        _img_cfg["imageSize"] = _GEMINI_IMAGE_SIZE[model_id]
    _gen_cfg = {"responseModalities": ["TEXT", "IMAGE"]}
    if _img_cfg:
        _gen_cfg["imageConfig"] = _img_cfg
    payload = {"contents": [{"parts": parts}], "generationConfig": _gen_cfg}
    for _try in range(2):
        async with s.post(url, json=payload, headers=headers) as r:
            _body = await r.text()
            if r.status == 200:
                try:
                    data = json.loads(_body)
                except Exception:
                    raise GenError("provider", f"⚠️ <b>{_html.escape(_name)}</b> вернула непонятный ответ.",
                                   raw=_body[:500], provider="Google", model=model_id)
                return _gemini_image_or_error(data, _name, model_id, prompt)
            # Модель не знает imageSize — повторяем без него (одна попытка).
            if (r.status == 400 and _try == 0 and "imageSize" in _img_cfg
                    and ("image_size" in _body.lower() or "imagesize" in _body.lower())):
                logging.warning(f"Gemini {model_id}: imageSize не принят, повторяю без него")
                _img_cfg.pop("imageSize", None)
                if not _img_cfg:
                    _gen_cfg.pop("imageConfig", None)
                continue
            logging.warning(f"Gemini image HTTP {r.status} model={model_id}: {_body[:400]}")
            raise google_http_error(r.status, _body, _name, model_id, prompt)
    raise GenError("params", f"⚠️ <b>{_html.escape(_name)}</b> не приняла параметры запроса.",
                   raw="imageSize retry exhausted", provider="Google", model=model_id)


async def api_generate_image(prompt: str, model_id: str, aspect_ratio: str = "1:1",
                              api_type: str = "gemini", quality: str = "medium",
                              model_name: str = "") -> bytes:
    if api_type == "fal":
        return await api_generate_fal_image(prompt, model_id, aspect_ratio, quality=quality,
                                            model_name=model_name)
    if api_type == "imagen":
        # Imagen 4 Google отключил 17.08.2026 (страница deprecations Gemini API).
        # Ключи img_* оставлены только ради истории и статистики и всегда выключены.
        raise GenError("provider", "⚠️ Эта модель больше не поддерживается Google.\n\n"
                       "💡 Выбери Nano Banana, GPT Image или Flux.",
                       raw=f"retired model {model_id} (Imagen shutdown 2026-08-17)",
                       provider="Google", model=model_id)
    _name = model_name or _model_name(model_id)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240, sock_connect=30,
                                                                       sock_read=200)) as s:
            return await _gemini_generate(s, model_id, [{"text": prompt}], aspect_ratio,
                                          model_name=_name, prompt=prompt)
    except asyncio.TimeoutError:
        raise GenError("timeout", f"⏱ <b>{_html.escape(_name)}</b> не успела за 4 минуты.\n\n"
                       "💡 Попробуй ещё раз или выбери другую модель.",
                       raw="gemini image timeout 240s", provider="Google", model=model_id)
    except aiohttp.ClientError as _ce:
        raise GenError("overload", f"⚠️ <b>{_html.escape(_name)}</b> сейчас недоступна.\n\n"
                       "💡 Попробуй ещё раз через минуту.",
                       raw=f"{type(_ce).__name__}: {_ce}", provider="Google", model=model_id)


# Редактирование через Gemini: основная модель и резервы (только при НЕДОСТУПНОСТИ
# предыдущей — 404/перегрузка; отказ по содержанию резервами не обходим).
# Preview-модели gemini-3.1-flash-image-preview и gemini-3-pro-image-preview
# Google отключил 25.06.2026 — раньше они стояли здесь и давали только 404.
EDIT_GEMINI_MODELS = [
    "gemini-2.5-flash-image",      # Nano Banana — основная (до 15.03.2027)
    "gemini-nano-banana-2.1",      # Nano Banana 2.1 — GA с 06.10.2026
]


async def api_edit_image(image_bytes: bytes, prompt: str, aspect_ratio: str = "1:1",
                         model_name: str = "Nano Banana") -> bytes:
    """Редактирование фото по референсу через Gemini (Nano Banana)."""
    parts = [
        {"inlineData": {"mimeType": _mime_of(image_bytes),
                        "data": base64.b64encode(image_bytes).decode()}},
        {"text": prompt},
    ]
    last_err = None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240, sock_connect=30,
                                                                   sock_read=200)) as s:
        for model in EDIT_GEMINI_MODELS:
            for attempt in range(2):
                try:
                    return await _gemini_generate(s, model, parts, "", model_name=model_name,
                                                  prompt=prompt)
                except GenError as ge:
                    last_err = ge
                    # Отказ по содержанию / фото / промту — другие модели Google
                    # применят тот же фильтр, а каждая попытка стоит денег.
                    if ge.kind in ("safety", "input", "no_output", "params", "auth"):
                        raise
                    if ge.kind in ("overload", "rate") and attempt == 0:
                        await asyncio.sleep(4)
                        continue
                    break  # 404/иная ошибка провайдера — следующая модель
                except (aiohttp.ClientError, asyncio.TimeoutError) as ne:
                    last_err = GenError("overload", f"⚠️ <b>{_html.escape(model_name)}</b> сейчас "
                                        "недоступна.\n\n💡 Попробуй ещё раз через минуту.",
                                        raw=f"{model}: {type(ne).__name__}: {ne}",
                                        provider="Google", model=model)
                    break
    raise last_err or GenError("provider", "⚠️ Редактирование сейчас недоступно. Попробуй позже.",
                               raw="no edit models", provider="Google", model="gemini")


async def api_fal_edit_image(image_bytes: bytes, prompt: str, model_id: str,
                             model_name: str = "") -> bytes:
    """Редактирование фото через fal.ai (Grok / GPT Image / Flux Kontext)."""
    _name = model_name or _model_name(model_id)
    data_uri = _data_uri(image_bytes)
    if "gpt-image" in model_id:
        # GPT Image 2 edit: image_urls (массив). Качество по умолчанию у fal —
        # "high" (самое дорогое, дороже нашей цены) — задаём medium явно.
        payload = {"prompt": prompt, "image_urls": [data_uri], "quality": "medium"}
    else:
        payload = {"prompt": prompt, "image_url": data_uri}
    result = await _fal_queue_run(model_id, payload, poll_max=72, model_name=_name,
                                  prompt=prompt, interval=5, log_tag="fal edit")
    out_url = _fal_extract_url(result, "image")
    _nsfw = result.get("has_nsfw_concepts")
    if not out_url or (isinstance(_nsfw, list) and any(_nsfw)):
        raise _fal_no_result_error(result, "image", _name, model_id, prompt,
                                   result.get("_request_id", ""))
    return await _fal_download(out_url, model_name=_name, model_id=model_id, kind="image",
                               request_id=result.get("_request_id", ""), timeout_total=120)


def _fal_anim_payload(model_id: str, image_uri: str, prompt: str, duration: int,
                      aspect_ratio: str, last_uri: str = "", resolution: str = "") -> dict:
    """Payload image-to-video под конкретную модель.

    Раньше всем моделям уходило одно и то же: duration числом. Kling ждёт
    строку "5"/"10", Wan — num_frames вместо duration, Grok — число.
    """
    p = {"image_url": image_uri, "prompt": prompt}
    if "kling" in model_id:
        p["duration"] = "10" if int(duration) >= 10 else "5"
        if last_uri:
            p["tail_image_url"] = last_uri
    elif "wan" in model_id:
        p["num_frames"] = 161 if int(duration) >= 10 else 81
        p["resolution"] = resolution or "720p"
    elif "grok-imagine-video" in model_id:
        p["duration"] = int(duration)
        p["resolution"] = resolution or "720p"
    elif "gemini-omni" in model_id:
        p["duration"] = max(3, min(10, int(duration or 8)))
        p["resolution"] = resolution or "720p"
        p["aspect_ratio"] = aspect_ratio if aspect_ratio in ("16:9", "9:16") else "16:9"
        if last_uri:
            p["end_image_url"] = last_uri
    else:
        p["duration"] = str(duration)
        p["aspect_ratio"] = aspect_ratio
    return p


async def api_fal_animate_image(image_bytes: bytes, prompt: str, model_id: str,
                                 duration: int = 5, aspect_ratio: str = "16:9",
                                 last_bytes: bytes | None = None, resolution: str = "",
                                 model_name: str = "") -> bytes:
    """Анимация фото через fal.ai image-to-video (Gemini Omni / Grok / Kling / Wan)."""
    _name = model_name or _model_name(model_id)
    payload = _fal_anim_payload(model_id, _data_uri(image_bytes), prompt, duration, aspect_ratio,
                                _data_uri(last_bytes) if last_bytes else "", resolution)
    result = await _fal_queue_run(model_id, payload, poll_max=150, model_name=_name, prompt=prompt,
                                  interval=8, first_wait=10, queue_limit_sec=360,
                                  log_tag="fal anim")
    video_url = _fal_extract_url(result, "video")
    if not video_url:
        raise _fal_no_result_error(result, "video", _name, model_id, prompt,
                                   result.get("_request_id", ""))
    return await _fal_download(video_url, model_name=_name, model_id=model_id, kind="video",
                               request_id=result.get("_request_id", ""), min_bytes=10000,
                               timeout_total=300)


# ══════════════════════════════════════════════════════════
#  ВИДЕО — Google Veo 3.1 (РЕЗЕРВ до 22.10.2026)
# ══════════════════════════════════════════════════════════

def _veo_filtered_error(pd: dict, model_name: str, model_id: str, prompt: str):
    """Отказ Veo по содержанию: Google кладёт причину в raiMediaFilteredReasons."""
    _resp = (pd.get("response") or {}) if isinstance(pd, dict) else {}
    _gvr = _resp.get("generateVideoResponse") or {}
    _reasons = _gvr.get("raiMediaFilteredReasons") or _resp.get("raiMediaFilteredReasons") or []
    _cnt = _gvr.get("raiMediaFilteredCount") or _resp.get("raiMediaFilteredCount")
    if _reasons or _cnt:
        _txt = " | ".join(str(x) for x in _reasons) if isinstance(_reasons, list) else str(_reasons)
        return safety_error(model_name, raw=f"raiMediaFilteredReasons: {_txt}", provider="Google",
                            model=model_id, prompt=prompt)
    _low = json.dumps(pd, ensure_ascii=False).lower()
    if "filtered" in _low or "safety" in _low or "blocked" in _low:
        return safety_error(model_name, raw=json.dumps(pd, ensure_ascii=False)[:800],
                            provider="Google", model=model_id, prompt=prompt)
    return None


async def _veo_run(instance: dict, model_id: str, aspect_ratio: str, resolution: str,
                   model_name: str, prompt: str) -> bytes:
    base = "https://generativelanguage.googleapis.com/v1beta"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    params = {"durationSeconds": 8, "sampleCount": 1,
              "aspectRatio": aspect_ratio if aspect_ratio in ("16:9", "9:16") else "16:9"}
    if resolution in ("720p", "1080p"):
        params["resolution"] = resolution
    payload = {"instances": [instance], "parameters": params}
    async with aiohttp.ClientSession(timeout=_LONG_TIMEOUT) as s:
        async with s.post(f"{base}/models/{model_id}:predictLongRunning",
                          json=payload, headers=headers) as r:
            _body = await r.text()
            if r.status != 200:
                logging.warning(f"Veo submit HTTP {r.status} model={model_id}: {_body[:400]}")
                raise google_http_error(r.status, _body, model_name, model_id, prompt)
            op_name = json.loads(_body).get("name")
            if not op_name:
                raise GenError("provider", f"⚠️ <b>{_html.escape(model_name)}</b> не приняла задачу.",
                               raw=_body[:500], provider="Google", model=model_id)
            logging.info(f"Veo operation started: {op_name}")
        # Ожидание до 10 минут (раньше 6 — 1080p в часы пик не успевал).
        for _ in range(120):
            await asyncio.sleep(5)
            try:
                async with s.get(f"{base}/{op_name}", headers=headers) as pr:
                    if pr.status != 200:
                        continue
                    pd = await pr.json()
            except Exception as _pe:
                logging.warning(f"Veo poll error: {_pe}")
                continue
            if not pd.get("done"):
                continue
            if "error" in pd:
                _e = pd.get("error") or {}
                raise google_http_error(int(_e.get("code") or 500), json.dumps(_e, ensure_ascii=False),
                                        model_name, model_id, prompt)
            _resp = pd.get("response", {}) or {}
            _samples = (_resp.get("generateVideoResponse", {}) or {}).get("generatedSamples") or []
            _preds = _resp.get("predictions") or []
            for _v in ([x.get("video", x) for x in _samples] + list(_preds)):
                if not isinstance(_v, dict):
                    continue
                if _v.get("bytesBase64Encoded"):
                    return base64.b64decode(_v["bytesBase64Encoded"])
                uri = _v.get("uri") or _v.get("videoUri") or _v.get("gcsUri")
                if uri:
                    if uri.startswith("gs://"):
                        uri = uri.replace("gs://", "https://storage.googleapis.com/")
                    async with s.get(uri, headers={"x-goog-api-key": GEMINI_API_KEY}) as vr:
                        _vb = await vr.read()
                        if vr.status == 200 and len(_vb) > 10000:
                            return _vb
                        raise GenError("provider", "⚠️ Видео создано, но не скачалось. Попробуй ещё раз.",
                                       raw=f"Veo download HTTP {vr.status} {len(_vb)}b",
                                       provider="Google", model=model_id)
            _blk = _veo_filtered_error(pd, model_name, model_id, prompt)
            if _blk:
                raise _blk
            raise GenError("no_output", f"🤷 <b>{_html.escape(model_name)}</b> не вернула видео.\n\n"
                           "💡 Переформулируй промт или выбери другую модель.",
                           raw=json.dumps(pd, ensure_ascii=False)[:1200], provider="Google",
                           model=model_id)
    raise GenError("timeout", f"⏱ <b>{_html.escape(model_name)}</b> не успела за 10 минут.\n\n"
                   "💡 Попробуй ещё раз или выбери другую модель.",
                   raw=f"Veo poll timeout {op_name}", provider="Google", model=model_id)


async def api_animate_image(
    first_bytes: bytes,
    prompt: str,
    aspect_ratio: str = "16:9",
    last_bytes: bytes | None = None,
    model_id: str = "veo-3.1-fast-generate-preview",
    resolution: str = "720p",
    model_name: str = "Veo 3.1",
) -> bytes:
    """Анимация фото через Veo 3.1 — РЕЗЕРВ на случай сбоя Gemini Omni (до 22.10.2026)."""
    instance = {
        "prompt": prompt,
        "image": {"bytesBase64Encoded": base64.b64encode(first_bytes).decode(),
                  "mimeType": _mime_of(first_bytes)},
    }
    if last_bytes:
        instance["lastFrame"] = {"bytesBase64Encoded": base64.b64encode(last_bytes).decode(),
                                 "mimeType": _mime_of(last_bytes)}
    return await _veo_run(instance, model_id, aspect_ratio, resolution, model_name, prompt)


def _clean_name(m: dict) -> str:
    return re.sub(r"^[^\w]+\s*", "", str(m.get("name") or "")).strip() or "Модель"


async def api_animate_by_model(m: dict, first_bytes: bytes, prompt: str,
                               aspect_ratio: str = "16:9",
                               last_bytes: bytes | None = None) -> bytes:
    """Анимация фото по описанию модели из ANIM_MODELS (бот и мини-апп — один путь).

    Для Gemini Omni при сбое НОВОГО эндпоинта (не при отказе по содержанию)
    уходим на Veo, пока Google его не отключил (22.10.2026).
    """
    _name = _clean_name(m)
    _last = last_bytes if m.get("two_frames") else None
    if m.get("api") == "veo_anim":
        return await api_animate_image(first_bytes, prompt, aspect_ratio, _last,
                                       model_id=m.get("model_id") or "veo-3.1-fast-generate-preview",
                                       resolution=m.get("resolution") or "720p", model_name=_name)
    try:
        return await api_fal_animate_image(first_bytes, prompt, m["model_id"],
                                           duration=int(m.get("duration", 5) or 5),
                                           aspect_ratio=aspect_ratio, last_bytes=_last,
                                           resolution=m.get("resolution", ""), model_name=_name)
    except GenError as ge:
        _fb = m.get("fallback_model_id")
        if _fb and _veo_alive() and ge.kind in ("params", "provider", "auth", "balance", "overload"):
            logging.warning(f"anim {m.get('model_id')} → резерв Veo {_fb}: {ge.kind} {ge.raw[:200]}")
            try:
                from common import notify_admin_error as _nae
                await _nae(f"Анимация: {m.get('model_id')} не сработала — ушёл на резерв {_fb}", ge)
            except Exception:
                pass
            return await api_animate_image(first_bytes, prompt, aspect_ratio, _last, model_id=_fb,
                                           resolution="1080p" if m.get("resolution") in ("1080p", "4k") else "720p",
                                           model_name=_name)
        raise


async def api_edit_by_model(m: dict, image_bytes: bytes, prompt: str) -> bytes:
    """Редактирование фото по описанию модели из EDIT_MODELS (бот и мини-апп)."""
    _name = _clean_name(m)
    if m.get("api") == "gemini":
        return await api_edit_image(image_bytes, prompt, model_name=_name)
    return await api_fal_edit_image(image_bytes, prompt, m["model_id"], model_name=_name)


# ══════════════════════════════════════════════════════════
#  EVOLINK - Kling Motion Control
# ══════════════════════════════════════════════════════════

async def _tg_file_public_url(file_id: str) -> str:
    """УСТАРЕЛО, НЕ ИСПОЛЬЗОВАТЬ для внешних сервисов.

    Возвращает ссылку Telegram, в которой содержится ТОКЕН БОТА. Передавать её
    наружу нельзя: токен = полный доступ к боту (рассылка по всей базе, перехват
    апдейтов). Для сторонних API используй common.tg_file_proxy_url() — она
    отдаёт файл с нашего домена по одноразовому токену.
    """
    file = await bot.get_file(file_id)
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"


async def api_kling_motion_control(
    image_url: str,
    video_url: str,
    duration: int = 8,
    prompt: str = "",
    aspect_ratio: str = "16:9",
) -> bytes:
    """Генерирует видео через Kling Motion Control на EvoLink.
    
    Args:
        image_url: публичный URL референс-фото (персонаж)
        video_url: публичный URL референс-видео (движение/эмоции)
        duration: длительность видео в секундах (5, 8 или 10)
        prompt: опциональный промт для описания сцены/фона
        aspect_ratio: соотношение сторон ('16:9', '9:16', '1:1')
    
    Returns: bytes готового видео (mp4)
    """
    if not EVOLINK_API_KEY:
        raise Exception("EvoLink API key not configured. Свяжись с админом.")

    base = "https://api.evolink.ai/v1"
    headers = {
        "Authorization": f"Bearer {EVOLINK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MOTION_MODEL_ID,
        "image_url": image_url,
        "video_url": video_url,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "quality": "720p",  # 720p дешевле и достаточно качественно
        # EvoLink требует motion-control-специфичные параметры в model_params
        "model_params": {
            "character_orientation": "video",  # персонаж смотрит как в референс-видео
        },
    }
    if prompt.strip():
        payload["prompt"] = prompt.strip()[:2500]

    async with aiohttp.ClientSession(timeout=_LONG_TIMEOUT) as s:
        # 1. Отправляем задачу
        async with s.post(f"{base}/videos/generations", json=payload, headers=headers) as r:
            if r.status != 200 and r.status != 202:
                err_text = (await r.text())[:600]
                logging.warning(
                    f"EvoLink Motion Control ERROR status={r.status} body={err_text}"
                )
                low = err_text.lower()
                # Safety блоки (контент)
                if r.status == 400 and ("safety" in low or "blocked" in low or "policy" in low):
                    raise safety_error("Kling Motion Control", raw=f"HTTP 400 {err_text}",
                                       provider="EvoLink", model=MOTION_MODEL_ID, prompt=prompt)
                # Только жёсткий индикатор нехватки баланса: HTTP 402 или явная фраза
                if r.status == 402:
                    raise Exception(f"EvoLink: баланс исчерпан (HTTP 402). Детали: {err_text[:200]}")
                if ("insufficient_balance" in low or "insufficient balance" in low
                    or "balance_insufficient" in low or "not enough balance" in low):
                    raise Exception(f"EvoLink: баланс исчерпан. Детали: {err_text[:200]}")
                # Все остальные ошибки показываем админу с реальным текстом
                raise Exception(f"EvoLink API {r.status}: {err_text}")
            resp_data = await r.json()
            task_id = resp_data.get("task_id") or resp_data.get("id")
            if not task_id:
                raise Exception(f"EvoLink: нет task_id в ответе: {str(resp_data)[:300]}")
            logging.info(f"Kling Motion Control task started: {task_id}")

        # 2. Polling - Motion Control обычно 2-5 минут, но может занимать до 15 при нагрузке
        # 240 попыток × 5 сек = 20 минут максимум
        last_status = None
        last_response = None
        for attempt in range(240):
            await asyncio.sleep(5)
            try:
                async with s.get(f"{base}/tasks/{task_id}", headers=headers) as pr:
                    if pr.status != 200:
                        if attempt % 6 == 0:  # логируем раз в 30 сек
                            logging.warning(f"Kling poll {task_id} status={pr.status}")
                        continue
                    try:
                        pd = await pr.json()
                    except (aiohttp.ContentTypeError, Exception) as je:
                        logging.warning(f"Kling poll json error attempt={attempt}: {je}")
                        continue
                    last_response = pd
            except Exception as pe:
                logging.warning(f"Kling poll exception attempt={attempt}: {pe}")
                continue

            # Статус может быть в разных местах
            status_raw = (
                pd.get("status")
                or pd.get("task_status")
                or (pd.get("task_info", {}) or {}).get("status")
                or (pd.get("data", {}) or {}).get("status")
                or ""
            )
            status = str(status_raw).lower()

            if status != last_status:
                logging.info(f"Kling task {task_id} status: {status_raw} (attempt {attempt+1})")
                last_status = status

            # ── Успешные статусы (расширенный список)
            if status in ("completed", "complete", "success", "succeed", "succeeded",
                          "finished", "done", "ready"):
                # Рекурсивный поиск URL видео по всему JSON-ответу.
                # Ищем поля со словами video/url/resource, значение которых - строка-URL на .mp4/.mov/.webm
                # или содержит video/mp4 в самом URL.
                found_urls = []

                def _find_video_urls(obj, path=""):
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            _find_video_urls(v, f"{path}.{k}")
                    elif isinstance(obj, list):
                        for i, item in enumerate(obj):
                            _find_video_urls(item, f"{path}[{i}]")
                    elif isinstance(obj, str) and obj.startswith("http"):
                        low = obj.lower()
                        # URL считаем видео если: в пути/расширении есть video/mp4/mov
                        # или в названии поля было "video" или "url" или "resource"
                        path_low = path.lower()
                        is_video = (
                            ".mp4" in low or ".mov" in low or ".webm" in low
                            or "/video" in low or "video_url" in low
                            or "video" in path_low or "url" in path_low or "resource" in path_low
                        )
                        # Отсекаем превью/обложки
                        is_cover = "cover" in path_low or "thumbnail" in path_low or "preview" in path_low or ".jpg" in low or ".png" in low
                        if is_video and not is_cover:
                            found_urls.append((path, obj))

                _find_video_urls(pd)

                # Сортируем: приоритет - без watermark
                found_urls.sort(key=lambda x: (0 if "without_watermark" in x[0].lower() else 1))

                video_url_out = found_urls[0][1] if found_urls else None

                if not video_url_out:
                    logging.error(f"Kling completed but no video URL. Full response: {str(pd)}")
                    # Для админа - полный JSON для диагностики
                    raise Exception(
                        f"EvoLink: задача завершена, но не нашёл URL видео. "
                        f"Полный ответ: {str(pd)[:2000]}"
                    )

                logging.info(
                    f"Kling task {task_id} DONE. Found {len(found_urls)} URL(s), "
                    f"using: {found_urls[0][0]} = {video_url_out}"
                )
                # Скачиваем результат
                async with s.get(video_url_out) as vr:
                    data_bytes = await vr.read()
                    if len(data_bytes) < 1000:
                        raise Exception(f"Получен слишком маленький файл ({len(data_bytes)} байт)")
                    return data_bytes

            # ── Ошибочные статусы
            if status in ("failed", "error", "cancelled", "canceled", "rejected"):
                err = pd.get("error", {})
                err_msg = err.get("message", "") if isinstance(err, dict) else str(err)
                low = err_msg.lower()
                if "safety" in low or "blocked" in low or "policy" in low:
                    raise safety_error("Kling Motion Control", raw=err_msg, provider="EvoLink",
                                       model=MOTION_MODEL_ID, prompt=prompt)
                raise Exception(f"Kling Motion Control: {err_msg or status or 'неизвестная ошибка'}")
            # pending / queued / generating / processing / running - продолжаем ждать

        # Таймаут - показываем последний известный статус для диагностики
        logging.error(f"Kling timeout task={task_id}, last_status={last_status}, last_response={str(last_response)[:500]}")
        raise Exception(
            f"Превышено время ожидания (20 мин). Последний статус: {last_status}. "
            f"Задача могла завершиться на стороне EvoLink - проверь в их логах. Кредиты возвращены."
        )


async def api_generate_video(prompt: str, model_id: str, aspect_ratio: str = "16:9",
                              api_type: str = "veo", duration: int = 8, resolution: str = "",
                              model_name: str = "", fallback_model_id: str = "") -> bytes:
    """Генерация видео. api_type="fal" — fal.ai (Gemini Omni, Kling, Seedance, Wan, Grok);
    "veo" — Google Veo 3.1 напрямую (до 22.10.2026).

    fallback_model_id — Veo-модель, на которую уходим, если НОВЫЙ эндпоинт на
    fal.ai не ответил как надо (404 / ошибка параметров / недоступность).
    Отказ по содержанию резервом не обходим: правила у Google одни.
    """
    _name = model_name or _model_name(model_id)
    if api_type == "fal":
        try:
            return await api_generate_fal_video(prompt, model_id, aspect_ratio, duration,
                                                resolution=resolution, model_name=_name)
        except GenError as ge:
            if (fallback_model_id and _veo_alive()
                    and ge.kind in ("params", "provider", "auth", "balance", "overload")):
                logging.warning(f"video {model_id} → резерв Veo {fallback_model_id}: {ge.kind} {ge.raw[:200]}")
                try:
                    from common import notify_admin_error as _nae
                    await _nae(f"Видео: {model_id} не сработала — ушёл на резерв {fallback_model_id}", ge)
                except Exception:
                    pass
                return await _veo_run({"prompt": prompt}, fallback_model_id, aspect_ratio,
                                      resolution if resolution in ("720p", "1080p") else "1080p",
                                      _name, prompt)
            raise
    return await _veo_run({"prompt": prompt}, model_id, aspect_ratio, resolution, _name, prompt)

# ══════════════════════════════════════════════════════════
#  ОБРАБОТЧИКИ - СТАРТ / МЕНЮ
# ══════════════════════════════════════════════════════════

