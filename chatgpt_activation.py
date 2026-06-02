"""
chatgpt_activation.py
Автоматическая активация ChatGPT подписки через 987ai.vip с помощью Playwright.

Railway: браузер хранится в /app/pw-browsers (попадает в Docker-образ).
Путь задаётся через PLAYWRIGHT_BROWSERS_PATH до любого импорта playwright.
"""

import asyncio
import logging
import os

# ── ВАЖНО: устанавливаем путь к браузеру ДО импорта playwright ──────────────
# /tmp/pw-browsers — всегда доступен на запись в любом контейнере Railway.
# Браузер скачивается один раз при старте бота (см. _ensure_playwright_browser в bot.py).
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/tmp/pw-browsers")

logger = logging.getLogger(__name__)


async def activate_chatgpt(card_code: str, access_token: str) -> dict:
    """
    Активирует подписку ChatGPT через сайт 987ai.vip.

    Args:
        card_code:    Код карты (например BYPRICEZ2VAXIC9R)
        access_token: accessToken из chatgpt.com/api/auth/session

    Returns:
        {"success": True, "message": "..."} или {"success": False, "error": "..."}
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        return {
            "success": False,
            "error": "Playwright не установлен. Выполни: pip install playwright"
        }

    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/app/pw-browsers")
    logger.info(f"Playwright browsers path: {browsers_path}")

    url = f"https://www.987ai.vip/recharge?card={card_code}"

    async with async_playwright() as p:
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
                "button:has-text('Check Card')"
            ).first
            await verify_btn.wait_for(state="visible", timeout=10_000)
            await verify_btn.click()
            logger.info("Нажали 'Проверить карту'")

            # ── ШАГ 2: Ввести токен ───────────────────────────────────────
            token_area = page.locator(
                "textarea, "
                "input[type='text']:not([value]):not([readonly])"
            ).last
            await token_area.wait_for(state="visible", timeout=20_000)
            await asyncio.sleep(1.0)
            await token_area.fill("")
            await token_area.fill(access_token)
            await asyncio.sleep(0.5)
            logger.info("Токен введён")

            confirm_btn = page.locator(
                "button:has-text('Проверить аккаунт'), "
                "button:has-text('验证账号'), "
                "button:has-text('Verify Account'), "
                "button:has-text('Next'), "
                "button:has-text('下一步')"
            ).last
            await confirm_btn.wait_for(state="visible", timeout=10_000)
            await confirm_btn.click()
            logger.info("Нажали 'Проверить аккаунт'")

            # ── ШАГ 3: Подтвердить пополнение ────────────────────────────
            await asyncio.sleep(2.0)

            final_btn = page.locator(
                "button:has-text('Подтвердить пополнение'), "
                "button:has-text('确认充值'), "
                "button:has-text('Confirm Recharge'), "
                "button:has-text('Confirm'), "
                "button:has-text('确认')"
            ).last
            try:
                await final_btn.wait_for(state="visible", timeout=15_000)
                await final_btn.click()
                logger.info("Нажали 'Подтвердить пополнение'")
            except PlaywrightTimeout:
                logger.warning("Кнопка шага 3 не найдена — проверяем текст страницы")

            await asyncio.sleep(3.0)

            # ── Проверяем результат ───────────────────────────────────────
            page_text = (await page.inner_text("body")).lower()

            success_markers = [
                "успешно", "success", "成功", "充值成功",
                "activated", "активирован", "подписка активирована"
            ]
            error_markers = [
                "ошибка", "error", "失败", "неверный токен",
                "invalid token", "token expired", "токен истёк",
                "не найден", "not found", "войдите снова"
            ]

            for marker in success_markers:
                if marker in page_text:
                    logger.info(f"Активация успешна (маркер: {marker})")
                    return {"success": True, "message": "Подписка успешно активирована!"}

            for marker in error_markers:
                if marker in page_text:
                    logger.warning(f"Ошибка активации (маркер: {marker})")
                    return {"success": False, "error": _extract_error_text(page_text)}

            # Нет явных маркеров — скриншот для диагностики
            screenshot = await page.screenshot(full_page=True)
            logger.warning("Результат активации неизвестен, нет явных маркеров")
            return {
                "success": False,
                "error": "Не удалось определить результат. Обратитесь к Александру.",
                "screenshot": screenshot
            }

        except PlaywrightTimeout as e:
            logger.error(f"Таймаут на 987ai.vip: {e}")
            return {"success": False, "error": "Сайт не ответил вовремя. Попробуй снова."}

        except Exception as e:
            logger.error(f"Ошибка активации: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

        finally:
            await browser.close()


def _extract_error_text(page_text: str) -> str:
    for line in page_text.splitlines():
        line = line.strip()
        if len(line) > 5 and any(m in line for m in ["ошибка", "error", "失败", "invalid", "expired"]):
            return line[:200]
    return "Ошибка активации. Возможно токен истёк или уже использован."


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) != 3:
        print("Использование: python chatgpt_activation.py <CARD_CODE> <ACCESS_TOKEN>")
        sys.exit(1)
    result = asyncio.run(activate_chatgpt(sys.argv[1], sys.argv[2]))
    print(result)
