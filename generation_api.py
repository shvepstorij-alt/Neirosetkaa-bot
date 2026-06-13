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

def _is_retryable_error(exc: Exception) -> bool:
    """Проверяет, стоит ли повторять запрос при этой ошибке."""
    msg = str(exc).lower()
    # Явные HTTP-статусы в тексте
    for code in _RETRY_STATUSES:
        if f" {code}:" in msg or f" {code} " in msg:
            return True
    # Ключевые слова временных ошибок
    triggers = [
        "rate limit", "timeout", "timed out", "temporarily",
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


async def api_generate_fal_image(prompt: str, model_id: str, aspect_ratio: str = "1:1",
                                  quality: str = "medium", _retry_count: int = 0) -> bytes:
    """Генерация изображений через fal.ai (Flux 2 Pro, Ideogram V3, GPT Image 2).
    Использует sync endpoint - результат приходит сразу.
    
    quality: для GPT Image 2 - 'low' / 'medium' / 'high'. Для остальных игнорируется.
    _retry_count: счётчик повторных попыток (внутренний параметр для retry при downstream errors)
    """
    MAX_DOWNSTREAM_RETRIES = 2  # Всего 3 попытки (0, 1, 2) - начальная + 2 retry
    if not FAL_API_KEY:
        raise Exception("FAL_API_KEY не задан. Добавь переменную в Railway.")

    # Санитизация промта для GPT Image 2 - OpenAI плохо обрабатывает
    # некоторые спецсимволы (ёлочки «», длинные тире, множественные переносы)
    if "gpt-image-2" in model_id:
        import re as _re
        # Заменяем ёлочки «» на обычные кавычки ""
        prompt = prompt.replace('«', '"').replace('»', '"')
        # Заменяем фигурные кавычки на обычные
        prompt = prompt.replace('"', '"').replace('"', '"').replace(''', "'").replace(''', "'")
        # Множественные переносы → один перенос
        prompt = _re.sub(r'\n{2,}', '\n', prompt)
        # Убираем ведущие/trailing пробелы на каждой строке
        prompt = '\n'.join(line.strip() for line in prompt.split('\n'))
        # Убираем пустые строки и лишние пробелы
        prompt = _re.sub(r' {2,}', ' ', prompt).strip()

    # Маппинг aspect_ratio для разных моделей
    aspect_map_flux = {
        "1:1": "square_hd",
        "16:9": "landscape_16_9",
        "9:16": "portrait_16_9",
        "4:3": "landscape_4_3",
        "3:4": "portrait_4_3",
    }

    url = f"https://fal.run/{model_id}"
    headers = {
        "Authorization": f"Key {FAL_API_KEY}",
        "Content-Type": "application/json",
    }

    # Разные payload под разные модели
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
        # GPT Image 2 через fal.ai - поддерживает все форматы
        # ВАЖНО: fal.ai принимает image_size как предустановленные значения
        # (те же что у Flux): square_hd/square/portrait_4_3/portrait_16_9/landscape_4_3/landscape_16_9
        size_map_gptimg = {
            "1:1":  "square_hd",         # 1024x1024 квадрат
            "16:9": "landscape_16_9",    # горизонтальный
            "9:16": "portrait_16_9",     # вертикальный (сторис/Reels)
            "4:3":  "landscape_4_3",     # классическое фото
            "3:4":  "portrait_4_3",      # портрет
        }
        payload = {
            "prompt": prompt,
            "image_size": size_map_gptimg.get(aspect_ratio, "square_hd"),
            "quality": quality if quality in ("low", "medium", "high") else "medium",
            "num_images": 1,
        }
        logging.info(f"GPT Image 2 payload: model={model_id} quality={quality} aspect={aspect_ratio} size={payload['image_size']}")
    elif "grok-imagine-image" in model_id:
        payload = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "num_images": 1,
        }
        # Grok Imagine Pro использует quality mode
        if quality == "quality":
            payload["mode"] = "quality"
    else:
        payload = {"prompt": prompt}

    # GPT Image 2 high quality + thinking mode может занять до 60 сек - увеличим timeout
    timeout_sec = 300 if "gpt-image-2" in model_id and quality == "high" else 180
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(url, json=payload, headers=headers) as r:
            if r.status == 401 or r.status == 403:
                raise Exception("FAL_API_KEY недействителен. Проверь ключ в Railway Variables.")
            if r.status == 422:
                err_text = (await r.text())[:500]
                err_lower = err_text.lower()
                # 422 может быть: safety, validation error, bad params
                if ("safety" in err_lower or "moderation" in err_lower or
                    "policy" in err_lower or "nsfw" in err_lower or "violat" in err_lower or
                    "inappropriate" in err_lower or "blocked" in err_lower or
                    "content_policy_violation" in err_lower or "flagged by a content" in err_lower):
                    logging.warning(f"fal.ai safety block for model={model_id}: {err_text}")
                    raise Exception(
                        "🛡 Контент заблокирован моделью.\n\n"
                        "Модель отклонила запрос по правилам безопасности.\n"
                        "Попробуй переформулировать - избегай NSFW, насилия или знаменитостей."
                    )
                # Иначе - валидационная ошибка: логируем полный текст для админа
                logging.error(f"fal.ai 422 (validation) model={model_id} payload={payload} response={err_text}")
                raise Exception(
                    f"⚠️ Ошибка параметров модели. Попробуй другой формат или напиши @neirosetkaalex."
                )
            if r.status != 200:
                # Детальное логирование любой другой ошибки
                err_text = (await r.text())[:500]
                logging.error(f"fal.ai HTTP {r.status} model={model_id} payload={payload} response={err_text} retry={_retry_count}")

                # Особая обработка downstream_service_error
                # Это сбой на стороне underlying модели (OpenAI для GPT Image 2)
                err_lower = err_text.lower()
                is_downstream = ("downstream_service_error" in err_lower or
                                "downstream service error" in err_lower)

                if is_downstream and "gpt-image-2" in model_id:
                    # Retry - OpenAI иногда временно глючит, особенно с новой моделью GPT Image 2
                    if _retry_count < MAX_DOWNSTREAM_RETRIES:
                        wait_sec = 3 * (_retry_count + 1)  # 3s, 6s
                        logging.info(f"GPT Image 2 downstream error - retry #{_retry_count + 1} через {wait_sec}с")
                        await asyncio.sleep(wait_sec)
                        return await api_generate_fal_image(
                            prompt, model_id, aspect_ratio,
                            quality=quality,
                            _retry_count=_retry_count + 1,
                        )
                    # Все 3 попытки упали - даём честное объяснение клиенту
                    raise Exception(
                        "⚠️ OpenAI GPT Image 2 сейчас нестабилен (модель вышла 21 апреля - возможны сбои).\n\n"
                        "Попробуй альтернативу:\n"
                        "• 🍌 <b>Nano Banana Pro</b> (30 кр) - 4K от Google\n"
                        "• ✒️ <b>Ideogram V3</b> (14 кр) - отличный текст в картинке\n"
                        "• 🎭 <b>Flux 2 Pro</b> (12 кр) - фотореализм\n\n"
                        "Или повтори попытку через пару минут 🙏"
                    )

                if is_downstream:
                    # Для не-GPT моделей - просто сообщаем
                    raise Exception(
                        "⚠️ Модель временно недоступна. Попробуй через минуту или выбери другую модель."
                    )

                raise Exception(f"fal.ai API {r.status}: {err_text[:200]}")
            if r.status != 200:
                raise Exception(f"fal.ai API {r.status}: {(await r.text())[:300]}")
            if r.content_type and "json" in r.content_type:
                data = await r.json()
            else:
                raw = await r.text()
                raise Exception(f"fal.ai image non-JSON: {raw[:200]}")

            # Ищем URL картинки в ответе
            images = data.get("images", [])
            if not images:
                logging.warning(f"fal.ai no images. model={model_id} response={str(data)[:500]}")
                raise Exception("Модель не вернула изображение. Попробуй другой промт.")

            img_url = images[0].get("url") if isinstance(images[0], dict) else images[0]
            if not img_url:
                raise Exception("Пустой URL изображения от fal.ai")

            # Скачиваем картинку
            async with s.get(img_url) as img_r:
                if img_r.status != 200:
                    raise Exception(f"Не удалось скачать картинку: HTTP {img_r.status}")
                img_bytes = await img_r.read()
                if len(img_bytes) < 1000:
                    raise Exception(f"Картинка слишком маленькая ({len(img_bytes)} bytes)")
                return img_bytes


async def api_generate_fal_video(prompt: str, model_id: str, aspect_ratio: str = "16:9",
                                  duration: int = 5) -> bytes:
    """Генерация видео через fal.ai (Kling 2.5 Turbo Pro, Kling 3.0 Pro).
    Использует queue API с polling - аналогично Veo.
    duration - длина видео в секундах (5/10 для Turbo, 5/8/10 для Pro)."""
    if not FAL_API_KEY:
        raise Exception("FAL_API_KEY не задан. Добавь переменную в Railway.")

    logging.info(f"fal.ai START: model={model_id} duration={duration}s aspect={aspect_ratio} prompt_len={len(prompt)}")

    headers = {
        "Authorization": f"Key {FAL_API_KEY}",
        "Content-Type": "application/json",
    }

    # Payload под разные модели
    if "kling" in model_id:
        if "v3" in model_id:
            payload = {
                "prompt": prompt,
                "duration": str(duration),
                "aspect_ratio": aspect_ratio,
                "generate_audio": True,
                "negative_prompt": "blur, distort, and low quality",
                "cfg_scale": 0.5,
            }
        else:
            safe_duration = "10" if duration >= 10 else "5"
            payload = {
                "prompt": prompt,
                "duration": safe_duration,
                "aspect_ratio": aspect_ratio,
                "negative_prompt": "blur, distort, and low quality",
                "cfg_scale": 0.5,
            }
    elif "seedance" in model_id:
        payload = {
            "prompt": prompt,
            "duration": str(duration),
            "aspect_ratio": aspect_ratio,
            "resolution": "720p",
        }
    elif "wan" in model_id:
        safe_duration = min(duration, 10)
        payload = {
            "prompt": prompt,
            "num_frames": safe_duration * 16,
            "aspect_ratio": aspect_ratio,
            "resolution": "720p",
        }
    elif "grok-imagine-video" in model_id:
        payload = {
            "prompt": prompt,
            "duration": str(duration),
            "aspect_ratio": aspect_ratio,
            "resolution": "720p",
        }
    else:
        payload = {"prompt": prompt, "aspect_ratio": aspect_ratio}

    queue_url = f"https://queue.fal.run/{model_id}"

    # Kling Pro реально долгий - до 25 минут на 5-10 секунд видео
    timeout = aiohttp.ClientTimeout(total=1700)  # 28 минут запас на скачивание
    async with aiohttp.ClientSession(timeout=timeout) as s:
        # 1. Ставим задачу в очередь
        async with s.post(queue_url, json=payload, headers=headers) as r:
            if r.status == 401 or r.status == 403:
                raise Exception("FAL_API_KEY недействителен. Проверь ключ в Railway Variables.")
            if r.status == 422:
                raise Exception(
                    "Промт заблокирован фильтром безопасности 🛡\n"
                    "Переформулируй - избегай сцен с насилием, NSFW или знаменитостями."
                )
            if r.status not in (200, 202):
                raise Exception(f"fal.ai queue API {r.status}: {(await r.text())[:300]}")
            submit_data = await r.json()
            request_id = submit_data.get("request_id")
            if not request_id:
                raise Exception(f"fal.ai не вернул request_id: {str(submit_data)[:200]}")

            # КРИТИЧНО: берём URL-ы напрямую из ответа fal.ai.
            # Они сами точно знают свой формат, а мы угадывать не будем.
            # Документация: fal.ai возвращает status_url и response_url в submit-ответе.
            status_url = submit_data.get("status_url") or f"{queue_url}/requests/{request_id}/status"
            result_url = submit_data.get("response_url") or f"{queue_url}/requests/{request_id}"

            logging.info(
                f"fal.ai video submitted: {request_id} ({model_id}) | "
                f"status_url={status_url[:80]} result_url={result_url[:80]} | "
                f"payload_keys={list(payload.keys())}"
            )

        # 2. Polling через /status - каждые 10 сек, до 25 минут
        # Content-Type убираем из заголовков т.к. это GET без тела (некоторые сервера
        # ругаются на Content-Type в GET запросах и возвращают 405).
        get_headers = {"Authorization": f"Key {FAL_API_KEY}"}

        await asyncio.sleep(20)  # Даём fal время поставить задачу на runner
        max_iterations = 150  # 150 × 10 сек = 25 мин
        vid_url = None
        in_queue_count = 0
        consecutive_errors = 0

        for i in range(max_iterations):
            try:
                async with s.get(status_url, headers=get_headers) as sr:
                    # Защита от non-JSON ответов
                    if sr.content_type and "json" not in sr.content_type:
                        consecutive_errors += 1
                        if consecutive_errors % 5 == 0:
                            logging.warning(f"fal.ai status poll non-JSON: {sr.content_type}")
                        await asyncio.sleep(10)
                        continue

                    if sr.status == 200:
                        sd = await sr.json()
                        status = sd.get("status", "")
                        consecutive_errors = 0

                        if status == "COMPLETED":
                            elapsed = 20 + (i + 1) * 10
                            logging.info(f"fal.ai video completed after ~{elapsed}s ({model_id}) request_id={request_id}")
                            break

                    elif sr.status == 202:
                        try:
                            sd = await sr.json()
                        except (aiohttp.ContentTypeError, Exception):
                            consecutive_errors += 1
                            await asyncio.sleep(10)
                            continue
                        status = sd.get("status", "IN_PROGRESS")
                        consecutive_errors = 0

                        if status in ("FAILED", "ERROR"):
                            err_msg = sd.get("error", "Unknown error")
                            raise Exception(f"fal.ai ошибка генерации: {err_msg}")

                        # Защита от зомби в очереди
                        if status == "IN_QUEUE":
                            in_queue_count += 1
                            if in_queue_count >= 30:  # 30 × 10 сек = 5 мин
                                raise Exception(
                                    "⏱ Запрос завис в очереди fal.ai (5+ мин). "
                                    "Попробуй ещё раз через минуту."
                                )
                        else:
                            in_queue_count = 0

                        # Логируем прогресс каждую минуту
                        if (i + 1) % 6 == 0:
                            elapsed_min = (20 + (i + 1) * 10) / 60
                            logging.info(f"fal.ai still generating: {model_id} ({elapsed_min:.1f} min, status={status}, request_id={request_id})")

                    elif sr.status == 405:
                        # Не должно случаться - fal.ai official API поддерживает GET
                        # Но если всё же вернули 405, пробуем альтернативу: запросить result_url напрямую
                        logging.warning(f"fal.ai /status returned 405! Switching to direct result_url polling")
                        # Переключаемся на опрос result_url - он возвращает данные когда готово
                        try:
                            async with s.get(result_url, headers=get_headers) as rr2:
                                if rr2.status == 200:
                                    rd2 = await rr2.json() if rr2.content_type and "json" in rr2.content_type else {}
                                    # Проверяем есть ли уже результат
                                    v2 = rd2.get("video")
                                    if isinstance(v2, dict) and v2.get("url"):
                                        vid_url = v2["url"]
                                        logging.info(f"fal.ai got result via result_url (405 fallback): {request_id}")
                                        break
                                    elif isinstance(v2, str):
                                        vid_url = v2
                                        break
                        except Exception as fallback_err:
                            logging.warning(f"fal.ai result_url fallback also failed: {fallback_err}")

                    else:
                        # Другие коды - логируем и продолжаем
                        consecutive_errors += 1
                        err_body = (await sr.text())[:200]
                        logging.warning(f"fal.ai status poll {sr.status}: {err_body}")
                        if consecutive_errors >= 10:
                            raise Exception(f"fal.ai status API возвращает ошибки 10 раз подряд: {sr.status}")

            except (aiohttp.ClientError, aiohttp.ContentTypeError) as ce:
                consecutive_errors += 1
                logging.warning(f"fal.ai status poll error: {ce}")
                if consecutive_errors >= 10:
                    raise Exception(f"fal.ai сеть упала 10 раз подряд: {ce}")

            await asyncio.sleep(10)
        else:
            # Исчерпали итерации - rescue попытка
            logging.warning(f"fal.ai polling exhausted, rescue attempt: {request_id}")
            try:
                async with s.get(result_url, headers=get_headers) as last_try:
                    if last_try.status == 200:
                        ld = await last_try.json() if last_try.content_type and "json" in last_try.content_type else {}
                        v = ld.get("video")
                        if isinstance(v, dict):
                            vid_url = v.get("url")
                        elif isinstance(v, str):
                            vid_url = v
                        if vid_url:
                            logging.info(f"fal.ai rescued video via direct fetch: {request_id}")
            except Exception as rescue_err:
                logging.warning(f"fal.ai rescue failed: {rescue_err}")

            if not vid_url:
                raise Exception(
                    f"⏱ Таймаут генерации (>25 мин). Request ID: {request_id}. "
                    f"Попробуй ещё раз или выбери более быструю модель."
                )

        # 3. Получаем результат (если ещё не получили через 405-fallback или rescue)
        if not vid_url:
            rd = None
            for _res_att in range(10):
                try:
                    async with s.get(result_url, headers=get_headers) as rr:
                        if rr.status != 200:
                            err_text = (await rr.text())[:500]
                            logging.error(f"fal.ai result fetch FAILED: status={rr.status} url={result_url} body={err_text}")
                            raise Exception(f"fal.ai result fetch {rr.status}: {err_text[:200]}")
                        if rr.content_type and "json" in rr.content_type:
                            rd = await rr.json()
                            break
                        else:
                            logging.warning(f"fal.ai result non-JSON attempt {_res_att}/10: {rr.content_type}")
                            await asyncio.sleep(5)
                except aiohttp.ContentTypeError:
                    logging.warning(f"fal.ai result ContentTypeError attempt {_res_att}/10")
                    await asyncio.sleep(5)
            if not rd:
                raise Exception(f"fal.ai result: не удалось получить JSON после 10 попыток. Request ID: {request_id}")

            # Парсим URL видео
            video = rd.get("video")
            if isinstance(video, dict):
                vid_url = video.get("url")
            elif isinstance(video, str):
                vid_url = video
            if not vid_url:
                vid_url = rd.get("video_url")
            if not vid_url:
                output = rd.get("output")
                if isinstance(output, dict):
                    vid_url = (output.get("video", {}).get("url")
                               if isinstance(output.get("video"), dict)
                               else output.get("video_url"))

            if not vid_url:
                # Проверяем content_policy_violation
                rd_str = str(rd).lower()
                if "content_policy_violation" in rd_str or "flagged by a content" in rd_str:
                    raise Exception(
                        "🛡 Контент заблокирован моделью.\n\n"
                        "Попробуй переформулировать - избегай NSFW, насилия или знаменитостей."
                    )
                logging.error(f"fal.ai no video url in response. keys={list(rd.keys())} full={str(rd)[:800]}")
                raise Exception(f"fal.ai не вернул URL видео. Request ID: {request_id}")

        logging.info(f"fal.ai video URL obtained: {vid_url[:100]} (request_id={request_id})")

    # 4. Скачивание видео - ОТДЕЛЬНАЯ сессия с собственным таймаутом (вне сессии polling)
    # Это критично: polling мог «съесть» время общей сессии, а скачивание mp4 требует свежего окна.
    # До 3 попыток с экспоненциальным бэкоффом.
    download_timeout = aiohttp.ClientTimeout(total=300, sock_read=120)  # 5 мин общий, 2 мин на чтение
    last_download_err = None
    for attempt in range(1, 4):
        try:
            async with aiohttp.ClientSession(timeout=download_timeout) as dl_session:
                async with dl_session.get(vid_url) as vr:
                    if vr.status != 200:
                        last_download_err = f"HTTP {vr.status}"
                        logging.warning(f"fal.ai download attempt {attempt}/3 failed: {last_download_err}")
                        await asyncio.sleep(2 * attempt)
                        continue
                    vid_bytes = await vr.read()
                    if len(vid_bytes) < 10000:
                        last_download_err = f"too small ({len(vid_bytes)} bytes)"
                        logging.warning(f"fal.ai download attempt {attempt}/3: {last_download_err}")
                        await asyncio.sleep(2 * attempt)
                        continue
                    size_mb = len(vid_bytes) / 1024 / 1024
                    logging.info(f"fal.ai video DOWNLOADED: {size_mb:.1f} MB on attempt {attempt}/3 (request_id={request_id})")
                    return vid_bytes
        except asyncio.TimeoutError:
            last_download_err = "timeout"
            logging.warning(f"fal.ai download attempt {attempt}/3 timed out")
            await asyncio.sleep(2 * attempt)
        except Exception as dl_err:
            last_download_err = str(dl_err)[:200]
            logging.warning(f"fal.ai download attempt {attempt}/3 error: {last_download_err}")
            await asyncio.sleep(2 * attempt)

    # Все 3 попытки не удались - но видео есть на fal.ai, сообщаем админу request_id для ручного восстановления
    logging.error(f"fal.ai DOWNLOAD FAILED after 3 attempts. request_id={request_id} url={vid_url[:200]} err={last_download_err}")
    raise Exception(
        f"Видео создано, но не удалось его скачать ({last_download_err}). "
        f"Request ID: {request_id}. Админ восстановит вручную командой /recover."
    )


async def api_generate_image(prompt: str, model_id: str, aspect_ratio: str = "1:1",
                              api_type: str = "imagen", quality: str = "medium") -> bytes:
    # Dispatch на fal.ai (Flux 2 Pro, Ideogram V3, GPT Image 2)
    if api_type == "fal":
        return await api_generate_fal_image(prompt, model_id, aspect_ratio, quality=quality)

    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    async with aiohttp.ClientSession() as s:

        if api_type == "gemini":
            # ── Nano Banana (generateContent) ─────────────────
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseModalities": ["IMAGE", "TEXT"],
                    "imageConfig": {"aspectRatio": aspect_ratio},
                }
            }
            async with s.post(url, json=payload, headers=headers) as r:
                if r.status != 200:
                    raise Exception(f"Nano Banana API {r.status}: {(await r.text())[:300]}")
                data = await r.json()

                # Ищем inline image в частях ответа
                candidates = data.get("candidates", [])
                if candidates:
                    cand = candidates[0]
                    for part in cand.get("content", {}).get("parts", []):
                        if "inlineData" in part:
                            return base64.b64decode(part["inlineData"]["data"])

                    # Изображения нет - смотрим причину
                    finish_reason = cand.get("finishReason", "UNKNOWN")
                    logging.warning(
                        f"Nano Banana no image. model={model_id} reason={finish_reason} "
                        f"response={str(data)[:500]}"
                    )

                    # Понятные ошибки для юзера
                    if finish_reason in ("SAFETY", "IMAGE_SAFETY", "PROHIBITED_CONTENT"):
                        raise Exception(
                            "Промт заблокирован фильтром безопасности 🛡\n"
                            "Попробуй переформулировать - избегай сцен с насилием, "
                            "откровенным содержанием или узнаваемыми знаменитостями."
                        )
                    if finish_reason == "RECITATION":
                        raise Exception(
                            "Запрос слишком похож на защищённый копирайтом контент 📄\n"
                            "Попробуй описать сцену своими словами."
                        )
                    # Модель вернула только текст вместо картинки
                    text_parts = [
                        p.get("text", "") for p in cand.get("content", {}).get("parts", [])
                        if "text" in p
                    ]
                    if text_parts:
                        raise Exception(
                            f"Модель не смогла создать картинку. Совет: {text_parts[0][:200]}"
                        )
                    raise Exception(f"Пустой ответ (причина: {finish_reason}). Попробуй другой промт или модель.")

                # Нет candidates вообще - prompt заблокирован на входе
                block = data.get("promptFeedback", {}).get("blockReason", "UNKNOWN")
                logging.warning(f"Nano Banana blocked. model={model_id} block={block}")
                raise Exception(
                    "Промт заблокирован фильтром безопасности 🛡\n"
                    "Попробуй переформулировать запрос."
                )

        else:
            # ── Imagen (predict) ──────────────────────────────
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:predict"
            payload = {
                "instances": [{"prompt": prompt}],
                "parameters": {
                    "sampleCount": 1,
                    "aspectRatio": aspect_ratio,
                    "safetyFilterLevel": "block_few",
                }
            }
            async with s.post(url, json=payload, headers=headers) as r:
                if r.status != 200:
                    raise Exception(f"Imagen API {r.status}: {(await r.text())[:200]}")
                data = await r.json()
                return base64.b64decode(data["predictions"][0]["bytesBase64Encoded"])


async def api_edit_image(image_bytes: bytes, prompt: str, aspect_ratio: str = "1:1") -> bytes:
    """Редактирование фото по референсу через Gemini. Пробует несколько моделей.
    
    Список моделей в порядке приоритета (свежие → старые стабильные):
    - gemini-3.1-flash-image-preview (Nano Banana 2, февраль 2026) - новая, быстрая
    - gemini-2.5-flash-image (Nano Banana) - стабильная основная
    - gemini-3-pro-image-preview (Nano Banana Pro) - премиум-резерв
    """
    img_b64 = base64.b64encode(image_bytes).decode()
    # Список моделей - пробуем по очереди от свежей к стабильным
    models = [
        "gemini-2.5-flash-image",              # Стабильная основа (Nano Banana)
        "gemini-3.1-flash-image-preview",      # Nano Banana 2 (preview)
        "gemini-3-pro-image-preview",          # Премиум резерв (Nano Banana Pro)
    ]
    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "image/jpeg", "data": img_b64}},
                {"text": prompt}
            ]
        }],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    last_error = None
    async with aiohttp.ClientSession() as s:
        for model in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            for attempt in range(3):  # 3 попытки на каждую модель
                try:
                    async with s.post(url, json=payload, headers=headers) as r:
                        if r.status == 503:
                            await asyncio.sleep(3 * (attempt + 1))
                            continue
                        if r.status == 404:
                            # Модель не существует или больше не поддерживается - пропускаем сразу
                            last_error = f"Модель {model} недоступна (404)"
                            logging.warning(f"Gemini model {model} returned 404 - skipping")
                            break
                        if r.status != 200:
                            last_error = f"API {r.status}: {(await r.text())[:150]}"
                            break
                        data = await r.json()
                        for part in data["candidates"][0]["content"]["parts"]:
                            if "inlineData" in part:
                                return base64.b64decode(part["inlineData"]["data"])
                        last_error = "Gemini не вернул изображение. Попробуй другой промт."
                        break
                except Exception as e:
                    last_error = str(e)
                    await asyncio.sleep(2)
    raise Exception(last_error or "Все модели недоступны. Попробуй позже.")


async def api_animate_image(
    first_bytes: bytes,
    prompt: str,
    aspect_ratio: str = "16:9",
    last_bytes: bytes | None = None,
) -> bytes:
    """Анимация фото через Veo 3.1 (первый кадр + опционально последний)."""
    base = "https://generativelanguage.googleapis.com/v1beta"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    first_b64 = base64.b64encode(first_bytes).decode()
    instance = {
        "prompt": prompt,
        "image": {"bytesBase64Encoded": first_b64, "mimeType": "image/jpeg"},
    }
    params = {"durationSeconds": 8, "aspectRatio": aspect_ratio, "sampleCount": 1}
    if last_bytes:
        last_b64 = base64.b64encode(last_bytes).decode()
        instance["lastFrame"] = {"bytesBase64Encoded": last_b64, "mimeType": "image/jpeg"}

    payload = {"instances": [instance], "parameters": params}

    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{base}/models/veo-3.1-fast-generate-preview:predictLongRunning",
            json=payload, headers=headers
        ) as r:
            if r.status != 200:
                raise Exception(f"Veo Anim API {r.status}: {(await r.text())[:200]}")
            op_data = await r.json()
            op_name = op_data.get("name")
            if not op_name:
                raise Exception(f"Veo Anim: нет operation name: {op_data}")
            logging.info(f"Veo Anim operation: {op_name}")

        for _ in range(72):
            await asyncio.sleep(5)
            async with s.get(f"{base}/{op_name}", headers=headers) as pr:
                if pr.status != 200:
                    continue
                try:
                    pd = await pr.json()
                except (aiohttp.ContentTypeError, Exception) as je:
                    logging.warning(f"Veo anim poll json error: {je}")
                    continue
                if not pd.get("done"):
                    continue
                if "error" in pd:
                    raise Exception(pd["error"].get("message", "Veo Anim error"))
                # Парсим ответ
                gen_resp = pd.get("response", {}).get("generateVideoResponse", {})
                samples = gen_resp.get("generatedSamples", [])
                if samples:
                    video = samples[0].get("video", {})
                    if video.get("bytesBase64Encoded"):
                        return base64.b64decode(video["bytesBase64Encoded"])
                    uri = video.get("uri") or video.get("videoUri")
                    if uri:
                        vid_headers = {"x-goog-api-key": GEMINI_API_KEY}
                        scheme = "https://storage.googleapis.com/" if uri.startswith("gs://") else None
                        url = uri.replace("gs://", "https://storage.googleapis.com/") if scheme else uri
                        async with s.get(url, headers=vid_headers) as vr:
                            data_bytes = await vr.read()
                            if len(data_bytes) > 1000:
                                return data_bytes
                logging.error(f"Veo Anim unknown response: {str(pd)[:300]}")
                raise Exception("Неизвестная структура ответа Veo Anim")
    raise Exception("Превышено время ожидания анимации (6 мин)")


# ══════════════════════════════════════════════════════════
#  EVOLINK - Kling Motion Control
# ══════════════════════════════════════════════════════════

async def _tg_file_public_url(file_id: str) -> str:
    """Получает публичный URL файла Telegram (действителен ~1 час).
    EvoLink скачивает файл по этому URL для генерации."""
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

    async with aiohttp.ClientSession() as s:
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
                    raise Exception(
                        "Референсы заблокированы фильтром безопасности 🛡\n"
                        "Попробуй загрузить другие фото/видео - избегай знаменитостей, "
                        "откровенного содержания, брендов."
                    )
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
                    raise Exception(
                        "Референсы заблокированы фильтром безопасности 🛡\n"
                        "Попробуй другие фото/видео - избегай знаменитостей и брендов."
                    )
                raise Exception(f"Kling Motion Control: {err_msg or status or 'неизвестная ошибка'}")
            # pending / queued / generating / processing / running - продолжаем ждать

        # Таймаут - показываем последний известный статус для диагностики
        logging.error(f"Kling timeout task={task_id}, last_status={last_status}, last_response={str(last_response)[:500]}")
        raise Exception(
            f"Превышено время ожидания (20 мин). Последний статус: {last_status}. "
            f"Задача могла завершиться на стороне EvoLink - проверь в их логах. Кредиты возвращены."
        )


async def api_generate_video(prompt: str, model_id: str, aspect_ratio: str = "16:9",
                              api_type: str = "veo", duration: int = 8) -> bytes:
    # Dispatch на fal.ai (Kling 2.5 Turbo Pro, Kling 3.0 Pro)
    if api_type == "fal":
        return await api_generate_fal_video(prompt, model_id, aspect_ratio, duration)

    base = "https://generativelanguage.googleapis.com/v1beta"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    payload = {
        "instances": [{"prompt": prompt}],
        "parameters": {"durationSeconds": 8, "aspectRatio": aspect_ratio, "sampleCount": 1}
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{base}/models/{model_id}:predictLongRunning",
                          json=payload, headers=headers) as r:
            if r.status != 200:
                raise Exception(f"Veo API {r.status}: {(await r.text())[:200]}")
            op_data = await r.json()
            op_name = op_data.get("name")
            logging.info(f"Veo operation started: {op_name}")
            if not op_name:
                raise Exception(f"Veo не вернул operation name: {op_data}")

        # polling до 6 минут
        for i in range(72):
            await asyncio.sleep(5)
            async with s.get(f"{base}/{op_name}", headers=headers) as pr:
                if pr.status != 200:
                    logging.warning(f"Poll status {pr.status}")
                    continue
                pd = await pr.json()
                if not pd.get("done"):
                    continue

                logging.info(f"Veo done response keys: {list(pd.keys())}")

                if "error" in pd:
                    raise Exception(pd["error"].get("message", "Veo error"))

                # Структура 1: response.predictions[]
                preds = pd.get("response", {}).get("predictions", [])
                if preds:
                    logging.info(f"Veo preds[0] keys: {list(preds[0].keys())}")
                    p = preds[0]
                    if p.get("bytesBase64Encoded"):
                        return base64.b64decode(p["bytesBase64Encoded"])
                    uri = p.get("videoUri") or p.get("gcsUri") or p.get("uri")
                    if uri and uri.startswith("https://"):
                        async with s.get(uri) as vr:
                            return await vr.read()
                    if uri:
                        raise Exception(f"GCS URI требует доп. настройки: {uri[:80]}")

                # Структура 2: response.generateVideoResponse.generatedSamples[]
                gen_resp = pd.get("response", {}).get("generateVideoResponse", {})
                samples = gen_resp.get("generatedSamples", [])
                if samples:
                    logging.info(f"Veo samples[0] keys: {list(samples[0].keys())}")
                    sample = samples[0]
                    # video.uri или video.bytesBase64Encoded
                    video = sample.get("video", {})
                    if video.get("bytesBase64Encoded"):
                        return base64.b64decode(video["bytesBase64Encoded"])
                    uri = video.get("uri") or video.get("videoUri")
                    logging.info(f"Veo video uri: {uri[:100] if uri else 'None'}")
                    if uri and uri.startswith("https://"):
                        vid_headers = {"x-goog-api-key": GEMINI_API_KEY}
                        async with s.get(uri, headers=vid_headers) as vr:
                            data_bytes = await vr.read()
                            logging.info(f"Veo video downloaded: {len(data_bytes)} bytes, status: {vr.status}")
                            if len(data_bytes) > 1000:
                                return data_bytes
                            raise Exception(f"Видео слишком маленькое ({len(data_bytes)} bytes). Попробуй ещё раз.")
                    if uri and uri.startswith("gs://"):
                        # Конвертируем GCS URI в HTTPS
                        https_uri = uri.replace("gs://", "https://storage.googleapis.com/")
                        async with s.get(https_uri) as vr:
                            data_bytes = await vr.read()
                            logging.info(f"Veo GCS download: {len(data_bytes)} bytes")
                            if len(data_bytes) > 1000:
                                return data_bytes
                    if uri:
                        raise Exception(f"Не удалось скачать видео: {uri[:80]}")
                    # Может быть напрямую в sample
                    if sample.get("bytesBase64Encoded"):
                        return base64.b64decode(sample["bytesBase64Encoded"])
                    uri = sample.get("uri") or sample.get("videoUri")
                    if uri and uri.startswith("https://"):
                        async with s.get(uri) as vr:
                            return await vr.read()

                # Структура 3: videos[] напрямую
                videos = pd.get("response", {}).get("videos", [])
                if videos:
                    v = videos[0]
                    if v.get("bytesBase64Encoded"):
                        return base64.b64decode(v["bytesBase64Encoded"])
                    uri = v.get("videoUri") or v.get("uri")
                    if uri and uri.startswith("https://"):
                        async with s.get(uri) as vr:
                            return await vr.read()

                # Структура 4: result.videos[]
                result_videos = pd.get("result", {}).get("videos", [])
                if result_videos:
                    v = result_videos[0]
                    if v.get("bytesBase64Encoded"):
                        return base64.b64decode(v["bytesBase64Encoded"])

                # Лог полного ответа для отладки
                resp_str = str(pd.get("response", pd))[:600]
                logging.error(f"Veo unknown response: {resp_str}")
                raise Exception(f"Неизвестная структура ответа Veo. Ключи: {list(pd.get('response', pd).keys())}")

    raise Exception("Превышено время ожидания (6 мин)")

# ══════════════════════════════════════════════════════════
#  ОБРАБОТЧИКИ - СТАРТ / МЕНЮ
# ══════════════════════════════════════════════════════════

