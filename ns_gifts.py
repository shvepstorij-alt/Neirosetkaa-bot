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
import re as _re
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
        # .strip(): переменные Railway/окружения часто содержат хвостовой \n или пробел
        # при копипасте — глазами не видно, а сервер отвечает 403 «Invalid login details».
        self.login      = (login or "").strip()
        self.password   = (password or "").strip()
        self.api_secret = (api_secret or "").strip()
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


# Слияние «раздробленных» брендов в один продукт. Каждое правило: набор подстрок
# (все должны встретиться в имени, в нижнем регистре) → каноничное имя бренда.
# Напр. 'amazon.ae Gift Card', 'amazon.au Gift Card' → один 'Amazon Gift Card'.
# Список легко расширять новыми слияниями.
_CANON_RULES = [
    (("amazon", "gift"), "Amazon Gift Card"),
]


def _canon_brand(name: str) -> str:
    low = (name or "").lower()
    for kws, canon in _CANON_RULES:
        if all(k in low for k in kws):
            return canon
    return name


# Региональные/локализационные хвосты в НАЗВАНИИ товара (без '|'): напр.
# 'Mobile Legends: Bang Bang Top Up BR', 'Free Fire Top Up CIS',
# 'Delta Force Mobile Top Up MENA (Garena)'. Срезаем их, чтобы варианты одной
# игры складывались в одну папку.
_REGION_TOKENS = {
    "cis", "mena", "sea", "latam", "asia", "eu", "eur", "us", "usa", "uk", "global",
    "gl", "glob", "row", "na", "emea", "apac", "ww", "intl", "int",
    "br", "id", "my", "ph", "ru", "tr", "in", "jp", "kr", "th", "vn", "sa", "ae",
    "eg", "hk", "tw", "de", "fr", "it", "es", "nl", "pl", "pt", "be", "ca", "au",
    "mx", "ar", "cl", "co", "pe", "ua", "kz", "by", "ge", "az", "am", "garena",
}


def _strip_region_suffix(name: str) -> str:
    n = (name or "").strip()
    while True:
        before = n
        # хвостовая скобочная группа "(...)" (напр. "(Garena)")
        n = _re.sub(r"\s*\([^)]*\)\s*$", "", n).strip()
        # хвостовой регион-токен после разделителя (пробел/-/|//)
        m = _re.search(r"[\s|/\-]+([A-Za-z]{2,6})$", n)
        if m and m.group(1).lower() in _REGION_TOKENS:
            cand = n[:m.start()].strip()
            if cand:
                n = cand
        if n == before:
            break
    return n or (name or "").strip()


# Базовое имя франшизы: обрезаем версию/издание/подзаголовок, чтобы разные части
# одной серии (Dead Rising 2/3/4/Remaster, Dark Souls II/Remastered, Dishonored 2…)
# складывались в одну папку.
_ROMAN = {"ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii", "xiv"}
_EDITION_KW = {"edition", "remaster", "remastered", "deluxe", "definitive", "goty",
               "complete", "redux", "anniversary", "collection"}
_TM_RE = _re.compile(r"[™®©]")


def _clean_title(s: str) -> str:
    return _re.sub(r"\s+", " ", _TM_RE.sub("", s or "")).strip()


def _franchise_base(name: str) -> str:
    n = _clean_title(name).split(":")[0].strip()
    out = []
    for t in n.split():
        tl = t.strip(".,").lower()
        if tl.isdigit() or tl in _ROMAN or tl in _EDITION_KW:
            break
        out.append(t)
    base = " ".join(out).strip()
    return base or n or _clean_title(name)


def brand_of(category_name: str) -> str:
    """Базовое имя продукта/франшизы: до '|', минус регион-хвост, слияние
    раздробленных брендов (Amazon), минус версия/издание."""
    name = (category_name or "").split("|")[0].strip()
    name = name or (category_name or "").strip()
    name = _canon_brand(_strip_region_suffix(name))
    return _franchise_base(name)


def variant_label(folder_brand: str, category_name: str) -> str:
    """Метка варианта внутри папки: регион после '|' или версия/издание/подзаголовок."""
    if "|" in (category_name or ""):
        return category_name.split("|", 1)[1].strip() or _clean_title(category_name)
    n = _clean_title(category_name)
    fb = (folder_brand or "").strip()
    if fb and n.lower().startswith(fb.lower()):
        suf = n[len(fb):].strip(" -:/|")
        return suf or "Оригинал"
    return n or "Оригинал"


def brand_token(name: str) -> str:
    """Стабильный токен папки для callback_data (по нормализованному ключу)."""
    return hashlib.md5((name or "").strip().casefold().encode("utf-8")).hexdigest()[:10]


def is_apple_brand(brand: str) -> bool:
    b = (brand or "").lower()
    return ("apple" in b or "app store" in b or "itunes" in b)


def _cat_in_stock(cat: dict) -> bool:
    return any(s.get("in_stock", 0) > 0 for s in cat.get("services", []))


# Классификация продукта по типу (в каталоге нет поля продукта — определяем эвристикой)
_TOPUP_KW = ("top up", "topup", "top-up", "recharge", "donation", "donate", "пополн")
_GIFT_KW  = ("gift card", "giftcard", "wallet", "cash card", "gift  card")
_GIFT_BRAND_KW = (
    "apple", "amazon", "netflix", "spotify", "google play", "razer", "playstation",
    "xbox", "nintendo", "battle.net", "battlenet", "origin", "roblox", "riot",
    "steam wallet", "twitch", "telegram", "gift card", "music", "streaming",
    "social network",
)


def classify_bucket(brand: str, names_blob: str = "") -> str:
    """Тип продукта: 'topup' | 'gift' | 'game'."""
    b = (brand or "").lower()
    text = b + " " + (names_blob or "").lower()
    if any(k in text for k in _TOPUP_KW):
        return "topup"
    if any(k in text for k in _GIFT_KW):
        return "gift"
    # «… Games» (Steam Games, Xbox Games) — это игры, даже если бренд игровой.
    if "games" in b:
        return "game"
    if any(k in b for k in _GIFT_BRAND_KW):
        return "gift"
    return "game"


BUCKETS = [
    ("gift",  "💳 Гифт-карты и подписки"),
    ("game",  "🎮 Игры"),
    ("topup", "🔝 Пополнения (Top Up)"),
]
BUCKET_TITLES = dict(BUCKETS)


def _best_display(displays: dict) -> str:
    """Из вариантов написания выбираем самое «нормальное» (меньше ЗАГЛАВНЫХ), затем частое."""
    if not displays:
        return ""
    return sorted(displays, key=lambda s: (sum(ch.isupper() for ch in s), -displays[s]))[0]


_catalog_cache = {"key": None, "data": None}


def build_catalog(stock: dict) -> dict:
    """Единая кластеризация каталога в папки-продукты (с кэшем).
    Пасс 1: базовое имя франшизы. Пасс 2: слияние по общему префиксу
    (DOOM Eternal + DOOM: The Dark Ages → DOOM).
    → {'folders':[{brand,token,key,bucket,cats,min_usd,_cat_objs}], 'by_token', 'by_cat'}."""
    cats = stock.get("categories", []) if isinstance(stock, dict) else []
    ck = (id(stock), len(cats))
    if _catalog_cache["key"] == ck and _catalog_cache["data"] is not None:
        return _catalog_cache["data"]
    incat = [c for c in cats if _cat_in_stock(c)]
    base_by_cat = {c["category_id"]: brand_of(c.get("category_name", "")) for c in incat}
    base_lower = {}
    for b in base_by_cat.values():
        base_lower.setdefault(b.casefold(), b)

    def _product(base: str) -> str:
        words = base.split()
        for k in range(1, len(words)):
            pref = " ".join(words[:k]).casefold()
            if pref in base_lower:
                return base_lower[pref]
        return base

    groups: dict = {}
    for c in incat:
        disp = _product(base_by_cat[c["category_id"]])
        key = disp.casefold()
        g = groups.setdefault(key, {"displays": {}, "cats": [], "names": [], "min_usd": None})
        g["displays"][disp] = g["displays"].get(disp, 0) + 1
        g["cats"].append(c)
        g["names"].append(c.get("category_name", ""))
        for s in c.get("services", []):
            if s.get("in_stock", 0) > 0:
                p = s.get("price")
                if p is not None and (g["min_usd"] is None or p < g["min_usd"]):
                    g["min_usd"] = p
    folders = []
    for key, g in groups.items():
        disp = _best_display(g["displays"])
        objs = sorted(g["cats"], key=lambda c: c.get("category_name", ""))
        # Папка из одной игры без регионов ('|') — показываем ПОЛНОЕ название
        # (иначе 'Destiny 2' → 'Destiny', 'Directive 8020' → 'Directive').
        if len(objs) == 1 and "|" not in objs[0].get("category_name", ""):
            disp = _clean_title(objs[0].get("category_name", "")) or disp
        folders.append({
            "brand": disp, "token": brand_token(key), "key": key,
            "bucket": classify_bucket(disp, " ".join(g["names"])),
            "cats": len(objs), "min_usd": g["min_usd"], "_cat_objs": objs,
        })
    folders.sort(key=lambda x: x["brand"].lower())
    data = {"folders": folders,
            "by_token": {f["token"]: f for f in folders},
            "by_cat": {c["category_id"]: f for f in folders for c in f["_cat_objs"]}}
    _catalog_cache["key"] = ck
    _catalog_cache["data"] = data
    return data


def get_all_brands(stock: dict) -> list[dict]:
    return build_catalog(stock)["folders"]


def get_brands_by_bucket(stock: dict, bucket: str) -> list[dict]:
    return [f for f in get_all_brands(stock) if f["bucket"] == bucket]


def get_buckets_present(stock: dict) -> list[tuple]:
    """Типы, реально присутствующие в каталоге (в порядке BUCKETS), с количеством."""
    cnt: dict = {}
    for f in get_all_brands(stock):
        cnt[f["bucket"]] = cnt.get(f["bucket"], 0) + 1
    return [(k, title, cnt[k]) for k, title in BUCKETS if cnt.get(k)]


def get_folder_by_token(stock: dict, token: str):
    return build_catalog(stock)["by_token"].get(token)


def get_folder_by_category(stock: dict, cat_id: int):
    return build_catalog(stock)["by_cat"].get(cat_id)


def get_brand_categories(stock: dict, token_or_brand: str) -> list[dict]:
    """Категории папки (по token или по имени)."""
    cat = build_catalog(stock)
    f = cat["by_token"].get(token_or_brand)
    if not f:
        low = (token_or_brand or "").casefold()
        f = next((x for x in cat["folders"] if x["key"] == low or x["brand"].casefold() == low), None)
    return list(f["_cat_objs"]) if f else []


def find_category(stock: dict, cat_id: int) -> Optional[dict]:
    for c in stock.get("categories", []):
        if c.get("category_id") == cat_id:
            return c
    return None


def get_brand_by_token(stock: dict, token: str) -> str:
    f = get_folder_by_token(stock, token)
    return f["brand"] if f else ""


def brand_bucket(stock: dict, brand_or_token: str) -> str:
    cat = build_catalog(stock)
    f = cat["by_token"].get(brand_or_token)
    if not f:
        low = (brand_or_token or "").casefold()
        f = next((x for x in cat["folders"] if x["key"] == low or x["brand"].casefold() == low), None)
    return f["bucket"] if f else "game"


def brand_first_letter(brand: str) -> str:
    """Буква-раздел для алфавитного указателя: 'Battle.net'→'B', '7 Days'→'0-9'."""
    ch = (brand or "").strip()[:1].upper()
    if ch.isdigit():
        return "0-9"
    if ch.isalpha():
        return ch
    return "#"


def get_brand_letters(stock: dict, bucket: str = "") -> list[str]:
    """Список букв-разделов (опц. внутри типа), отсортирован."""
    src = get_brands_by_bucket(stock, bucket) if bucket else get_all_brands(stock)
    letters = {brand_first_letter(b["brand"]) for b in src}
    def _key(x):
        return (0, x) if x.isalpha() and len(x) == 1 else ((1, x) if x == "0-9" else (2, x))
    return sorted(letters, key=_key)


def get_brands_by_letter(stock: dict, letter: str, bucket: str = "") -> list[dict]:
    src = get_brands_by_bucket(stock, bucket) if bucket else get_all_brands(stock)
    return [b for b in src if brand_first_letter(b["brand"]) == letter]


def calc_price_rub(price_usd: float, usd_rate: float, markup_pct: float) -> int:
    """Цена для клиента в рублях: закупка_$ × курс × (1 + наценка/100),
    округлённая ВВЕРХ до красивого числа (кратно 10/50/100)."""
    import math
    rub = price_usd * usd_rate * (1 + markup_pct / 100)
    step = 10 if rub < 1000 else (50 if rub < 5000 else 100)
    return max(step, int(math.ceil(rub / step) * step))
