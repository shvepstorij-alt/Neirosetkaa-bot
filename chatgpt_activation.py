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

            # Чекбокс НЕ включаем сразу — только если нужно (см. retry логику ниже)

            STEP3_TEXTS = ["Подтвердить пополнение","确认充值","Confirm Recharge","Confirm","确认","充值","Activate","激活","Complete","完成","Submit"]
            # Маркеры: клиент УЖЕ имеет Plus → нужно принудительное пополнение
            ALREADY_PLUS_MARKERS = [
                "уже plus", "уже является plus", "already plus", "already subscribed",
                "пользователь уже", "смените аккаунт",
                "you are currently subscribed", "уже подписан",
            ]
            # Маркеры: обычный сбой (сеть, таймаут) → повтор БЕЗ чекбокса
            GENERIC_RETRY_MARKERS = [
                "пополнение не удалось", "若提交多次", "充值未成功", "充值失败了",
                "提交失败", "recharge failed", "top up failed",
            ]
            MAX_STEP3_RETRIES = 3   # попыток с force-checkbox
            MAX_GENERIC_RETRIES = 2 # попыток без force-checkbox
            generic_retries = 0

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
            # ВАЖНО: success_markers включают специфичные русские фразы первыми
            # чтобы перекрыть случай когда в DOM есть и старый "не удалось" и новый "успешно"
            success_markers = [
                "пополнение успешно", "аккаунт успешно пополнен",
                "успешно активирован", "подписка активирована",
                "recharge successful", "recharge success",
                "充值成功", "充值完成", "激活成功",
                "успешно", "success", "成功", "完成", "activated",
            ]
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

                # ── УСПЕХ ПРОВЕРЯЕМ ПЕРВЫМ ──────────────────────────────────────
                # Это исправляет случай когда DOM содержит и старый "не удалось"
                # и новый "успешно" (после принудительного повтора) — успех должен
                # выигрывать всегда.
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

                # ── СЦЕНАРИЙ 1: клиент уже имеет Plus → включаем force recharge ──
                if any(m in page_text for m in ALREADY_PLUS_MARKERS):
                    step3_retries += 1
                    if step3_retries <= MAX_STEP3_RETRIES:
                        logger.warning(f"⚠️ Клиент уже Plus — включаем принудительное пополнение ({step3_retries}/{MAX_STEP3_RETRIES})")
                        await asyncio.sleep(1.5)
                        await _check_force_checkbox()   # включаем ТОЛЬКО сейчас
                        await asyncio.sleep(1.5)
                        await _click_step3()
                        continue
                    else:
                        ss = None
                        try:
                            ss = await page.screenshot(full_page=True)
                        except Exception:
                            pass
                        final_result = {"success": False, "token_invalid": False, "error": "Клиент уже имеет Plus, принудительное пополнение не помогло. Напиши Александру.", "screenshot": ss}
                        break

                # ── СЦЕНАРИЙ 2: обычный сбой → retry без force checkbox ────────
                elif any(m in page_text for m in GENERIC_RETRY_MARKERS):
                    generic_retries += 1
                    if generic_retries <= MAX_GENERIC_RETRIES:
                        logger.warning(f"⚠️ Обычный сбой — повтор {generic_retries}/{MAX_GENERIC_RETRIES} (без принудительного)")
                        await asyncio.sleep(2.0)
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


async def _aipro_body_text(page) -> str:
    try:
        return await page.inner_text("body")
    except Exception:
        return ""


async def _aipro_ss(page):
    try:
        return await page.screenshot(full_page=True)
    except Exception:
        return None


def _email_from_session(session_json: str) -> str:
    """Достаёт email из полного Session JSON (или из accessToken внутри него)."""
    try:
        import json as _json, base64 as _b64
        obj = _json.loads(session_json)
        u = obj.get("user") or {}
        if u.get("email"):
            return u["email"]
        tok = obj.get("accessToken") or ""
        if tok:
            payload_b64 = tok.split(".")[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            payload = _json.loads(_b64.urlsafe_b64decode(payload_b64))
            prof = payload.get("https://api.openai.com/profile", {})
            return prof.get("email", "") or payload.get("email", "")
    except Exception:
        pass
    return ""


def _parse_member_modal(txt: str):
    """Из текста модалки «аккаунт уже Plus» достаём email и срок действия (если есть)."""
    import re as _re
    _acc = ""
    _m = _re.search(r'[\w.\-+]+@[\w.\-]+\.\w+', txt or "")
    if _m:
        _acc = _m.group(0)
    _until = ""
    _m2 = _re.search(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:\s*UTC)?', txt or "")
    if _m2:
        _until = _m2.group(0)
    return _acc, _until


async def _aipro_click(page, texts):
    """Кликает кнопку/элемент, содержащий один из texts (для модалок Confirmed/Cancelled)."""
    for _t in texts:
        for _sel in [f"button:has-text('{_t}')", f"[role=button]:has-text('{_t}')", f"a:has-text('{_t}')"]:
            try:
                _b = page.locator(_sel).last
                if await _b.count() > 0 and await _b.is_visible():
                    await _b.click(timeout=6000)
                    return True
            except Exception:
                pass
    try:
        return await page.evaluate("""(texts) => {
            const els = Array.from(document.querySelectorAll('button,a,[role=button],div,span'));
            for (const t of texts) {
                for (const e of els) {
                    const s = (e.textContent||'').trim();
                    if (s.includes(t) && s.length < 40) { e.click(); return true; }
                }
            }
            return false;
        }""", texts)
    except Exception:
        return False


async def activate_chatgpt_aipro(cdk_code: str, session_json: str, force: bool = False) -> dict:
    """Активация ChatGPT через 6661231.xyz (AI Pro 充值中心).
    Вводит CDK + полный Session JSON, жмёт «开始充值 (Fast)», ждёт результат.
    Статусы приходят на КИТАЙСКОМ даже при English UI:
      успех=充值成功/已激活 · использован=已被使用 · битый JSON=解析失败/格式错误 · распознан=已识别
    Формат ответа как у activate_chatgpt:
      {success, error, code_already_used, out_of_stock, email, screenshot}
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        return {"success": False, "error": "Playwright не установлен на сервере."}

    url = "https://6661231.xyz/"
    logger.info(f"activate_chatgpt_aipro: cdk={cdk_code} session_len={len(session_json or '')}")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--disable-setuid-sandbox","--single-process"])
        except Exception as launch_err:
            return {"success": False, "error": f"Браузер не запустился: {launch_err}"}

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US")
        page = await context.new_page()
        try:
            await page.goto(url, timeout=45_000, wait_until="networkidle")
            await asyncio.sleep(1.5)

            # Выбрать вкладку GPT (на всякий случай)
            try:
                gpt_tab = page.locator("button:has-text('GPT')").first
                if await gpt_tab.is_visible():
                    await gpt_tab.click()
                    await asyncio.sleep(0.5)
            except Exception:
                pass

            # Поле 1: CDK
            cdk_input = page.locator("input[placeholder*='CDK'], input[placeholder*='bbt']").first
            try:
                await cdk_input.wait_for(state="visible", timeout=15_000)
            except PlaywrightTimeout:
                return {"success": False, "error": "Поле CDK не найдено на сайте.", "screenshot": await _aipro_ss(page)}
            await cdk_input.fill("")
            await cdk_input.fill(cdk_code)
            await asyncio.sleep(0.6)

            # Поле 2: Session JSON
            sess_area = page.locator("textarea").first
            await sess_area.wait_for(state="visible", timeout=10_000)
            await sess_area.fill("")
            await sess_area.fill(session_json)
            await asyncio.sleep(1.2)

            # Ждём распознавания аккаунта (已识别) или ошибки формата
            recognized = False
            for _ in range(20):  # ~10 сек
                txt = await _aipro_body_text(page)
                if "解析失败" in txt or "格式错误" in txt or "not valid JSON" in txt:
                    return {"success": False, "token_invalid": True, "error": "Сайт не принял Session JSON (просрочен/битый). Обнови сессию.", "screenshot": await _aipro_ss(page)}
                if "已识别" in txt:
                    recognized = True
                    break
                await asyncio.sleep(0.5)

            # Клик по кнопке «开始充值 (Fast)».
            # ВНИМАНИЕ: НЕ искать по 'Fast' (это вкладка «Fast Session JSON») и не по одному
            # '充值' (это верхние вкладки «GPT 充值» и т.п.). Уникально только «开始充值».
            # Кнопка может быть НЕ <button>, а <div>/<a> (React) — ищем тег-агностично,
            # и в крайнем случае жмём через JS по самому маленькому элементу с этим текстом.
            clicked = False
            for sel in ["button:has-text('开始充值')", "[role=button]:has-text('开始充值')",
                        "a:has-text('开始充值')", "button:has-text('开始')"]:
                try:
                    b = page.locator(sel).last
                    if await b.count() > 0 and await b.is_visible():
                        await b.click(timeout=8000)
                        clicked = True
                        break
                except Exception:
                    pass
            if not clicked:
                try:
                    clicked = await page.evaluate("""() => {
                        const els = Array.from(document.querySelectorAll('button, a, [role=button], div, span'));
                        let best = null;
                        for (const e of els) {
                            const t = (e.textContent || '');
                            if (t.includes('开始充值') && t.length < 40) {
                                if (!best || t.length < (best.textContent || '').length) best = e;
                            }
                        }
                        if (best) { best.click(); return true; }
                        return false;
                    }""")
                except Exception:
                    clicked = False
            if not clicked:
                return {"success": False, "error": "Кнопка активации не найдена.", "screenshot": await _aipro_ss(page)}

            # Ждём итог. По заметке сайта Fast-充值 занимает ~30 сек – 2 мин → ждём до ~2.5 мин.
            _saw_processing = False
            _forced_info = {}   # заполняется при принудительном пополнении (force поверх активной подписки)
            for _ in range(60):  # ~150 сек
                await asyncio.sleep(2.5)
                txt = await _aipro_body_text(page)
                tl = txt.lower()
                if "充值成功" in txt or "已激活" in txt or "激活成功" in txt:
                    email = _email_from_session(session_json)
                    logger.info(f"aipro успех: cdk={cdk_code} email={email}")
                    return {"success": True, "email": email, **_forced_info}
                if "已被使用" in txt or "已使用" in txt:
                    # Код стал Used ПОСЛЕ обработки/подтверждения force → это МЫ его активировали
                    # (баннер «充值成功» мог не пойматься). Считаем УСПЕХОМ, а не «код занят».
                    if _saw_processing:
                        email = _email_from_session(session_json)
                        logger.info(f"aipro: код Used после обработки — считаем успехом cdk={cdk_code}")
                        return {"success": True, "email": email, **_forced_info}
                    # иначе код был занят ЕЩЁ ДО нашей попытки → берём следующий
                    return {"success": False, "code_already_used": True, "error": f"CDK {cdk_code} уже использован.", "screenshot": await _aipro_ss(page)}
                if "解析失败" in txt or "格式错误" in txt:
                    return {"success": False, "token_invalid": True, "error": "Сайт отклонил Session JSON.", "screenshot": await _aipro_ss(page)}
                if ("库存不足" in txt or "无可用" in txt or "暂无库存" in txt or "无库存" in txt
                        or "已售罄" in txt or "售罄" in txt or "缺货" in txt or "无货" in txt
                        or "out of stock" in tl or "no stock" in tl or "sold out" in tl):
                    return {"success": False, "out_of_stock": True, "error": "Нет стока на сайте (сайт не выдал сертификат).", "screenshot": await _aipro_ss(page)}
                # МОДАЛКА «аккаунт уже Plus» (This account is already a member / 已是会员 / 已订阅)
                # с выбором Confirmed Value / Cancelled Value. Проверяем РАНЬШЕ «в процессе»:
                # на экране одновременно висит и «正在提交…», и модалка — если проверять процесс
                # первым, до модалки дело не дойдёт и уйдём в ложный needs_check.
                if (("already a member" in tl or "already a plus" in tl or "already subscribed" in tl
                        or "已订阅" in txt or "已是会员" in txt or "已是plus" in tl or "已是 plus" in tl
                        or "already have a" in tl or "account is already" in tl)
                        and ("confirmed value" in tl or "确认" in txt or "cancelled value" in tl
                             or "confirm" in tl or "取消" in txt)):
                    _acc = _email_from_session(session_json) or _parse_member_modal(txt)[0]
                    _until = _parse_member_modal(txt)[1]
                    if not force:
                        _shot = await _aipro_ss(page)
                        # безопасно отменяем — аккаунт клиента не трогаем
                        try:
                            await _aipro_click(page, ["取消", "Cancelled Value", "Cancel"])
                        except Exception:
                            pass
                        return {"success": False, "needs_force_confirm": True,
                                "already_account": _acc, "already_until": _until,
                                "error": (f"У аккаунта {_acc} уже есть активная подписка ChatGPT Plus"
                                          + (f" до {_until}" if _until else "")
                                          + ". Принудительная активация начнёт новый месяц (остаток может не суммироваться)."),
                                "screenshot": _shot}
                    # force=True → клиент подтвердил принудительное пополнение
                    logger.warning(f"aipro force-recharge подтверждён клиентом: cdk={cdk_code} acc={_acc}")
                    _forced_info = {"forced": True, "prev_until": _until, "prev_account": _acc}
                    try:
                        await _aipro_click(page, ["确认", "Confirmed Value", "Confirm", "确定"])
                    except Exception:
                        pass
                    _saw_processing = True
                    continue
                # сайт ещё В ПРОЦЕССЕ (提交中 / 正在提交充值请求 / …) — это НЕ ошибка, ждём дальше
                if ("提交中" in txt or "正在提交" in txt or "正在充值" in txt or "处理中" in txt
                        or "请稍候" in txt or "请稍後" in txt or "submitting" in tl or "processing" in tl):
                    _saw_processing = True
                    continue
                # КОНКРЕТНЫЕ признаки протухшей сессии клиента.
                # ВАЖНО: не ловим общие слова "session"/"token"/"login" — они есть в СТАТИЧНОМ
                # интерфейсе сайта (заголовки «ChatGPT Session JSON», ссылка /api/auth/session,
                # шаг «Sign in / login»), иначе получаем ложное «токен истёк».
                if ("重新登录" in txt or "登录失效" in txt or "登录已失效" in txt or "会话已过期" in txt
                        or "会话失效" in txt or "账号异常" in txt or "认证失败" in txt or "授权失败" in txt
                        or "未授权" in txt or "token已失效" in tl or "token 已失效" in txt
                        or "session expired" in tl or "session invalid" in tl or "session has expired" in tl
                        or "token expired" in tl or "token invalid" in tl or "token has expired" in tl
                        or "please log in" in tl or "please login" in tl or "re-login" in tl
                        or "unauthorized" in tl):
                    return {"success": False, "token_invalid": True,
                            "error": "Сайт принял код, но не смог активировать — вероятно, сессия/токен клиента устарели. Нужно вставить свежий Session JSON.",
                            "screenshot": await _aipro_ss(page)}
                # явный сбой пополнения (специфичная фраза)
                if "充值失败" in txt:
                    return {"success": False, "token_invalid": False,
                            "error": "Сайт сообщил о неудаче пополнения (充值失败). Проверь вручную.",
                            "screenshot": await _aipro_ss(page)}
            # Таймаут. Сайт был в процессе или аккаунт распознан → активация МОГЛА пройти:
            # НЕ блэймим токен и НЕ даём авто-повтор (риск двойной), просим админа проверить.
            if _saw_processing or recognized:
                return {"success": False, "needs_check": True,
                        "error": "Активация не подтвердилась за 2.5 мин, но сайт был в процессе. Возможно, уже прошла — проверь вручную на 6661231.xyz перед повторной активацией.",
                        "screenshot": await _aipro_ss(page)}
            return {"success": False, "error": "Активация не завершилась — проверь вручную на 6661231.xyz (возможно, нет стока тарифа).", "screenshot": await _aipro_ss(page)}
        except Exception as e:
            logger.error(f"activate_chatgpt_aipro error: {e}", exc_info=True)
            return {"success": False, "error": f"Ошибка активации: {str(e)[:200]}", "screenshot": await _aipro_ss(page)}
        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def activate_claude_aipro(cdk_code: str, org_id: str, plan: str = "pro") -> dict:
    """Активация Claude через 6661231.xyz (#/claude).
    Вводит CDK + Organization ID, выбирает тариф, жмёт «激活 / Activate the claude code», ждёт результат.
    Статусы приходят на КИТАЙСКОМ даже при English UI:
      успех=充值成功/激活成功/已激活 · использован=已被使用/已使用 · нет стока=库存不足/售罄
      битый org=组织/organization invalid/格式错误
    Формат ответа как у activate_chatgpt_aipro:
      {success, error, code_already_used, out_of_stock, bad_org}
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        return {"success": False, "error": "Playwright не установлен на сервере."}

    url = "https://6661231.xyz/#/claude"
    logger.info(f"activate_claude_aipro: cdk={cdk_code} org={org_id} plan={plan}")

    # какой тариф выбрать на странице
    _plan_labels = {"pro": "Pro", "max_5x": "Max 5x", "max_20x": "Max 20x"}
    plan_label = _plan_labels.get(plan, "Pro")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--disable-setuid-sandbox","--single-process"])
        except Exception as launch_err:
            return {"success": False, "error": f"Браузер не запустился: {launch_err}"}

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US")
        page = await context.new_page()
        try:
            await page.goto(url, timeout=45_000, wait_until="networkidle")
            await asyncio.sleep(1.5)

            # На всякий случай кликаем вкладку Claude (верхний переключатель GPT/Claude/…)
            try:
                cl_tab = page.locator("button:has-text('Claude'), [role=button]:has-text('Claude')").first
                if await cl_tab.count() and await cl_tab.is_visible():
                    await cl_tab.click()
                    await asyncio.sleep(0.6)
            except Exception:
                pass

            # Выбираем тариф (Pro / Max 5x / Max 20x). Pro обычно выбран по умолчанию.
            try:
                _pl = page.locator(f"text={plan_label}").first
                if await _pl.count() and await _pl.is_visible():
                    await _pl.click()
                    await asyncio.sleep(0.4)
            except Exception:
                pass

            # Поле 1: CDK (THE FULL VALUE CODE)
            cdk_input = page.locator(
                "input[placeholder*='CDK'], input[placeholder*='bbc'], input[placeholder*='bbt'], "
                "input[placeholder*='value'], input[placeholder*='code']").first
            try:
                await cdk_input.wait_for(state="visible", timeout=15_000)
            except PlaywrightTimeout:
                # запасной путь: первый input на странице
                cdk_input = page.locator("input").first
                try:
                    await cdk_input.wait_for(state="visible", timeout=8_000)
                except PlaywrightTimeout:
                    return {"success": False, "error": "Поле CDK не найдено на сайте.", "screenshot": await _aipro_ss(page)}
            await cdk_input.fill("")
            await cdk_input.fill(cdk_code)
            await asyncio.sleep(0.6)

            # Поле 2: Organization ID
            org_input = page.locator(
                "input[placeholder*='rganization'], input[placeholder*='rg ID'], "
                "input[placeholder*='2d5'], input[placeholder*='8-4-4']").first
            if not (await org_input.count() and await org_input.is_visible()):
                # запасной путь: второй input
                _all_inp = page.locator("input")
                if await _all_inp.count() >= 2:
                    org_input = _all_inp.nth(1)
            try:
                await org_input.wait_for(state="visible", timeout=10_000)
            except PlaywrightTimeout:
                return {"success": False, "error": "Поле Organization ID не найдено.", "screenshot": await _aipro_ss(page)}
            await org_input.fill("")
            await org_input.fill(org_id)
            await asyncio.sleep(0.8)

            # Кнопка «Activate the claude code» / «激活». НЕ жать верхние вкладки.
            clicked = False
            for sel in ["button:has-text('激活')", "button:has-text('Activate')",
                        "[role=button]:has-text('Activate the claude')", "[role=button]:has-text('激活')",
                        "a:has-text('激活')"]:
                try:
                    b = page.locator(sel).last
                    if await b.count() > 0 and await b.is_visible():
                        await b.click(timeout=8000)
                        clicked = True
                        break
                except Exception:
                    pass
            if not clicked:
                try:
                    clicked = await page.evaluate("""() => {
                        const els = Array.from(document.querySelectorAll('button, a, [role=button], div, span'));
                        let best = null;
                        for (const e of els) {
                            const t = (e.textContent || '');
                            if ((t.includes('激活') || t.toLowerCase().includes('activate the claude')) && t.length < 60) {
                                if (!best || t.length < (best.textContent || '').length) best = e;
                            }
                        }
                        if (best) { best.click(); return true; }
                        return false;
                    }""")
                except Exception:
                    clicked = False
            if not clicked:
                return {"success": False, "error": "Кнопка активации Claude не найдена.", "screenshot": await _aipro_ss(page)}

            # Ждём итог: 充值处理中 → 充值成功 / 已激活. Опрос ЧАЩЕ, чтобы не пропустить баннер успеха.
            # ВАЖНО: на странице ВСЕГДА есть статичные метки тарифов «Sold by» (Max 5x) и
            # «Prepare for line» (Max 20x) — по ним НЕЛЬЗЯ определять «нет стока», иначе ложное oos.
            _saw_processing = False
            _org_l = (org_id or "").lower()
            for _ in range(200):  # ~5 минут при 1.5с (сайт бывает медленным)
                await asyncio.sleep(1.5)
                txt = await _aipro_body_text(page)
                tl = txt.lower()
                # ЯВНЫЙ успех (баннер)
                if ("充值成功" in txt or "激活成功" in txt or "已激活" in txt
                        or "is a success" in tl or "has been upgraded" in tl
                        or "recharge successful" in tl or "recharged successfully" in tl
                        or "activated successfully" in tl):
                    logger.info(f"claude aipro успех: cdk={cdk_code} org={org_id}")
                    return {"success": True}
                # успех по инфо о зачислении: код зачислен НА ORG клиента
                # (метки «recharged account_id / 充值账号 / redeemed at» бывают ТОЛЬКО после зачисления,
                #  в отличие от поля ввода «Organization ID»)
                if _org_l and _org_l in tl and any(
                        k in tl for k in ["recharged account", "account_id", "充值账号", "已充值",
                                          "redeemed at", "recharged"]):
                    logger.info(f"claude aipro успех (зачислено на org): cdk={cdk_code} org={org_id}")
                    return {"success": True}
                # идёт обработка — запоминаем и ждём
                if ("充值处理中" in txt or "处理中" in txt or "请耐心等待" in txt
                        or "processing" in tl or "please wait" in tl or "do not leave" in tl):
                    _saw_processing = True
                    continue
                # у аккаунта уже есть активная подписка (клиент может отменить сам)
                if ("已订阅" in txt or "已是会员" in txt or "已有订阅" in txt or "当前已订阅" in txt
                        or "already subscribed" in tl or "active subscription" in tl
                        or "existing subscription" in tl):
                    return {"success": False, "has_plan": True,
                            "error": "У аккаунта уже есть активная подписка Claude.", "screenshot": await _aipro_ss(page)}
                # неверный Organization ID
                if (("组织" in txt and ("错误" in txt or "无效" in txt)) or "格式错误" in txt
                        or ("organization" in tl and ("invalid" in tl or "not found" in tl))):
                    return {"success": False, "bad_org": True, "error": "Сайт отклонил Organization ID — проверь и попробуй снова.", "screenshot": await _aipro_ss(page)}
                # код помечен «Used/已使用»
                if "已被使用" in txt or "已使用" in txt or "already used" in tl or "already redeemed" in tl:
                    # если ДО этого была «обработка» — код зачислили МЫ (баннер успеха пропустили) → УСПЕХ
                    if _saw_processing:
                        logger.info(f"claude aipro: код стал Used после обработки — считаем успехом cdk={cdk_code}")
                        return {"success": True}
                    # иначе код был занят ЕЩЁ ДО нашей попытки → берём следующий
                    return {"success": False, "code_already_used": True,
                            "error": f"CDK {cdk_code} уже использован.", "screenshot": await _aipro_ss(page)}
                # нет стока — ТОЛЬКО явные фразы дефицита (не метки тарифов «Sold by»/«售罄»)
                if "库存不足" in txt or "暂无库存" in txt or "无库存" in txt or "无可用" in txt:
                    return {"success": False, "out_of_stock": True, "error": "Нет стока тарифа на 6661231.xyz.", "screenshot": await _aipro_ss(page)}
                # иначе — продолжаем ждать результата
            # Таймаут. Если была обработка — активация СКОРЕЕ ВСЕГО прошла, но баннер не пойман:
            # не жжём следующий код и не врём «не прошла» — просим админа проверить по Org ID (со скрином).
            if _saw_processing:
                return {"success": False, "needs_check": True,
                        "error": "Активация, вероятно, прошла (была обработка), но подтверждение не поймано за 5 мин. Проверь на 6661231.xyz по Org ID.",
                        "screenshot": await _aipro_ss(page)}
            return {"success": False, "error": "Активация Claude не завершилась за 5 мин — проверь вручную на 6661231.xyz.", "screenshot": await _aipro_ss(page)}
        except Exception as e:
            logger.error(f"activate_claude_aipro error: {e}", exc_info=True)
            return {"success": False, "error": f"Ошибка активации: {str(e)[:200]}", "screenshot": await _aipro_ss(page)}
        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def activate_claude_ipiap(cdk_code: str, org_id: str, plan: str = "pro") -> dict:
    """Активация Claude через САЙТ ipiap.com (браузер, НЕ API — их API часто сбоит).
    Шаги: ввод CDK → «Verify Activation Code» → ввод Organization ID → «Confirm Recharge»
    → ожидание (до ~2 мин, «Processing…») → «Recharge Successful».
    Формат ответа как у activate_claude_aipro:
      {success, error, code_already_used, out_of_stock, bad_org, has_plan, needs_check, screenshot}."""
    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        return {"success": False, "error": "Playwright не установлен на сервере."}

    url = "https://ipiap.com/#/home/index"
    logger.info(f"activate_claude_ipiap: cdk={cdk_code} org={org_id} plan={plan}")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                      "--disable-setuid-sandbox", "--single-process"])
        except Exception as launch_err:
            return {"success": False, "error": f"Браузер не запустился: {launch_err}"}

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US")
        page = await context.new_page()
        try:
            await page.goto(url, timeout=45_000, wait_until="networkidle")
            await asyncio.sleep(1.5)

            # ── Шаг 1: код активации ──────────────────────────────────────────
            code_input = page.locator(
                "input[placeholder*='activation code'], input[placeholder*='AI-DEMO'], "
                "input[placeholder*='ctivation'], input").first
            try:
                await code_input.wait_for(state="visible", timeout=15_000)
            except PlaywrightTimeout:
                return {"success": False, "error": "Поле кода не найдено на ipiap.com.", "screenshot": await _aipro_ss(page)}
            await code_input.fill("")
            await code_input.fill(cdk_code)
            await asyncio.sleep(0.5)

            if not await _aipro_click(page, ["Verify Activation Code", "Verify", "验证激活码", "验证"]):
                return {"success": False, "error": "Кнопка «Verify Activation Code» не найдена.", "screenshot": await _aipro_ss(page)}

            # ждём шаг 2 (появится «Confirm Recharge» / «Organization ID») либо ошибку кода
            _step2 = False
            for _ in range(24):  # ~12 сек
                await asyncio.sleep(0.5)
                _t = await _aipro_body_text(page); _tl = _t.lower()
                if "已被使用" in _t or "已使用" in _t or "already used" in _tl or "already redeemed" in _tl:
                    return {"success": False, "code_already_used": True,
                            "error": f"CDK {cdk_code} уже использован.", "screenshot": await _aipro_ss(page)}
                if ("invalid" in _tl and ("code" in _tl or "activation" in _tl)) or "not found" in _tl \
                        or "无效" in _t or "不存在" in _t or "已过期" in _t or "expired" in _tl:
                    # код невалиден/просрочен → берём следующий (как not_found)
                    return {"success": False, "code_already_used": True,
                            "error": "Код не принят сайтом (invalid/expired).", "screenshot": await _aipro_ss(page)}
                if "confirm recharge" in _tl or "organization id" in _tl:
                    _step2 = True
                    break
            if not _step2:
                return {"success": False, "error": "Сайт не перешёл к шагу Organization ID после Verify.", "screenshot": await _aipro_ss(page)}

            # ── Шаг 2: Organization ID ────────────────────────────────────────
            # поле org id — видимый input на шаге Recharge (обычно единственный/последний видимый)
            try:
                org_input = page.locator("input:visible").last
                await org_input.wait_for(state="visible", timeout=8_000)
                await org_input.fill("")
                await org_input.fill(org_id)
            except Exception:
                return {"success": False, "error": "Поле Organization ID не найдено.", "screenshot": await _aipro_ss(page)}
            await asyncio.sleep(0.5)

            if not await _aipro_click(page, ["Confirm Recharge", "确认充值", "Confirm"]):
                return {"success": False, "error": "Кнопка «Confirm Recharge» не найдена.", "screenshot": await _aipro_ss(page)}

            # ── Ожидание результата (до ~3 мин) ───────────────────────────────
            _saw_processing = False
            _org_l = (org_id or "").lower()
            for _ in range(100):  # 100 × 2с = 200 сек
                await asyncio.sleep(2.0)
                txt = await _aipro_body_text(page); tl = txt.lower()
                # успех
                if ("recharge successful" in tl or "has been activated" in tl
                        or "activated successfully" in tl or "充值成功" in txt or "激活成功" in txt
                        or "已激活" in txt or ("successful" in tl and "recharge" in tl)):
                    logger.info(f"ipiap успех: cdk={cdk_code} org={org_id}")
                    return {"success": True}
                # идёт обработка — ждём
                if ("processing" in tl or "please wait" in tl or "do not close" in tl
                        or "充值处理中" in txt or "处理中" in txt or "请耐心等待" in txt):
                    _saw_processing = True
                    continue
                # уже есть подписка
                if ("already a member" in tl or "already subscribed" in tl or "已订阅" in txt
                        or "已是会员" in txt or "active subscription" in tl):
                    return {"success": False, "has_plan": True,
                            "error": "У аккаунта уже есть активная подписка Claude.", "screenshot": await _aipro_ss(page)}
                # неверный Organization ID
                if (("organization" in tl and ("invalid" in tl or "not found" in tl or "incorrect" in tl))
                        or ("组织" in txt and ("错误" in txt or "无效" in txt)) or "格式错误" in txt):
                    return {"success": False, "bad_org": True,
                            "error": "Сайт отклонил Organization ID — проверь и попробуй снова.", "screenshot": await _aipro_ss(page)}
                # код стал Used после обработки → это наш успех
                if "已被使用" in txt or "已使用" in txt or "already used" in tl:
                    if _saw_processing:
                        logger.info(f"ipiap: код Used после обработки — успех cdk={cdk_code}")
                        return {"success": True}
                    return {"success": False, "code_already_used": True,
                            "error": f"CDK {cdk_code} уже использован.", "screenshot": await _aipro_ss(page)}
                # явный сбой
                if "failed" in tl or "充值失败" in txt or "激活失败" in txt or "错误" in txt and "系统" in txt:
                    return {"success": False, "error": "Сайт сообщил об ошибке пополнения (проверь вручную).",
                            "screenshot": await _aipro_ss(page)}
            # таймаут: был процесс → вероятно прошла, просим проверить
            if _saw_processing:
                return {"success": False, "needs_check": True,
                        "error": "Активация не подтвердилась за 3 мин, но сайт был в процессе. Проверь на ipiap.com по Org ID.",
                        "screenshot": await _aipro_ss(page)}
            return {"success": False, "error": "Активация Claude на ipiap.com не завершилась — проверь вручную.",
                    "screenshot": await _aipro_ss(page)}
        except Exception as e:
            logger.error(f"activate_claude_ipiap error: {e}", exc_info=True)
            return {"success": False, "error": f"Ошибка активации: {str(e)[:200]}", "screenshot": await _aipro_ss(page)}
        finally:
            try:
                await browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) != 3:
        print("Использование: python chatgpt_activation.py <CARD_CODE> <ACCESS_TOKEN>")
        sys.exit(1)
    result = asyncio.run(activate_chatgpt(sys.argv[1], sys.argv[2]))
    print(result)
