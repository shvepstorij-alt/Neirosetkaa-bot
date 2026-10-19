# -*- coding: utf-8 -*-
"""Клиент «9527 Agent API v1» — сайт aichongz.top (Александр 14.10.2026).

Документация продавца: контракт 2026-10-14-r14. Главное, на чём построен модуль:

  • Все запросы — POST с JSON. Подпись HMAC-SHA256 от строки
    METHOD\\nPATH\\nTIMESTAMP\\nNONCE\\nsha256(тело), заголовки X-Api-Key,
    X-Timestamp, X-Nonce, X-Signature: v1=<hex>.
  • Активация: POST /redemptions. 201 — заказ СОЗДАН, а не выполнен. Успех
    подтверждает только status=success.
  • Idempotency-Key — один на логическую операцию. Повтор после обрыва — тот же
    ключ и ТЕ ЖЕ байты тела, новые nonce/время. Тайм-аут и 5xx не доказывают,
    что заказа нет: сначала спросить код, потом повторять тем же ключом.
  • manual_review — НЕ отказ и НЕ возврат: автоматика остановлена, решает
    продавец. failed с autoRetryState scheduled/checking/attempting — сайт ещё
    работает.
  • /cards/query читает локальный снимок и может отставать.

Ключ и секрет — ТОЛЬКО из переменных окружения (AICHONGZ_API_KEY,
AICHONGZ_API_SECRET). Тело запроса (в нём Session клиента) в лог не пишем.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

import aiohttp

logger = logging.getLogger(__name__)

PREFIX = "/api/agent/v1"


def _env_base(var: str = "AICHONGZ_BASE", default: str = "https://aichongz.top") -> str:
    b = (os.getenv(var, "") or default or "").strip().rstrip("/")
    if not b:
        return ""
    if b.endswith(PREFIX):
        b = b[: -len(PREFIX)]
    if b.startswith("http://"):
        # Секрет и Session клиента по открытому HTTP не отправляем.
        b = "https://" + b[len("http://"):]
    return b


BASE = _env_base()
API_KEY = (os.getenv("AICHONGZ_API_KEY", "") or "").strip()
API_SECRET = (os.getenv("AICHONGZ_API_SECRET", "") or "").strip()
# Claude может идти через ДРУГОЙ домен/магазин той же платформы 9527 (поставщик
# 14.10.2026: «Claude пока на 9527.mobi»). Пустые значения — берутся общие.
CLAUDE_BASE = _env_base("AICHONGZ_CLAUDE_BASE", "")
CLAUDE_API_KEY = (os.getenv("AICHONGZ_CLAUDE_API_KEY", "") or "").strip()
CLAUDE_API_SECRET = (os.getenv("AICHONGZ_CLAUDE_API_SECRET", "") or "").strip()


def creds(svc: str = "gpt") -> tuple:
    """(base, key, secret) для сервиса. svc: 'gpt' | 'chatgpt' | 'claude'."""
    if svc == "claude":
        return (CLAUDE_BASE or BASE, CLAUDE_API_KEY or API_KEY,
                CLAUDE_API_SECRET or (API_SECRET if not CLAUDE_API_KEY else ""))
    return BASE, API_KEY, API_SECRET


def site_host(svc: str = "gpt") -> str:
    b = creds(svc)[0]
    return b.replace("https://", "").replace("http://", "").split("/")[0] or "aichongz.top"
SITE_NAME = "aichongz.top"


def ready(svc: str = "gpt") -> bool:
    b, k, sec = creds(svc)
    return bool(k and sec and b)


# ── Состояния (английские коды из документации) ─────────────────────────────
ORDER_WAIT = ("submitting", "submitted", "processing", "pending_check", "pending",
              "running", "queued", "")
AUTO_RETRY_ACTIVE = ("scheduled", "checking", "attempting")
# Значения autoRetryState, которые точно значат «автоповторов больше нет».
# Любое другое непустое значение считаем активным: ошибиться в сторону
# «ещё ждём» дешевле, чем объявить провал и взять второй код.
AUTO_RETRY_DONE = ("", "none", "idle", "exhausted", "stopped", "done", "finished",
                   "failed", "disabled", "cancelled", "canceled", "completed", "skipped")


def sign(method: str, path: str, ts: str, nonce: str, body: bytes, secret: str) -> str:
    canonical = "\n".join([method.upper(), path, ts, nonce, hashlib.sha256(body).hexdigest()])
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def body_bytes(payload: dict) -> bytes:
    """Сериализация ОДИН раз: повтор того же ключа обязан нести те же байты."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def idem_key(*parts) -> str:
    """Детерминированный ключ: 8–160 ASCII. Одни и те же части — один ключ."""
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return "gpt11-" + hashlib.sha256(raw).hexdigest()[:40]


def _headers(path: str, body: bytes, idem: str = "", svc: str = "gpt") -> dict:
    _b, _key, _sec = creds(svc)
    ts = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    h = {
        "Content-Type": "application/json",
        "User-Agent": "9527-Agent-API/1.0",
        "X-Api-Key": _key,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Signature": "v1=" + sign("POST", path, ts, nonce, body, _sec),
    }
    if idem:
        h["Idempotency-Key"] = idem
    return h


async def _post_once(path: str, body: bytes, idem: str = "", timeout: float = 30,
                     svc: str = "gpt") -> dict:
    """Один HTTP-запрос. Возвращает разобранный итог, исключений наружу не бросает.

    reached: False — соединиться не удалось, запрос до сайта ТОЧНО не дошёл;
             True  — мог дойти (ответ получен, либо обрыв/тайм-аут после отправки).
    """
    out = {"http": 0, "ok": False, "data": None, "code": "", "msg": "",
           "reached": False, "retry_after": 0}
    if not ready(svc):
        out.update(code="NOT_CONFIGURED",
                   msg="не заданы AICHONGZ_API_KEY / AICHONGZ_API_SECRET")
        return out
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.post(creds(svc)[0] + path, data=body,
                              headers=_headers(path, body, idem, svc)) as r:
                out["reached"] = True
                out["http"] = r.status
                try:
                    out["retry_after"] = int(float(r.headers.get("Retry-After", "0") or 0))
                except Exception:
                    out["retry_after"] = 0
                txt = await r.text()
        try:
            d = json.loads(txt) if txt else {}
        except Exception:
            # Прокси/HTML-ответ: структура другая, разбираем только HTTP-статус.
            d = {}
            out["msg"] = (txt or "")[:200]
        if isinstance(d, dict):
            out["ok"] = bool(d.get("ok"))
            out["data"] = d.get("data") if isinstance(d.get("data"), dict) else None
            err = d.get("error") if isinstance(d.get("error"), dict) else {}
            out["code"] = str(err.get("code") or "")
            out["msg"] = str(err.get("message") or out["msg"] or "")
        return out
    except aiohttp.ClientConnectorError as e:
        out.update(code="NETWORK", msg=f"не соединился: {e}"[:200], reached=False)
        return out
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        out.update(code="UNCLEAR", msg=f"{type(e).__name__}: {e}"[:200], reached=True)
        return out
    except Exception as e:  # noqa
        out.update(code="UNCLEAR", msg=f"{type(e).__name__}: {e}"[:200], reached=True)
        return out


def _retryable(res: dict) -> bool:
    if res["code"] in ("NETWORK", "UNCLEAR", "NONCE_REPLAY", "RATE_LIMITED"):
        return True
    return res["http"] in (429, 500, 502, 503, 504)


async def post(path: str, payload_bytes: bytes, idem: str = "", tries: int = 3,
               timeout: float = 30, svc: str = "gpt") -> dict:
    """Запрос с безопасными повторами: тот же ключ и те же байты, новые nonce/время."""
    res = {}
    reached_any = False
    for i in range(max(1, tries)):
        res = await _post_once(path, payload_bytes, idem, timeout, svc)
        reached_any = reached_any or res["reached"]
        if not _retryable(res) or i == tries - 1:
            break
        wait = 2 + 2 * i
        if res["http"] == 429 or res["code"] == "RATE_LIMITED":
            wait = max(res.get("retry_after") or 5, 1)
        await asyncio.sleep(wait)
    res["reached_any"] = reached_any
    return res


# ── Проверка кодов ──────────────────────────────────────────────────────────

async def card_query(code: str, svc: str = "gpt") -> dict:
    """{'ok': True, 'card': {...}, 'order': {...}|None} или
    {'ok': False, 'kind': ..., 'code': ..., 'msg': ...}."""
    res = await post(PREFIX + "/cards/query", body_bytes({"cardCode": str(code).strip()}),
                     tries=3, timeout=20, svc=svc)
    if res["ok"] and res["data"]:
        return {"ok": True, "card": res["data"].get("card") or {},
                "order": res["data"].get("order")}
    return {"ok": False, "kind": error_kind(res), "code": res["code"],
            "msg": res["msg"], "http": res["http"]}


async def batch_query(codes: list, svc: str = "gpt") -> dict:
    """{КОД_КАК_ПЕРЕДАН: {'ok', 'card', 'order'} | {'ok': False, 'code', 'msg'}}.

    Пустой словарь — проверить не удалось (сеть, ключ, лимит). Вызывающий
    обязан тогда ничего не менять.
    """
    items = [str(c).strip() for c in (codes or []) if str(c).strip()]
    out = {}
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        res = await post(PREFIX + "/cards/batch-query", body_bytes({"cardCodes": chunk}),
                         tries=3, timeout=30, svc=svc)
        if not (res["ok"] and res["data"]):
            logger.warning(f"9527 batch-query: HTTP {res['http']} {res['code']} {res['msg'][:120]}")
            return {}
        for it in res["data"].get("items") or []:
            try:
                idx = int(it.get("index"))
                src = chunk[idx]
            except Exception:
                continue
            if it.get("ok"):
                out[src] = {"ok": True, "card": it.get("card") or {}, "order": it.get("order")}
            else:
                er = it.get("error") or {}
                out[src] = {"ok": False, "code": str(er.get("code") or ""),
                            "msg": str(er.get("message") or "")}
    return out


def error_kind(res: dict) -> str:
    """Тип ошибки для логики бота (не для показа клиенту)."""
    c, h = res.get("code") or "", res.get("http") or 0
    if c == "NOT_CONFIGURED":
        return "config"
    if c == "NETWORK":
        return "network"
    if c == "UNCLEAR":
        return "unclear"
    if c in ("CARD_NOT_OWNED",):
        return "not_owned"
    if c in ("CARD_INVALID",):
        return "card_invalid"
    if c in ("TARGET_INVALID",):
        return "bad_target"
    if c in ("IDEMPOTENCY_CONFLICT", "REQUEST_IN_PROGRESS"):
        return "conflict"
    if c in ("UPSTREAM_UNAVAILABLE",):
        return "upstream"
    if c in ("BUSINESS_RULE_REJECTED",):
        return "rejected"
    if c in ("AUTH_REQUIRED", "AUTH_INVALID", "SIGNATURE_INVALID", "TIMESTAMP_EXPIRED",
             "SCOPE_DENIED", "IP_NOT_ALLOWED", "IDEMPOTENCY_REQUIRED", "INVALID_JSON",
             "NOT_FOUND", "BATCH_INVALID"):
        return "config"
    if h in (401, 403, 404, 413):
        return "config"
    if h == 429 or c == "RATE_LIMITED":
        return "rate"
    if h >= 500:
        return "unclear"
    if h in (400, 409, 422):
        return "rejected"
    return "unclear" if not h else "rejected"


_PLAN_WORDS = ("already subscribed", "active subscription", "already has", "already plus",
               "已订阅", "已有订阅", "已经是", "已开通", "已是会员", "存在订阅", "当前已有",
               "уже есть подписка", "уже подписан")
_STOCK_WORDS = ("stock", "库存", "缺货", "无货", "sold out")


def reject_flavor(msg: str) -> str:
    """'has_plan' | 'stock' | '' — по тексту бизнес-отказа (текст может быть китайским)."""
    m = (msg or "").lower()
    if any(w in m for w in _PLAN_WORDS):
        return "has_plan"
    if any(w in m for w in _STOCK_WORDS):
        return "stock"
    return ""


# ── Активация ───────────────────────────────────────────────────────────────

async def redeem(code: str, target_type: str, target_value: str, idem: str,
                 client_order_no: str = "", svc: str = "gpt") -> dict:
    """POST /redemptions. Возвращает:
      {'ok': True, 'order': {...}, 'duplicate': bool}
      {'ok': False, 'kind': ..., 'code': ..., 'msg': ..., 'reached': bool}
    kind 'unclear' — запрос мог дойти, ответа нет; повторять только тем же ключом.
    """
    payload = {"cardCode": str(code).strip(),
               "target": {"type": target_type, "value": target_value}}
    if client_order_no:
        payload["clientOrderNo"] = client_order_no[:160]
    raw = body_bytes(payload)
    path = PREFIX + "/redemptions"
    last = {}
    reached = False      # хоть одна попытка могла дойти до сайта
    unclear_rounds = 0
    net_tries = 0
    for _ in range(8):
        last = await _post_once(path, raw, idem=idem, timeout=60, svc=svc)
        reached = reached or last["reached"]
        o = (last["data"] or {}).get("order") or {}
        if o.get("orderNo"):
            # Заказ есть — даже если рядом ошибка, верим заказу.
            return {"ok": True, "order": o, "duplicate": bool((last["data"] or {}).get("duplicate"))}
        if last["code"] == "NONCE_REPLAY":
            continue                        # отклонён до обработки — новый nonce
        if last["http"] == 429 or last["code"] == "RATE_LIMITED":
            # Отказ по лимиту квоту не тратит и до обработки не доходит.
            await asyncio.sleep(max(last.get("retry_after") or 5, 1))
            continue
        kind = error_kind(last)
        if kind == "network" and not reached:
            net_tries += 1
            if net_tries < 2:
                await asyncio.sleep(3)
                continue                    # ещё одна попытка соединиться
            return {"ok": False, "kind": "network", "code": last["code"],
                    "msg": last["msg"], "reached": False}
        if last["code"] == "IDEMPOTENCY_CONFLICT":
            # Тот же ключ уже занят запросом с ДРУГИМ телом. Чей там заказ и на
            # какой аккаунт — не знаем; «найти» его и выдать за свой нельзя.
            return {"ok": False, "kind": "unclear", "code": last["code"], "msg": last["msg"],
                    "reached": True, "http": last["http"]}
        if kind not in ("unclear", "conflict", "network"):
            return {"ok": False, "kind": kind, "code": last["code"], "msg": last["msg"],
                    "reached": True, "http": last["http"]}
        # Неясно, создан ли заказ. По документации: СНАЧАЛА спросить код, и
        # только потом повторять тем же ключом и теми же байтами.
        unclear_rounds += 1
        q = await card_query(code, svc)
        if q.get("ok"):
            qo = q.get("order") or {}
            if qo.get("orderNo") and (not client_order_no
                                      or str(qo.get("clientOrderNo") or "") == client_order_no):
                return {"ok": True, "order": qo, "duplicate": True, "recovered": True}
        if unclear_rounds >= 3:
            break
        await asyncio.sleep(3 * unclear_rounds)
    if unclear_rounds == 0 and (last.get("http") == 429 or last.get("code") == "RATE_LIMITED"):
        # Все попытки упёрлись в лимит: такие запросы сайт не обрабатывает.
        return {"ok": False, "kind": "rate", "code": last.get("code", "") or "RATE_LIMITED",
                "msg": last.get("msg", "") or "лимит запросов", "reached": False,
                "http": last.get("http", 0)}
    return {"ok": False, "kind": "unclear", "code": last.get("code", ""),
            "msg": last.get("msg", ""), "reached": True, "http": last.get("http", 0)}


# ── Вердикт по коду ─────────────────────────────────────────────────────────

def verdict(card: dict, order, expect_order_no: str = "") -> dict:
    """Нормализует состояние кода/заказа в одно из:
      success  — заказ success (и это НАШ заказ, если номер известен);
      pending  — сайт ещё работает (в т.ч. failed с активным автоповтором);
      review   — manual_review: автоматика остановлена, решает продавец;
      failed   — заказ failed, автоповторов нет;
      free     — заказа нет, код доступен для активации;
      foreign  — у кода есть заказ, но не наш (номер не совпал);
      used     — сайт считает код использованным, а нашего успешного заказа нет;
      dead     — код недоступен (не выдан/отозван/аннулирован);
      unknown  — всё остальное.
    """
    card = card or {}
    o = order if isinstance(order, dict) else {}
    cst = str(card.get("status") or "").lower()
    ost = str(o.get("status") or "").lower()
    ono = str(o.get("orderNo") or card.get("orderNo") or "")
    ars = str(o.get("autoRetryState") or "").lower()
    info = {"card_status": cst, "order_status": ost, "order_no": ono,
            "can_redeem": bool(card.get("canRedeem")), "can_retry": bool(o.get("canRetry")),
            "auto_retry": ars, "reason": str(o.get("reason") or o.get("statusLabel")
                                              or o.get("statusDescription") or ""),
            "target_email": str(o.get("targetEmail") or ""),
            "target_org": str(o.get("targetOrganizationId") or ""),
            "target_mask": str(o.get("targetMask") or ""),
            "project": str(card.get("projectName") or card.get("projectCode") or ""),
            "released": bool(card.get("previousOrderReleased"))}
    if expect_order_no and ono and ono != expect_order_no:
        # Последний заказ по коду — чужой (например, активировали руками на
        # сайте) или наш был освобождён и код ушёл в новый заказ.
        st = "free" if (cst == "available" and card.get("canRedeem") and not ost) else "foreign"
        return {"state": st, **info}
    if ost == "success":
        return {"state": "success", **info}
    if ost == "manual_review" or cst == "manual_review":
        return {"state": "review", **info}
    if ost == "failed":
        if ars in AUTO_RETRY_ACTIVE or ars not in AUTO_RETRY_DONE:
            return {"state": "pending", **info}
        if o.get("isFinal") is False:
            return {"state": "pending", **info}      # сайт сам говорит: процесс не окончен
        return {"state": "failed", **info}
    if ost and ost in ORDER_WAIT:
        return {"state": "pending", **info}
    if not o:
        if cst == "available" and card.get("canRedeem"):
            return {"state": "free", **info}
        if cst == "processing":
            return {"state": "pending", **info}
        if cst == "used":
            return {"state": "used", **info}
        if cst == "unavailable":
            return {"state": "dead", **info}
    if cst == "processing":
        return {"state": "pending", **info}
    return {"state": "unknown", **info}


def code_free(v: dict) -> bool:
    """Сайт подтверждает: код можно снова активировать и живого заказа нет.

    «free» — заказа не было; «failed» + canRedeem + код available — прошлый
    заказ провалился и сайт САМ освободил код (previousOrderReleased).
    """
    if not v:
        return False
    if v.get("state") == "free":
        return True
    return (v.get("state") == "failed" and bool(v.get("can_redeem"))
            and v.get("card_status") == "available")


# Какие тарифы этот сайт активирует автоматически (решение Александра
# 14.10.2026): ChatGPT Plus и Claude Pro. Max/Go/Pro — вручную.
ALLOWED_PLANS = {"chatgpt": ("plus",), "claude": ("pro",)}


def plan_allowed(service: str, plan: str) -> bool:
    return (plan or "").strip().lower() in ALLOWED_PLANS.get(service, ())


def product_conflict(service: str, plan: str, project: str, level: int = 1) -> str:
    """Причина, если товар кода по данным сайта ЯВНО не совпадает с пулом.
    Незнакомое/китайское название — не конфликт (''), решает сайт.

    level 0 — только путаница СЕРВИСОВ (код Claude в пуле ChatGPT и наоборот):
              такой код точно не сработает.
    level 1 — плюс слово «Max»: Max-товар не Plus и не Pro (Max — вручную).
    level 2 — плюс догадки по словам pro/go/team/business… Только для
              ПРЕДУПРЕЖДЕНИЯ при загрузке: «Pro» в названии Plus-товара может
              значить другое, выбрасывать рабочий код по нему нельзя."""
    import re as _re
    p = (project or "").lower()
    if not p:
        return ""
    w = lambda x: bool(_re.search(r"(?<![a-z])" + x + r"(?![a-z])", p))
    if service == "chatgpt":
        if "claude" in p and "gpt" not in p:
            return f"это код Claude ({project}), а не ChatGPT"
        if level >= 1 and w("max"):
            return f"товар «{project}» — Max, а не ChatGPT Plus"
        if level >= 2 and (plan or "plus") == "plus" and (
                w("pro") or w("go") or w("team") or w("business") or w("enterprise")):
            return f"товар «{project}» — похоже, не ChatGPT Plus"
    if service == "claude":
        if ("gpt" in p or "openai" in p) and "claude" not in p:
            return f"это код ChatGPT ({project}), а не Claude"
        if level >= 1 and (plan or "pro") == "pro" and w("max"):
            return f"товар «{project}» — Claude Max, а не Pro"
    return ""


def gpt_target(session_raw: str):
    """(type, value) для ChatGPT или None, если присланное не Session JSON.

    Session передаётся СТРОКОЙ — так требует документация. Сырой accessToken
    сайт не принимает, а account.id без проверки товара слать рискованно.
    """
    s = (session_raw or "").strip()
    if not s.startswith("{"):
        return None
    try:
        d = json.loads(s)
    except Exception:
        return None
    if not isinstance(d, dict) or not d.get("accessToken"):
        return None
    return ("chatgpt_session", s)
