"""
chatgpt_activation.py — активация ChatGPT через 987ai.vip (Playwright).
"""
import asyncio
import logging
import os

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/pw-browsers")
logger = logging.getLogger(__name__)


async def activate_chatgpt(card_code: str, access_token: str) -> dict:
    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        return {"success": False, "error": "Playwright не установлен на сервере."}

    url = f"https://www.987ai.vip/recharge?card={card_code}"

    account_id = ""
    try:
        import base64 as _b64, json as _json
        payload_b64 = access_token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = _json.loads(_b64.urlsafe_b64decode(payload_b64))
        account_id = payload.get("sub", "") or payload.get("https://api.openai.com/profile", {}).get("id", "")
    except Exception:
        pass

    logger.info(f"activate_chatgpt: card={card_code} account_id={account_id or '(нет)'} PLAYWRIGHT_BROWSERS_PATH={os.environ.get('PLAYWRIGHT_BROWSERS_PATH')}")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--disable-setuid-sandbox","--single-process"])
        except Exception as launch_err:
            return {"success": False, "error": f"Браузер не запустился: {launch_err}"}

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="ru-RU"
        )
        page = await context.new_page()

        try:
            await page.goto(url, timeout=30_000, wait_until="networkidle")

            # ШАГ 1: заполнить карту
            card_input = page.locator("input").first
            await card_input.wait_for(state="visible", timeout=15_000)
            await card_input.fill("")
            await card_input.fill(card_code)
            await asyncio.sleep(0.5)

            verify_btn = page.locator(
                "button:has-text('Проверить карту'), button:has-text('验证卡密'), "
                "button:has-text('Check Card'), button:has-text('Verify Card'), "
                "button:has-text('查询'), button:has-text('Submit')"
            ).first
            await verify_btn.wait_for(state="visible", timeout=10_000)
            await verify_btn.click()
            logger.info("Нажали 'Проверить карту'")

            # ШАГ 2: ждём textarea (только на Step 2!)
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
                pt_lower = page_txt.lower()
                already_used = ["уже использован","already used","已使用","已激活","пополнить с существующей","аккаунт пополнения"]
                if any(m in pt_lower for m in already_used):
                    return {"success": False, "code_already_used": True, "error": f"Код {card_code} уже занят.", "screenshot": diag_ss}
                return {"success": False, "error": "Карта не прошла проверку или сайт не загрузил шаг 2.", "screenshot": diag_ss}

            await asyncio.sleep(1.0)
            await token_area.fill("")
            await token_area.fill(access_token)
            await asyncio.sleep(0.8)
            logger.info(f"Токен введён (длина: {len(access_token)})")

            # Диагностика кнопок
            try:
                btn_texts = []
                all_b = page.locator("button")
                for i in range(await all_b.count()):
                    try:
                        b = all_b.nth(i)
                        if await b.is_visible():
                            t = (await b.inner_text()).strip()
                            if t:
                                btn_texts.append(repr(t))
                    except Exception:
                        pass
                logger.info(f"Видимые кнопки: {', '.join(btn_texts) or '(нет)'}")
            except Exception:
                pass

            # Кнопка Шага 2
            STEP2_TEXTS = ["Проверить аккаунт","验证账号","Verify Account","Verify","Next","下一步","Continue","Продолжить","Подтвердить","Confirm","确认","充值","Recharge","Submit","Apply","OK","兑换","激活","Activate"]
            confirm_btn = None
            confirm_sel = None
            for text in STEP2_TEXTS:
                try:
                    btn = page.locator(f"button:has-text('{text}')").last
                    if await btn.is_visible():
                        confirm_btn = btn
                        confirm_sel = text
                        break
                except Exception:
                    pass
            if not confirm_btn:
                SKIP = {"отмена","назад","cancel","back","закрыть","close","нет","no"}
                try:
                    all_b2 = page.locator("button")
                    for i in range(await all_b2.count() - 1, -1, -1):
                        try:
                            b = all_b2.nth(i)
                            if not await b.is_visible() or not await b.is_enabled():
                                continue
                            tl = (await b.inner_text()).strip().lower()
                            if any(w in tl for w in SKIP):
                                continue
                            confirm_btn = b
                            confirm_sel = f"fallback:{tl[:30]}"
                            break
                        except Exception:
                            pass
                except Exception:
                    pass
            if not confirm_btn:
                diag = None
                try:
                    diag = await page.screenshot(full_page=True)
                except Exception:
                    pass
                return {"success": False, "error": "Кнопка Шага 2 не найдена.", "screenshot": diag}

            logger.info(f"Нажимаем кнопку Шага 2: '{confirm_sel}'")
            await confirm_btn.click()

            # ШАГ 3
            await asyncio.sleep(2.5)

            async def _check_force_checkbox():
                """Принудительно включает чекбокс несколькими способами.
                JS-клик наиболее надёжен для Vue/React компонентов."""
                try:
                    await asyncio.sleep(0.8)

                    # Способ 1: JavaScript — самый надёжный для React/Vue
                    try:
                        already = await page.evaluate("""
                            () => {
                                const cb = document.querySelector('input[type="checkbox"]');
                                if (!cb) return null;
                                const was = cb.checked;
                                if (!cb.checked) { cb.click(); }
                                return {was: was, now: cb.checked};
                            }
                        """)
                        if already is not None:
                            status = "уже был" if already.get("was") else "включён через JS"
                            logger.info(f"✅ Чекбокс {status}")
                            if already.get("now"):
                                return True
                    except Exception as js_e:
                        logger.warning(f"JS checkbox: {js_e}")

                    # Способ 2: клик по label с текстом
                    for lbl_text in ["Принудительное пополнение","强制充值","Force Recharge","Принудительн","force","Force"]:
                        try:
                            lbl = page.locator(f"label:has-text('{lbl_text}'), span:has-text('{lbl_text}')").first
                            if await lbl.count() and await lbl.is_visible():
                                cb = page.locator("input[type='checkbox']").first
                                if await cb.count():
                                    if not await cb.is_checked():
                                        await cb.click(force=True)
                                        await asyncio.sleep(0.3)
                                        if not await cb.is_checked():
                                            await lbl.click(force=True)
                                        logger.info(f"✅ Чекбокс через label '{lbl_text}'")
                                    else:
                                        logger.info("✅ Чекбокс уже включён")
                                    return True
                                await lbl.click(force=True)
                                await asyncio.sleep(0.3)
                                logger.info(f"✅ Клик по label '{lbl_text}'")
                                return True
                        except Exception:
                            pass

                    # Способ 3: любой видимый чекбокс
                    try:
                        all_cbs = page.locator("input[type='checkbox']")
                        for i in range(await all_cbs.count()):
                            cb = all_cbs.nth(i)
                            try:
                                if not await cb.is_checked():
                                    await cb.click(force=True)
                                    await asyncio.sleep(0.2)
                                logger.info(f"✅ Чекбокс #{i} (любой)")
                                return True
                            except Exception:
                                pass
                    except Exception:
                        pass

                    # Способ 4: JS click на label/parent
                    try:
                        await page.evaluate("""
                            () => {
                                const cb = document.querySelector('input[type="checkbox"]');
                                if (cb) { cb.click(); if (cb.parentElement) cb.parentElement.click(); return true; }
                                const labels = document.querySelectorAll('label');
                                for (const l of labels) {
                                    const t = (l.innerText||'').toLowerCase();
                                    if (t.includes('force') || t.includes('принудит') || t.includes('充值')) {
                                        l.click(); return true;
                                    }
                                }
                                return false;
                            }
                        """)
                        logger.info("✅ JS label/parent click")
                        return True
                    except Exception:
                        pass

                    logger.info("ℹ️ Чекбокс не найден ни одним способом")
                    return False
                except Exception as e:
                    logger.warning(f"Чекбокс ошибка: {e}")
                    return False

            await _check_force_checkbox()
            await asyncio.sleep(0.5)

            STEP3_TEXTS = ["Подтвердить пополнение","确认充值","Confirm Recharge","Confirm","确认","充值","Activate","激活","Complete","完成","Submit"]
            STEP3_RETRY_MARKERS = [
                "пополнение не удалось","若提交多次","充值未成功","充值失败了","提交失败",
                "recharge failed","top up failed",
                "уже plus","уже является plus","already plus","already subscribed",
                "смените аккаунт","пользователь уже",
            ]
            MAX_STEP3_RETRIES = 5

            async def _click_step3():
                for text in STEP3_TEXTS:
                    try:
                        btn = page.locator(f"button:has-text('{text}')").last
                        if await btn.is_visible() and await btn.is_enabled():
                            await btn.click()
                            logger.info(f"Нажали кнопку Шага 3: '{text}'")
                            return True
                    except Exception:
                        pass
                return False

            if not await _click_step3():
                logger.warning("Кнопка Шага 3 не найдена — смотрим результат")

            # Polling
            success_markers = ["успешно","success","成功","充值成功","recharge successful","activated","активирован","подписка активирована","充值完成","完成"]
            token_invalid_markers = ["token无效或已过期","token无效","无效或已过期","token invalid or expired","token invalid","invalid token","token expired","неверный токен","токен истёк","войдите снова","token is invalid"]
            error_markers = ["失败","failed","не найден","not found","充值失败","错误"] + token_invalid_markers
            processing_markers = ["обработка","обрабатываем","processing","处理中","请稍候","очередь","в очереди","queue","排队","等待中","ожидайте","подождите","pending","waiting","поставлен в очередь","queued"]

            max_polls = 100
            final_result = None
            step3_retries = 0

            for attempt in range(max_polls):
                await asyncio.sleep(3.0)
                try:
                    page_text = (await page.inner_text("body")).lower()
                except Exception:
                    break

                logger.info(f"Polling {attempt+1}/{max_polls}")

                if any(m in page_text for m in processing_markers):
                    logger.debug(f"Обрабатывается ({attempt+1})")
                    continue

                if any(m in page_text for m in STEP3_RETRY_MARKERS):
                    step3_retries += 1
                    if step3_retries <= MAX_STEP3_RETRIES:
                        logger.warning(f"⚠️ «Пополнение не удалось» — повтор {step3_retries}/{MAX_STEP3_RETRIES}")
                        await asyncio.sleep(1.5)
                        await _check_force_checkbox()
                        await asyncio.sleep(1.5)
                        await _click_step3()
                        continue
                    else:
                        ss = None
                        try:
                            ss = await page.screenshot(full_page=True)
                        except Exception:
                            pass
                        final_result = {"success": False, "token_invalid": False, "error": "Пополнение не удалось после нескольких попыток. Возможно токен устарел — скопируй заново.", "screenshot": ss}
                        break

                for marker in success_markers:
                    if marker in page_text:
                        logger.info(f"✅ Успешно (маркер: '{marker}')")
                        ss = None
                        try:
                            ss = await page.screenshot(full_page=True)
                        except Exception:
                            pass
                        final_result = {"success": True, "message": "Подписка успешно активирована!", "screenshot": ss}
                        break
                if final_result:
                    break

                for marker in error_markers:
                    if marker in page_text:
                        logger.warning(f"❌ Ошибка (маркер: '{marker}')")
                        ss = None
                        try:
                            ss = await page.screenshot(full_page=True)
                        except Exception:
                            pass
                        is_token = any(m in page_text for m in token_invalid_markers)
                        final_result = {"success": False, "token_invalid": is_token, "error": _extract_error_text(page_text), "screenshot": ss}
                        break
                if final_result:
                    break

            if final_result:
                return final_result

            ss = None
            try:
                ss = await page.screenshot(full_page=True)
            except Exception:
                pass
            return {"success": False, "error": "Сайт долго обрабатывал запрос. Александр проверит скриншот.", "screenshot": ss}

        except PlaywrightTimeout as e:
            logger.error(f"Таймаут: {e}")
            ss = None
            try:
                ss = await page.screenshot(full_page=True)
            except Exception:
                pass
            return {"success": False, "error": "Сайт не ответил вовремя. Попробуй снова.", "screenshot": ss}
        except Exception as e:
            logger.error(f"Ошибка активации: {e}", exc_info=True)
            ss = None
            try:
                ss = await page.screenshot(full_page=True)
            except Exception:
                pass
            return {"success": False, "error": str(e), "screenshot": ss}
        finally:
            await browser.close()


def _extract_error_text(page_text: str) -> str:
    for line in page_text.splitlines():
        line = line.strip()
        if len(line) > 5 and any(m in line for m in ["ошибка","error","失败","invalid","expired","не удалось"]):
            return line[:200]
    return "Ошибка активации. Возможно токен истёк или уже использован."


async def check_gpt_code(card_code: str) -> dict:
    """Проверяет код через Шаг 1 на 987ai.vip без токена."""
    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        return {"status": "error", "error": "Playwright не установлен"}

    url = f"https://www.987ai.vip/recharge?card={card_code}"

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--single-process"])
        except Exception as e:
            return {"status": "error", "error": f"Браузер: {e}"}

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="ru-RU"
        )
        page = await context.new_page()

        try:
            await page.goto(url, timeout=25_000, wait_until="networkidle")
            card_input = page.locator("input").first
            await card_input.wait_for(state="visible", timeout=12_000)
            await card_input.fill("")
            await card_input.fill(card_code)
            await asyncio.sleep(0.4)

            verify_btn = page.locator(
                "button:has-text('Проверить карту'), button:has-text('验证卡密'), "
                "button:has-text('Check Card'), button:has-text('Verify Card')"
            ).first
            await verify_btn.wait_for(state="visible", timeout=8_000)
            await verify_btn.click()
            await asyncio.sleep(2.0)

            try:
                page_text = await page.inner_text("body")
            except Exception:
                page_text = ""
            pt_lower = page_text.lower()

            used_markers = ["уже использован","already used","已使用","已激活","пополнить с существующей","аккаунт пополнения"]
            if any(m in pt_lower for m in used_markers):
                import re as _re
                email = ""
                m = _re.search(r'[\w.+-]+@[\w.-]+\.\w+', page_text)
                if m:
                    email = m.group(0)
                return {"status": "used", "email": email}

            invalid_markers = ["не существует","does not exist","不存在","invalid card","ключ не найден","card not found"]
            if any(m in pt_lower for m in invalid_markers):
                return {"status": "invalid"}

            try:
                textarea = page.locator("textarea").last
                await textarea.wait_for(state="visible", timeout=10_000)
                return {"status": "ok"}
            except PlaywrightTimeout:
                pass

            return {"status": "error", "error": f"Неизвестный ответ: {page_text[:100]}"}

        except PlaywrightTimeout as e:
            return {"status": "error", "error": "timeout"}
        except Exception as e:
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
