"""
chatgpt_activation.py — активация ChatGPT через 987ai.vip (Playwright).

Библиотеки Chromium устанавливаются через apt-get при сборке (nixpacks.toml).
Браузер скачивается в /app/pw-browsers при сборке И при старте как fallback.
"""

import asyncio
import logging
import os

# Путь к браузеру — /app/pw-browsers скачан при сборке и живёт в Docker-образе.
# os.environ.setdefault не перезаписывает если переменная уже выставлена (напр. Railway Variables).
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/pw-browsers")

logger = logging.getLogger(__name__)


async def activate_chatgpt(card_code: str, access_token: str) -> dict:
    """
    Активирует подписку ChatGPT через сайт 987ai.vip.

    Returns:
        {"success": True, "message": "..."} или {"success": False, "error": "...", "screenshot": bytes|None}
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        return {"success": False, "error": "Playwright не установлен на сервере."}

    url = f"https://www.987ai.vip/recharge?card={card_code}"
    # Извлекаем account_id из JWT payload для диагностики (без верификации подписи)
    account_id = ""
    try:
        import base64 as _b64, json as _json
        payload_b64 = access_token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = _json.loads(_b64.urlsafe_b64decode(payload_b64))
        # OpenAI кладёт user ID в sub или в https://api.openai.com/profile
        account_id = (
            payload.get("sub", "")
            or payload.get("https://api.openai.com/profile", {}).get("id", "")
        )
    except Exception:
        pass

    logger.info(
        f"activate_chatgpt: card={card_code} account_id={account_id or '(не извлечён)'} "
        f"PLAYWRIGHT_BROWSERS_PATH={os.environ.get('PLAYWRIGHT_BROWSERS_PATH')}"
    )

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-setuid-sandbox",
                    "--single-process",
                ]
            )
        except Exception as launch_err:
            logger.error(f"Chromium launch failed: {launch_err}")
            return {"success": False, "error": f"Не удалось запустить браузер: {launch_err}"}

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ru-RU"
        )
        page = await context.new_page()

        try:
            logger.info(f"Открываем 987ai.vip для карты {card_code}")
            await page.goto(url, timeout=30_000, wait_until="networkidle")

            # ── ШАГ 1: Проверить карту ────────────────────────────────────
            card_input = page.locator("input").first
            await card_input.wait_for(state="visible", timeout=15_000)
            await card_input.fill("")
            await card_input.fill(card_code)
            await asyncio.sleep(0.5)

            verify_btn = page.locator(
                "button:has-text('Проверить карту'), "
                "button:has-text('验证卡密'), "
                "button:has-text('Check Card'), "
                "button:has-text('Verify Card'), "
                "button:has-text('查询'), "
                "button:has-text('Submit')"
            ).first
            await verify_btn.wait_for(state="visible", timeout=10_000)
            await verify_btn.click()
            logger.info("Нажали 'Проверить карту'")

            # ── ШАГ 2: Ввести токен ───────────────────────────────────────
            # ВАЖНО: ждём именно textarea — она есть ТОЛЬКО на Step 2.
            # На Step 1 её нет (там обычный <input>). Это гарантирует что
            # мы не вольём JWT в поле карты если Step 2 ещё не загрузился.
            token_area = page.locator("textarea").last
            try:
                await token_area.wait_for(state="visible", timeout=25_000)
            except PlaywrightTimeout:
                diag_ss = None
                try:
                    diag_ss = await page.screenshot(full_page=True)
                except Exception:
                    pass
                page_txt = ""
                try:
                    page_txt = await page.inner_text("body")
                except Exception:
                    pass
                page_txt_lower = page_txt.lower()

                # Проверяем: код уже использован на сайте?
                already_used_markers = [
                    "уже использован", "already used", "已使用", "已激活",
                    "пополнить с существующей", "существующей записью",
                    "аккаунт пополнения",   # текст под «уже использован»
                ]
                if any(m in page_txt_lower for m in already_used_markers):
                    logger.warning(f"Код {card_code} уже использован на 987ai.vip — нужен новый")
                    return {
                        "success": False,
                        "code_already_used": True,
                        "error": f"Код {card_code} уже занят. Выдаём следующий автоматически.",
                        "screenshot": diag_ss,
                    }

                logger.error(f"Step 2 не загрузился (нет textarea). Страница: {page_txt[:400]!r}")
                return {
                    "success": False,
                    "error": "Карта не прошла проверку или сайт не загрузил шаг 2. Проверь код карты.",
                    "screenshot": diag_ss,
                }
            await asyncio.sleep(1.0)
            await token_area.fill("")
            await token_area.fill(access_token)
            await asyncio.sleep(0.8)
            logger.info(f"Токен введён в textarea (длина: {len(access_token)})")

            # ── Диагностика: логируем все видимые кнопки и поля ─────────────
            try:
                all_btns = page.locator("button")
                btn_count = await all_btns.count()
                visible_btn_texts = []
                for i in range(btn_count):
                    try:
                        btn = all_btns.nth(i)
                        if await btn.is_visible():
                            txt = (await btn.inner_text()).strip()
                            if txt:
                                visible_btn_texts.append(f"'{txt}'")
                    except Exception:
                        pass
                logger.info(f"Видимые кнопки после ввода токена: {', '.join(visible_btn_texts) or '(не найдено)'}")
            except Exception as log_err:
                logger.warning(f"Не удалось перечислить кнопки: {log_err}")

            # Проверяем что токен попал именно в textarea, а не куда-то ещё
            try:
                actual_val = (await token_area.input_value()).strip()
                if actual_val.startswith("eyJ"):
                    logger.info(f"✅ Токен в поле подтверждён (начало: {actual_val[:20]}...)")
                else:
                    logger.warning(f"⚠️ В токен-поле неожиданное значение: {actual_val[:40]!r}")
            except Exception:
                pass

            # ── Ищем кнопку Шага 2 — расширенный список + fallback ───────
            # Приоритетные тексты (в порядке проверки)
            STEP2_TEXTS = [
                "Проверить аккаунт",    # текущий текст кнопки на 987ai.vip (RU)
                "验证账号",
                "Verify Account",
                "Verify",
                "Next",
                "下一步",
                "Continue",
                "Продолжить",
                "Подтвердить",
                "Confirm",
                "确认",
                "充值",
                "Recharge",
                "Submit",
                "Apply",
                "OK",
                "兑换",
                "激活",
                "Activate",
            ]

            confirm_btn = None
            confirm_sel_used = None

            # Стратегия 1: перебираем известные тексты
            for text in STEP2_TEXTS:
                try:
                    btn = page.locator(f"button:has-text('{text}')").last
                    if await btn.is_visible():
                        confirm_btn = btn
                        confirm_sel_used = f"text='{text}'"
                        break
                except Exception:
                    pass

            # Стратегия 2: последняя видимая enabled-кнопка (не «назад/отмена»)
            if not confirm_btn:
                logger.warning("Стратегия 1 не нашла кнопку — пробуем fallback (последняя видимая кнопка)")
                SKIP_WORDS = {"отмена", "назад", "cancel", "back", "закрыть", "close", "нет", "no"}
                try:
                    all_btns2 = page.locator("button")
                    count2 = await all_btns2.count()
                    for i in range(count2 - 1, -1, -1):
                        try:
                            btn = all_btns2.nth(i)
                            if not await btn.is_visible():
                                continue
                            if not await btn.is_enabled():
                                continue
                            txt_lower = (await btn.inner_text()).strip().lower()
                            if any(w in txt_lower for w in SKIP_WORDS):
                                continue
                            confirm_btn = btn
                            confirm_sel_used = f"fallback_last_btn='{txt_lower[:40]}'"
                            break
                        except Exception:
                            pass
                except Exception as fb_err:
                    logger.warning(f"Fallback поиск кнопки упал: {fb_err}")

            # Стратегия 3: любой input[type=submit]
            if not confirm_btn:
                try:
                    sub = page.locator("input[type='submit']").last
                    if await sub.is_visible():
                        confirm_btn = sub
                        confirm_sel_used = "input[type=submit]"
                except Exception:
                    pass

            if not confirm_btn:
                # Снимок страницы для диагностики
                try:
                    diag_ss = await page.screenshot(full_page=True)
                except Exception:
                    diag_ss = None
                page_body = ""
                try:
                    page_body = (await page.inner_text("body"))[:600]
                except Exception:
                    pass
                logger.error(f"Кнопка Шага 2 не найдена. Текст страницы: {page_body}")
                return {
                    "success": False,
                    "error": "Сайт изменил интерфейс — кнопка подтверждения не найдена. Скриншот отправлен Александру.",
                    "screenshot": diag_ss,
                }

            logger.info(f"Нажимаем кнопку Шага 2 ({confirm_sel_used})")
            await confirm_btn.click()
            logger.info("Нажали кнопку 'Подтвердить аккаунт'")

            # ── ШАГ 3: Подтвердить пополнение ────────────────────────────
            # Шаг 3 загружается после "Проверить аккаунт". Ждём его.
            await asyncio.sleep(2.5)

            # ── Принудительное пополнение — включаем чекбокс на Шаге 3 ──────
            # Чекбокс появляется ТОЛЬКО на Шаге 3 (подтверждение пополнения).
            # Без него сайт не активирует если подписка уже активна.
            try:
                # Ищем чекбокс рядом с текстом "Принудительное пополнение"
                force_cb = None
                for cb_label in ["Принудительное пополнение", "强制充值", "Force Recharge", "Принудительн"]:
                    try:
                        # Сначала ищем внутри label
                        cb = page.locator(f"label:has-text('{cb_label}') input[type='checkbox']").first
                        if await cb.count() and await cb.is_visible():
                            force_cb = cb
                            break
                        # Потом ищем label и берём соседний чекбокс
                        lbl = page.locator(f"label:has-text('{cb_label}')").first
                        if await lbl.is_visible():
                            force_cb = page.locator("input[type='checkbox']").first
                            break
                    except Exception:
                        pass

                # Fallback: любой видимый чекбокс на странице
                if not force_cb:
                    all_cbs = page.locator("input[type='checkbox']")
                    for i in range(await all_cbs.count()):
                        cb = all_cbs.nth(i)
                        if await cb.is_visible():
                            force_cb = cb
                            break

                if force_cb:
                    if not await force_cb.is_checked():
                        await force_cb.check()
                        logger.info("✅ Чекбокс 'Принудительное пополнение' включён")
                    else:
                        logger.info("✅ Чекбокс 'Принудительное пополнение' уже был включён")
                else:
                    logger.info("ℹ️ Чекбокс 'Принудительное пополнение' не найден на Шаге 3")
            except Exception as cb_err:
                logger.warning(f"Ошибка чекбокса на Шаге 3: {cb_err}")

            await asyncio.sleep(0.5)

            STEP3_TEXTS = [
                "Подтвердить пополнение",
                "确认充值",
                "Confirm Recharge",
                "Confirm",
                "确认",
                "充值",
                "Activate",
                "激活",
                "Complete",
                "完成",
                "Submit",
            ]

            final_btn = None
            for text in STEP3_TEXTS:
                try:
                    btn = page.locator(f"button:has-text('{text}')").last
                    if await btn.is_visible():
                        final_btn = btn
                        break
                except Exception:
                    pass

            if final_btn:
                try:
                    await final_btn.wait_for(state="visible", timeout=15_000)
                    await final_btn.click()
                    logger.info("Нажали 'Подтвердить пополнение'")
                except PlaywrightTimeout:
                    logger.warning("Кнопка шага 3 исчезла до клика — продолжаем")
            else:
                logger.warning("Кнопка шага 3 не найдена — возможно шагов 2, смотрим результат")

            # ── Ждём результата: polling до 5 минут ──────────────────────
            success_markers = [
                "успешно", "success", "成功", "充值成功", "recharge successful",
                "activated", "активирован", "подписка активирована",
                "充值完成", "完成",
            ]
            # Маркеры невалидного токена (клиент неправильно скопировал)
            token_invalid_markers = [
                "token无效或已过期",  # скриншот 2: точный текст сайта
                "token无效",
                "无效或已过期",
                "token invalid or expired",
                "token invalid",
                "invalid token",
                "token expired",
                "неверный токен",
                "токен истёк",
                "войдите снова",
                "token is invalid",
                "access token",  # "invalid access token"
            ]
            error_markers = [
                "失败", "failed",
                "не найден", "not found",
                "充值失败", "错误",
            ] + token_invalid_markers
            # Маркеры "ещё обрабатывается / в очереди" — продолжаем ждать
            processing_markers = [
                "обработка", "обрабатываем", "processing", "处理中", "请稍候",
                # Маркеры очереди
                "очередь", "в очереди", "queue", "排队", "等待中",
                "ожидайте", "подождите", "pending", "waiting",
                "поставлен в очередь", "queued",
            ]

            max_polls = 100      # 100 × 3с = 5 минут (покрывает очереди на сайте)
            final_result = None

            for attempt in range(max_polls):
                await asyncio.sleep(3.0)
                try:
                    page_text = (await page.inner_text("body")).lower()
                except Exception:
                    break

                logger.info(f"Polling {attempt+1}/{max_polls}: проверяем результат")

                if any(m in page_text for m in processing_markers):
                    logger.debug(f"Ещё обрабатывается (попытка {attempt+1})")
                    continue

                for marker in success_markers:
                    if marker in page_text:
                        logger.info(f"✅ Активация успешна (маркер: '{marker}')")
                        try:
                            _ss = await page.screenshot(full_page=True)
                        except Exception:
                            _ss = None
                        final_result = {"success": True, "message": "Подписка успешно активирована!", "screenshot": _ss}
                        break

                if final_result:
                    break

                for marker in error_markers:
                    if marker in page_text:
                        logger.warning(f"❌ Ошибка активации (маркер: '{marker}')")
                        try:
                            _ss_err = await page.screenshot(full_page=True)
                        except Exception:
                            _ss_err = None
                        # Определяем тип ошибки: невалидный токен или другая
                        _is_token_err = any(m in page_text for m in token_invalid_markers)
                        final_result = {
                            "success": False,
                            "token_invalid": _is_token_err,
                            "error": _extract_error_text(page_text),
                            "screenshot": _ss_err,
                        }
                        break

                if final_result:
                    break

                if attempt >= max_polls - 3:
                    logger.warning(f"Неизвестное состояние страницы после {attempt+1} попыток")

            if final_result:
                return final_result

            screenshot = await page.screenshot(full_page=True)
            logger.warning(f"Таймаут polling ({max_polls * 3}с) — результат неизвестен, скриншот отправлен")
            return {
                "success": False,
                "error": "Сайт долго обрабатывал запрос. Александр проверит скриншот.",
                "screenshot": screenshot,
            }

        except PlaywrightTimeout as e:
            logger.error(f"Таймаут на 987ai.vip: {e}")
            # Берём скриншот для диагностики (раньше его не было при таймауте)
            diag_ss = None
            try:
                diag_ss = await page.screenshot(full_page=True)
            except Exception:
                pass
            return {
                "success": False,
                "error": "Сайт не ответил вовремя. Попробуй снова.",
                "screenshot": diag_ss,
            }

        except Exception as e:
            logger.error(f"Ошибка активации: {e}", exc_info=True)
            diag_ss = None
            try:
                diag_ss = await page.screenshot(full_page=True)
            except Exception:
                pass
            return {"success": False, "error": str(e), "screenshot": diag_ss}

        finally:
            await browser.close()


def _extract_error_text(page_text: str) -> str:
    for line in page_text.splitlines():
        line = line.strip()
        if len(line) > 5 and any(m in line for m in ["ошибка", "error", "失败", "invalid", "expired"]):
            return line[:200]
    return "Ошибка активации. Возможно токен истёк или уже использован."


async def check_gpt_code(card_code: str) -> dict:
    """
    Проверяет код активации на 987ai.vip (только Шаг 1 — без токена).

    Returns одно из:
        {"status": "ok"}                          — код валидный, Step 2 открылся
        {"status": "used",  "email": "..."}       — код уже использован
        {"status": "invalid"}                     — код не существует
        {"status": "error", "error": "..."}       — сетевая ошибка / таймаут
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        return {"status": "error", "error": "Playwright не установлен"}

    url = f"https://www.987ai.vip/recharge?card={card_code}"
    logger.debug(f"check_gpt_code: {card_code}")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-gpu", "--single-process"]
            )
        except Exception as e:
            return {"status": "error", "error": f"Браузер не запустился: {e}"}

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ru-RU"
        )
        page = await context.new_page()

        try:
            await page.goto(url, timeout=25_000, wait_until="networkidle")

            # Вводим код и нажимаем «Проверить карту»
            card_input = page.locator("input").first
            await card_input.wait_for(state="visible", timeout=12_000)
            await card_input.fill("")
            await card_input.fill(card_code)
            await asyncio.sleep(0.4)

            verify_btn = page.locator(
                "button:has-text('Проверить карту'), "
                "button:has-text('验证卡密'), "
                "button:has-text('Check Card'), "
                "button:has-text('Verify Card')"
            ).first
            await verify_btn.wait_for(state="visible", timeout=8_000)
            await verify_btn.click()

            # Ждём реакции сайта (до 12 сек)
            await asyncio.sleep(2.0)
            try:
                page_text = await page.inner_text("body")
            except Exception:
                page_text = ""
            pt_lower = page_text.lower()

            # ── Код уже использован ──────────────────────────────────────
            used_markers = [
                "уже использован", "already used", "已使用", "已激活",
                "пополнить с существующей", "аккаунт пополнения",
            ]
            if any(m in pt_lower for m in used_markers):
                # Пробуем вытащить email
                email = ""
                import re as _re
                m = _re.search(r'[\w.+-]+@[\w.-]+\.\w+', page_text)
                if m:
                    email = m.group(0)
                logger.info(f"check_gpt_code {card_code}: USED email={email}")
                return {"status": "used", "email": email}

            # ── Код не существует ────────────────────────────────────────
            invalid_markers = [
                "не существует", "does not exist", "不存在", "invalid card",
                "ключ не найден", "card not found",
            ]
            if any(m in pt_lower for m in invalid_markers):
                logger.info(f"check_gpt_code {card_code}: INVALID")
                return {"status": "invalid"}

            # ── Шаг 2 загрузился — textarea видна ───────────────────────
            try:
                textarea = page.locator("textarea").last
                await textarea.wait_for(state="visible", timeout=10_000)
                logger.info(f"check_gpt_code {card_code}: OK")
                return {"status": "ok"}
            except PlaywrightTimeout:
                pass

            # ── Неизвестное состояние ────────────────────────────────────
            logger.warning(f"check_gpt_code {card_code}: unknown state — {page_text[:200]!r}")
            return {"status": "error", "error": f"Неизвестный ответ сайта: {page_text[:100]}"}

        except PlaywrightTimeout as e:
            logger.warning(f"check_gpt_code {card_code}: timeout — {e}")
            return {"status": "error", "error": "timeout"}
        except Exception as e:
            logger.error(f"check_gpt_code {card_code}: {e}")
            return {"status": "error", "error": str(e)[:200]}
        finally:
            await browser.close()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) != 3:
        print("Использование: python chatgpt_activation.py <CARD_CODE> <ACCESS_TOKEN>")
        sys.exit(1)
    result = asyncio.run(activate_chatgpt(sys.argv[1], sys.argv[2]))
    print(result)
