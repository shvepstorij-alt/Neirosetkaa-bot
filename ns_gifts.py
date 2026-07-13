"""
NS Gifts API v2 — async client.
https://api.ns.gifts/api-docs

Авторизация: api_secret (постоянный) + session token (TTL 2ч, из /get_token).
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://api.ns.gifts"


class NSGiftsClient:
    def __init__(self, user_id: int, login: str, password: str, api_secret: str,
                 proxy: str = ""):
        self.user_id    = user_id
        self.login      = login
        self.password   = password
        self.api_secret = api_secret
        self.proxy      = proxy or None     # None → без прокси

        self._token: Optional[str] = None
        self._token_expires: float = 0.0
        self._lock = asyncio.Lock()         # защита от параллельного рефреша

    # ── Подпись ────────────────────────────────────────────────────────────────

    def _sign(self, method: str, path: str, query: str,
              body: bytes, ts: str, token: Optional[str]) -> str:
        body_hash = hashlib.sha256(body or b"").hexdigest()
        parts = [method.upper(), path, query, ts]
        if token is not None:
            parts.append(token)
        parts.append(body_hash)
        sts = "\n".join(parts).encode()
        key = base64.b64decode(self.api_secret)
        digest = hmac.new(key, sts, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _make_headers(self, method: str, path: str, query: str,
                      body: bytes, token: Optional[str]) -> dict:
        ts  = str(int(time.time()))
        sig = self._sign(method, path, query, body, ts, token)
        h = {
            "X-User-Id":   str(self.user_id),
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
        }
        if token:
            h["X-Token"] = token
        return h

    # ── Токен ──────────────────────────────────────────────────────────────────

    async def _ensure_token(self):
        """Получает / обновляет токен если истёк (с запасом 5 мин)."""
        if self._token and time.time() < self._token_expires - 300:
            return
        async with self._lock:
            # Повторная проверка под локом
            if self._token and time.time() < self._token_expires - 300:
                return
            await self._refresh_token()

    async def _refresh_token(self):
        body = json.dumps(
            {"login": self.login, "password": self.password},
            separators=(",", ":")
        ).encode()
        path = "/api/v2/get_token"
        # Railway HA-static-IP: исходящий egress-IP варьируется по соединению (3 общих IP).
        # NS Gifts на не-белый IP отвечает 403 «Invalid login details». Креды верные →
        # повторяем логин с НОВЫМ соединением: другое соединение может уйти с белого IP.
        _last = "unknown"
        for _i in range(6):
            headers = self._make_headers("POST", path, "", body, token=None)  # ts обновляем каждый раз
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        BASE_URL + path, headers=headers, data=body,
                        proxy=self.proxy,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as r:
                        data = await r.json()
                        if r.status == 200:
                            self._token         = data["token"]
                            self._token_expires = time.time() + data.get("expires_in", 7200)
                            logger.info(f"NSGifts token refreshed (попытка {_i + 1})")
                            return
                        _last = f"{r.status}: {data}"
                        if r.status != 403:
                            break   # не IP-проблема (напр. 400/500) — нет смысла повторять
            except Exception as _e:
                _last = str(_e)
            await asyncio.sleep(0.7)
        raise RuntimeError(f"NSGifts login failed {_last}")

    # ── Базовый запрос ─────────────────────────────────────────────────────────

    async def _call(self, method: str, path: str,
                    params: Optional[dict] = None,
                    json_body: Optional[dict] = None) -> dict:
        await self._ensure_token()
        query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        body  = (
            b"" if json_body is None
            else json.dumps(json_body, separators=(",", ":")).encode()
        )
        headers = self._make_headers(method, path, query, body, self._token)
        url = BASE_URL + path + (f"?{query}" if query else "")

        async with aiohttp.ClientSession() as s:
            async with s.request(
                method, url, headers=headers, data=body,
                proxy=self.proxy,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                if r.status == 401:
                    # Токен истёк — рефреш и повтор
                    async with self._lock:
                        await self._refresh_token()
                    headers = self._make_headers(method, path, query, body, self._token)
                    async with s.request(
                        method, url, headers=headers, data=body,
                        proxy=self.proxy,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as r2:
                        r2.raise_for_status()
                        return await r2.json()
                r.raise_for_status()
                return await r.json()

    # ── Публичные методы ───────────────────────────────────────────────────────

    async def get_stock(self) -> dict:
        """Каталог: категории → сервисы с ценами и остатками."""
        return await self._call("GET", "/api/v2/stock")

    async def create_order(self, service_id: int, quantity: int = 1) -> dict:
        """
        Создаёт заказ. Возвращает custom_id + total_to_pay.
        Оплата — отдельно через pay_order().
        """
        custom_id = str(uuid.uuid4())
        resp = await self._call("POST", "/api/v2/create_order", json_body={
            "service_id": service_id,
            "custom_id":  custom_id,
            "fields":     [{"key": "quantity", "value": quantity}],
        })
        resp["_custom_id"] = custom_id   # удобный алиас если API его не вернул
        return resp

    async def pay_order(self, custom_id: str) -> dict:
        """
        Подтверждает оплату. Возвращает status + pins.
        status: completed | insufficient | refunded | in_progress
        """
        return await self._call("POST", "/api/v2/pay_order",
                                json_body={"custom_id": custom_id})

    async def order_info(self, custom_id: str) -> dict:
        """Полная информация о заказе (статус, пины, сумма)."""
        return await self._call("GET", f"/api/v2/order_info/{custom_id}")

    async def check_balance(self) -> float:
        """Текущий баланс кабинета в USD."""
        resp = await self._call("GET", "/api/v2/check_balance")
        return float(resp["balance"])


# ── Кеш каталога ───────────────────────────────────────────────────────────────

_stock_cache: dict = {"data": None, "ts": 0.0}
_CACHE_TTL = 1800  # 30 минут


async def get_stock_cached(client: NSGiftsClient) -> dict:
    """
    Возвращает каталог из кеша. Обновляет если кеш устарел.
    Безопасно вызывать из нескольких хендлеров одновременно.
    """
    if _stock_cache["data"] and time.time() - _stock_cache["ts"] < _CACHE_TTL:
        return _stock_cache["data"]
    try:
        data = await client.get_stock()
        _stock_cache["data"] = data
        _stock_cache["ts"]   = time.time()
        return data
    except Exception as e:
        logger.error(f"NSGifts get_stock failed: {e}")
        return _stock_cache["data"] or {}   # вернуть устаревший кеш при ошибке


def invalidate_stock_cache():
    """Сбросить кеш вручную (например после изменения настроек)."""
    _stock_cache["data"] = None
    _stock_cache["ts"]   = 0.0


# ── Хелперы для Apple Gift Card ────────────────────────────────────────────────

# Флаги регионов по ключевым словам в названии категории
REGION_FLAGS = {
    "russia":      "🇷🇺",
    "rus":         "🇷🇺",
    "рос":         "🇷🇺",
    "usa":         "🇺🇸",
    "united states": "🇺🇸",
    "turkey":      "🇹🇷",
    "turk":        "🇹🇷",
    "kazakhstan":  "🇰🇿",
    "kz":          "🇰🇿",
    "казах":       "🇰🇿",
    "ukraine":     "🇺🇦",
    "ukr":         "🇺🇦",
    "uk":          "🇬🇧",
    "united kingdom": "🇬🇧",
    "europe":      "🇪🇺",
    "eur":         "🇪🇺",
    "germany":     "🇩🇪",
    "france":      "🇫🇷",
    "china":       "🇨🇳",
    "uae":         "🇦🇪",
    "brazil":      "🇧🇷",
    "india":       "🇮🇳",
    "japan":       "🇯🇵",
    "canada":      "🇨🇦",
    "australia":   "🇦🇺",
    "mexico":      "🇲🇽",
    "saudi":       "🇸🇦",
    "south korea": "🇰🇷",
}


_CODE_ALIASES = {"UK": "GB"}  # «UK» — не ISO-код, флаг Британии = GB

def _iso2_to_flag(code: str) -> str:
    """2-буквенный ISO-код страны → эмодзи-флаг (AE → 🇦🇪, UK → 🇬🇧)."""
    code = code.strip().upper()
    code = _CODE_ALIASES.get(code, code)
    if len(code) == 2 and code.isalpha():
        return chr(0x1F1E6 + ord(code[0]) - 65) + chr(0x1F1E6 + ord(code[1]) - 65)
    return ""


def region_flag(category_name: str) -> str:
    # 1) 2-буквенный код страны после "|" (формат "Apple Gift Card | AE")
    for part in reversed(category_name.split("|")):
        f = _iso2_to_flag(part)
        if f:
            return f
    # 2) по названию страны (USA, Russia и т.п.)
    lower = category_name.lower()
    for kw, flag in REGION_FLAGS.items():
        if kw in lower:
            return flag
    return "🌐"


def get_apple_categories(stock: dict) -> list[dict]:
    """
    Возвращает категории из каталога где есть Apple/AppStore/iTunes.
    Только те у которых есть хотя бы один сервис в наличии.
    """
    result = []
    for cat in stock.get("categories", []):
        name = cat.get("category_name", "").lower()
        if not ("apple" in name or "appstore" in name
                or "app store" in name or "itunes" in name):
            continue
        # хотя бы один товар в наличии
        if any(s.get("in_stock", 0) > 0 for s in cat.get("services", [])):
            result.append(cat)
    return sorted(result, key=lambda c: c["category_name"])


def calc_price_rub(price_usd: float, usd_rate: float, markup_pct: float) -> int:
    """Цена для клиента в рублях: закупка_$ × курс × (1 + наценка/100),
    округлённая ВВЕРХ до красивого числа (кратно 10/50/100)."""
    import math
    rub = price_usd * usd_rate * (1 + markup_pct / 100)
    step = 10 if rub < 1000 else (50 if rub < 5000 else 100)
    return max(step, int(math.ceil(rub / step) * step))
