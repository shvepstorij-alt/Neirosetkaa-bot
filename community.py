"""Сообщество Neirosetka: Mini App /webapp/community + API /api/community/*.

Пока закрыто: пускает только ADMIN_ID. Открыть всем — в БД:
  INSERT INTO settings (key,value) VALUES ('community_open','1')
  ON CONFLICT (key) DO UPDATE SET value='1';
"""
import json
import logging
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

HTML = Path(__file__).with_name("community_webapp.html")
POST_TYPES = ("Вопрос", "Рецепт", "Кейс", "Разбор", "Коллаб")

SCHEMA = """
CREATE TABLE IF NOT EXISTS community_profiles (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    name TEXT NOT NULL,
    niche TEXT DEFAULT '',
    looking TEXT DEFAULT '',
    offering TEXT DEFAULT '',
    open_collab BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS community_posts (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    type TEXT NOT NULL DEFAULT 'Вопрос',
    tag TEXT DEFAULT '',
    text TEXT NOT NULL,
    hidden BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS community_comments (
    id SERIAL PRIMARY KEY,
    post_id INT NOT NULL REFERENCES community_posts(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    text TEXT NOT NULL,
    is_ai BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS community_likes (
    post_id INT NOT NULL REFERENCES community_posts(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    PRIMARY KEY (post_id, user_id)
);
CREATE TABLE IF NOT EXISTS community_reports (
    id SERIAL PRIMARY KEY,
    post_id INT NOT NULL REFERENCES community_posts(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    reason TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (post_id, user_id)
);
CREATE TABLE IF NOT EXISTS community_bans (
    user_id BIGINT PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE community_posts ADD COLUMN IF NOT EXISTS best_comment_id INT;
CREATE INDEX IF NOT EXISTS community_posts_created ON community_posts(created_at DESC);
CREATE INDEX IF NOT EXISTS community_comments_post ON community_comments(post_id);
"""


def _clip(v, n):
    return str(v or "").strip()[:n]


def _tg_user(init_data):
    try:
        return json.loads(dict(parse_qsl(init_data or "", keep_blank_values=True)).get("user") or "{}")
    except Exception:
        return {}


async def _allowed(conn, uid):
    from config import ADMIN_ID
    if uid == ADMIN_ID:
        return True
    return (await conn.fetchval("SELECT value FROM settings WHERE key='community_open'")) == "1"


def _api(fn):
    """Общая обёртка: JSON, проверка initData и доступа."""
    async def handler(request):
        from config import _verify_tg_init_data
        from db import get_pool
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        init_data = str(body.get("initData") or "")
        uid = _verify_tg_init_data(init_data)
        if not uid:
            return web.json_response({"error": "auth"}, status=403)
        try:
            async with (await get_pool()).acquire() as conn:
                if not await _allowed(conn, int(uid)):
                    return web.json_response({"error": "closed"}, status=403)
                if await conn.fetchval("SELECT 1 FROM community_bans WHERE user_id=$1", int(uid)):
                    return web.json_response({"error": "banned"}, status=403)
                res = await fn(conn, int(uid), body, _tg_user(init_data))
            return web.json_response(res)
        except web.HTTPException:
            raise
        except Exception:
            logging.exception("community api error")
            return web.json_response({"error": "server"}, status=500)
    return handler


def _row(r):
    d = dict(r)
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


# ─────────────── Опыт и уровни ───────────────
XP_SQL = """
SELECT pr.user_id, pr.name, pr.niche,
  (SELECT count(*) FROM community_posts p WHERE p.user_id=pr.user_id AND NOT p.hidden) AS posts,
  (SELECT count(*) FROM community_posts p WHERE p.user_id=pr.user_id AND NOT p.hidden AND p.type='Коллаб') AS collabs,
  (SELECT count(*) FROM community_comments c JOIN community_posts p ON p.id=c.post_id
     WHERE c.user_id=pr.user_id AND NOT c.is_ai AND p.user_id<>pr.user_id) AS answers,
  (SELECT count(*) FROM community_likes l JOIN community_posts p ON p.id=l.post_id
     WHERE p.user_id=pr.user_id AND l.user_id<>pr.user_id) AS likes_got,
  (SELECT count(*) FROM community_posts p JOIN community_comments c ON c.id=p.best_comment_id
     WHERE c.user_id=pr.user_id AND p.user_id<>pr.user_id) AS best
FROM community_profiles pr
"""
XP_RULES = {"profile": 20, "post": 10, "answer": 5, "like": 2, "best": 25}
LEVELS = [(0, "Новичок"), (50, "Исследователь"), (150, "Промптер"), (300, "Практик"), (500, "Знаток"),
          (800, "Мастер связок"), (1200, "Наставник"), (1700, "Эксперт"), (2300, "Нейро-гуру"), (3000, "Легенда")]
ACHIEVEMENTS = [("Первый пост", "posts", 1), ("Помощник", "answers", 5), ("Лучший ответ", "best", 1),
                ("Коннектор", "collabs", 1), ("Популярный", "likes_got", 20), ("Наставник", "best", 5),
                ("Активист", "answers", 30), ("Автор", "posts", 10)]


def _xp(r):
    d = dict(r)
    xp = XP_RULES["profile"] + d["posts"] * XP_RULES["post"] + d["answers"] * XP_RULES["answer"] + \
        d["likes_got"] * XP_RULES["like"] + d["best"] * XP_RULES["best"]
    lvl = max(i for i, (t, _) in enumerate(LEVELS) if xp >= t)
    nxt = LEVELS[lvl + 1][0] if lvl + 1 < len(LEVELS) else None
    d.update(xp=xp, level=lvl + 1, level_name=LEVELS[lvl][1], level_from=LEVELS[lvl][0], level_to=nxt,
             achievements=[{"name": n, "done": d[k] >= v, "need": "%s: %s" % (k, v)} for n, k, v in ACHIEVEMENTS])
    return d


async def me(conn, uid, body, user):
    p = await conn.fetchrow("SELECT * FROM community_profiles WHERE user_id=$1", uid)
    uname = _clip(user.get("username"), 64)
    if p and uname and p["username"] != uname:
        # ник в Telegram сменился — обновляем, чтобы кнопка «Написать» вела куда нужно
        await conn.execute("UPDATE community_profiles SET username=$2 WHERE user_id=$1", uid, uname)
        p = await conn.fetchrow("SELECT * FROM community_profiles WHERE user_id=$1", uid)
    stats = await conn.fetchrow(
        "SELECT (SELECT count(*) FROM community_posts WHERE user_id=$1 AND NOT hidden) AS posts,"
        " (SELECT count(*) FROM community_comments WHERE user_id=$1 AND NOT is_ai) AS comments", uid)
    from config import ADMIN_ID
    xr = await conn.fetchrow(XP_SQL + " WHERE pr.user_id=$1", uid) if p else None
    return {"profile": _row(p) if p else None, "stats": _row(stats), "is_admin": uid == ADMIN_ID,
            "xp": _xp(xr) if xr else None, "rules": XP_RULES,
            "tg": {"first_name": user.get("first_name", ""), "username": user.get("username", "")}}


async def save_profile(conn, uid, body, user):
    name = _clip(body.get("name"), 60)
    if not name:
        return {"error": "name_required"}
    await conn.execute(
        """INSERT INTO community_profiles (user_id,username,name,niche,looking,offering,open_collab)
           VALUES ($1,$2,$3,$4,$5,$6,$7)
           ON CONFLICT (user_id) DO UPDATE SET username=$2,name=$3,niche=$4,looking=$5,
           offering=$6,open_collab=$7,updated_at=now()""",
        uid, _clip(user.get("username"), 64), name, _clip(body.get("niche"), 120),
        _clip(body.get("looking"), 500), _clip(body.get("offering"), 500), bool(body.get("open_collab", True)))
    return {"ok": True}


async def _need_profile(conn, uid):
    if not await conn.fetchval("SELECT 1 FROM community_profiles WHERE user_id=$1", uid):
        raise web.HTTPForbidden(text='{"error":"no_profile"}', content_type="application/json")


async def feed(conn, uid, body, user):
    sort = body.get("sort") or "new"
    ptype = body.get("type") if body.get("type") in POST_TYPES else None
    order = {"hot": "(likes*2+comments) DESC, p.created_at DESC",
             "unanswered": "p.created_at DESC"}.get(sort, "p.created_at DESC")
    where = "NOT p.hidden" + (" AND p.type=$2" if ptype else "")
    having = " WHERE comments=0" if sort == "unanswered" else ""
    q = f"""SELECT * FROM (
        SELECT p.id,p.user_id,p.type,p.tag,p.text,p.created_at,
               coalesce(pr.name,'Участник') AS author,
               (SELECT count(*) FROM community_likes l WHERE l.post_id=p.id) AS likes,
               (SELECT count(*) FROM community_comments c WHERE c.post_id=p.id) AS comments,
               EXISTS(SELECT 1 FROM community_likes l WHERE l.post_id=p.id AND l.user_id=$1) AS liked
        FROM community_posts p LEFT JOIN community_profiles pr ON pr.user_id=p.user_id
        WHERE {where}) p{having} ORDER BY {order} LIMIT 50"""
    rows = await conn.fetch(q, *([uid, ptype] if ptype else [uid]))
    return {"posts": [_row(r) for r in rows]}


async def create_post(conn, uid, body, user):
    await _need_profile(conn, uid)
    text = _clip(body.get("text"), 3000)
    if len(text) < 5:
        return {"error": "too_short"}
    recent = await conn.fetchval(
        "SELECT count(*) FROM community_posts WHERE user_id=$1 AND created_at > now()-interval '10 minutes'", uid)
    if recent >= 5:
        return {"error": "slow_down"}
    ptype = body.get("type") if body.get("type") in POST_TYPES else "Вопрос"
    pid = await conn.fetchval(
        "INSERT INTO community_posts (user_id,type,tag,text) VALUES ($1,$2,$3,$4) RETURNING id",
        uid, ptype, _clip(body.get("tag"), 40), text)
    asyncio.create_task(_after_post(pid, ptype))
    return {"ok": True, "id": pid}


async def post_detail(conn, uid, body, user):
    pid = int(body.get("id") or 0)
    p = await conn.fetchrow(
        """SELECT p.id,p.user_id,p.type,p.tag,p.text,p.created_at,p.hidden,p.best_comment_id,coalesce(pr.name,'Участник') AS author,
           (SELECT count(*) FROM community_likes l WHERE l.post_id=p.id) AS likes,
           EXISTS(SELECT 1 FROM community_likes l WHERE l.post_id=p.id AND l.user_id=$2) AS liked
           FROM community_posts p LEFT JOIN community_profiles pr ON pr.user_id=p.user_id
           WHERE p.id=$1 AND (NOT p.hidden OR $2=$3)""", pid, uid, __import__("config").ADMIN_ID)
    if not p:
        return {"error": "not_found"}
    cs = await conn.fetch(
        """SELECT c.id,c.user_id,c.text,c.is_ai,c.created_at,
           CASE WHEN c.is_ai THEN 'Neirosetka' ELSE coalesce(pr.name,'Участник') END AS author
           FROM community_comments c LEFT JOIN community_profiles pr ON pr.user_id=c.user_id
           WHERE c.post_id=$1 ORDER BY c.created_at""", pid)
    return {"post": _row(p), "comments": [_row(c) for c in cs]}


async def create_comment(conn, uid, body, user):
    await _need_profile(conn, uid)
    pid = int(body.get("post_id") or 0)
    text = _clip(body.get("text"), 2000)
    if not text:
        return {"error": "empty"}
    if not await conn.fetchval("SELECT 1 FROM community_posts WHERE id=$1 AND NOT hidden", pid):
        return {"error": "not_found"}
    await conn.execute("INSERT INTO community_comments (post_id,user_id,text) VALUES ($1,$2,$3)", pid, uid, text)
    author = await conn.fetchval("SELECT user_id FROM community_posts WHERE id=$1", pid)
    if author and author != uid:
        name = await conn.fetchval("SELECT name FROM community_profiles WHERE user_id=$1", uid) or "Участник"
        asyncio.create_task(_notify(author, "💬 %s ответил(а) на твой пост в сообществе:\n\n«%s»" % (name, text[:200]), pid))
    return {"ok": True}


async def like(conn, uid, body, user):
    await _need_profile(conn, uid)
    pid = int(body.get("post_id") or 0)
    deleted = await conn.execute("DELETE FROM community_likes WHERE post_id=$1 AND user_id=$2", pid, uid)
    if deleted.endswith(" 0"):
        await conn.execute("INSERT INTO community_likes (post_id,user_id) VALUES ($1,$2) ON CONFLICT DO NOTHING", pid, uid)
        return {"liked": True}
    return {"liked": False}


async def people(conn, uid, body, user):
    q = _clip(body.get("q"), 60)
    rows = await conn.fetch(
        """SELECT user_id,name,niche,looking,offering,open_collab,username,
           EXISTS(SELECT 1 FROM community_bans b WHERE b.user_id=community_profiles.user_id) AS banned
           FROM community_profiles
           WHERE ($1='' OR name ILIKE '%'||$1||'%' OR niche ILIKE '%'||$1||'%'
                  OR looking ILIKE '%'||$1||'%' OR offering ILIKE '%'||$1||'%')
           ORDER BY updated_at DESC LIMIT 100""", q)
    return {"people": [_row(r) for r in rows]}


# ─────────────── Neirosetka (ИИ) ───────────────
import asyncio
import time

AI_UID = 0
MODELS = ["claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929"]
RECIPES = "Видео-аватар из одного текста; Один персонаж на серии фото; Оживить фото без артефактов; Карточки товара для WB и Ozon; Трек с обложкой и клипом; Как оплатить подписку и не потерять аккаунт; Аватарка для резюме и соцсетей из селфи; Предметная съёмка без фотографа; Реставрация и раскраска старых фото; Нарезка Reels из длинного видео; Рекламный ролик 15 сек для бизнеса; Контент-план на месяц за вечер; Карусель, которую сохраняют; Презентация за 10 минут; Конспект лекции, вебинара или созвона; Формулы Excel и Google Таблиц словами; Резюме и отклик под вакансию; Сайт-визитка или лендинг за вечер; Первое приложение через вайб-кодинг; Заявки с сайта → ИИ → Telegram; Анализ рынка и конкурентов с источниками; Перевод видео на другой язык своим голосом; Эстетичные визуалы в Midjourney; Первые клиенты на ИИ-услуги; Нейрофотосессии на продажу; Бесплатно или Pro: что выбрать"
PERSONA = (
    "Ты — Neirosetka, ИИ-персонаж сообщества в Telegram-боте @neirosetkaa_bot (автор — Александр, канал «Александр ИИ»). "
    "Стиль: дружелюбный, на «ты», коротко и по делу, без воды и канцелярита, иногда «йо»/«хэй». Пиши по-русски, обычным текстом без markdown-разметки и без эмодзи-спама. "
    "Ты эксперт по нейросетям: тексты, фото, видео, музыка, озвучка, код, автоматизация, заработок на ИИ. "
    "Давай конкретику: какой сервис, какой шаг, пример промпта. Не выдумывай цены и точные лимиты — если не уверен, так и скажи. "
    "В боте можно генерировать за кредиты: Изображение (Nano Banana Pro, GPT Image 2.5, Ideogram 4.5, Flux 3, Imagen 4, Grok Imagine), "
    "Редактировать фото (Nano Banana, GPT Image 2.5, Flux 3), Анимировать фото (Kling 2.5 Turbo, Gemini Omni, Wan 3.0), Видео (Seedance 2.5, Kling 3.0 Pro, Gemini Omni). "
    "В Каталоге бота — подписки: ChatGPT, Claude, Gemini, Midjourney, Kling, Runway, HeyGen, ElevenLabs, Suno, Gamma, Canva, CapCut, Higgsfield, Krea, Perplexity, Cursor, Lovable, Make и др. "
    "Рекомендуй их, когда это реально подходит задаче, без навязывания. "
    "В сообществе есть вкладки: Лента, Рецепты (пошаговые инструкции: " + RECIPES + "), Коллабы, Люди, Профиль. Если подходит рецепт — назови его."
)
_ask_hits = {}


def _rate_ok(uid, limit=20, window=3600):
    now = time.time()
    hits = [t for t in _ask_hits.get(uid, []) if now - t < window]
    if len(hits) >= limit:
        _ask_hits[uid] = hits
        return False
    hits.append(now)
    _ask_hits[uid] = hits
    return True


async def _claude(system, messages, max_tokens=700):
    from config import claude_client
    loop = asyncio.get_event_loop()
    last = None
    for m in MODELS:
        try:
            resp = await loop.run_in_executor(None, lambda: claude_client.messages.create(
                model=m, max_tokens=max_tokens, system=system, messages=messages))
            return "".join(getattr(b, "text", "") for b in resp.content).strip()
        except Exception as e:  # пробуем следующую модель
            last = e
    raise last or RuntimeError("claude failed")


async def _ai_first_answer(pid):
    """Фоновая задача: первый ответ Neirosetka под вопросом."""
    from db import get_pool
    try:
        async with (await get_pool()).acquire() as conn:
            p = await conn.fetchrow("SELECT type,tag,text FROM community_posts WHERE id=$1", pid)
        if not p:
            return
        task = ("Участник опубликовал в ленте сообщества пост типа «%s»%s. Дай первый ответ: 3–8 коротких предложений или короткий список шагов. "
                "Если это «Разбор» — что хорошо, что улучшить и как. В конце можно предложить участникам дополнить.") % (
                p["type"], (" с тегом " + p["tag"]) if p["tag"] else "")
        text = await _claude(PERSONA + "\n\n" + task, [{"role": "user", "content": p["text"]}])
        if not text:
            return
        async with (await get_pool()).acquire() as conn:
            await conn.execute("INSERT INTO community_comments (post_id,user_id,text,is_ai) VALUES ($1,$2,$3,TRUE)",
                               pid, AI_UID, text[:3000])
    except Exception:
        logging.exception("community: ИИ-ответ не получился (post %s)", pid)


async def _notify(user_id, text, post_id=None):
    """Уведомление в бот с кнопкой открыть пост."""
    try:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
        from config import bot, webapp_url
        kb = None
        if post_id:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="Открыть", web_app=WebAppInfo(url=webapp_url("/webapp/community", post=post_id)))]])
        await bot.send_message(user_id, text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        logging.info("community: не удалось отправить уведомление %s", user_id)


async def ask(conn, uid, body, user):
    """Чат с Neirosetka во вкладке «Нейро» + подбор людей."""
    q = _clip(body.get("q"), 1500)
    if not q:
        return {"error": "empty"}
    if not _rate_ok(uid):
        return {"error": "slow_down"}
    me = await conn.fetchrow("SELECT name,niche,looking,offering FROM community_profiles WHERE user_id=$1", uid)
    ppl = await conn.fetch(
        "SELECT name,niche,looking,offering FROM community_profiles WHERE open_collab AND user_id<>$1 "
        "ORDER BY updated_at DESC LIMIT 40", uid)
    ctx = PERSONA
    if me:
        ctx += "\n\nСобеседник: %s. Ниша: %s. Ищет: %s. Может предложить: %s." % (
            me["name"], me["niche"] or "-", me["looking"] or "-", me["offering"] or "-")
    if ppl:
        ctx += "\n\nУчастники, открытые к знакомствам (рекомендуй по имени, только если реально подходят; не выдумывай других людей):\n" + \
            "\n".join("- %s | ниша: %s | ищет: %s | полезен: %s" % (p["name"], p["niche"] or "-", p["looking"] or "-", p["offering"] or "-") for p in ppl)
    hist = body.get("history") if isinstance(body.get("history"), list) else []
    msgs = []
    for h in hist[-8:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("text"):
            msgs.append({"role": h["role"], "content": _clip(h["text"], 1500)})
    if msgs and msgs[0]["role"] != "user":
        msgs = msgs[1:]
    msgs.append({"role": "user", "content": q})
    try:
        return {"answer": await _claude(ctx, msgs)}
    except Exception:
        logging.exception("community ask failed")
        return {"error": "ai"}


async def collabs(conn, uid, body, user):
    body = dict(body, type="Коллаб")
    return await feed(conn, uid, body, user)


async def best(conn, uid, body, user):
    """Автор поста (или админ) отмечает лучший ответ. Повторное нажатие снимает отметку."""
    from config import ADMIN_ID
    pid, cid = int(body.get("post_id") or 0), int(body.get("comment_id") or 0)
    p = await conn.fetchrow("SELECT user_id,best_comment_id FROM community_posts WHERE id=$1", pid)
    if not p or (p["user_id"] != uid and uid != ADMIN_ID):
        return {"error": "forbidden"}
    c = await conn.fetchrow("SELECT user_id,is_ai FROM community_comments WHERE id=$1 AND post_id=$2", cid, pid)
    if not c:
        return {"error": "not_found"}
    if p["best_comment_id"] == cid:
        await conn.execute("UPDATE community_posts SET best_comment_id=NULL WHERE id=$1", pid)
        return {"ok": True, "best": None}
    await conn.execute("UPDATE community_posts SET best_comment_id=$2 WHERE id=$1", pid, cid)
    if not c["is_ai"] and c["user_id"] != p["user_id"]:
        asyncio.create_task(_notify(c["user_id"], "★ Твой ответ отметили лучшим! +%s XP" % XP_RULES["best"], pid))
    return {"ok": True, "best": cid}


async def top(conn, uid, body, user):
    rows = await conn.fetch(XP_SQL + """ WHERE NOT EXISTS (SELECT 1 FROM community_bans b WHERE b.user_id=pr.user_id)""")
    ppl = sorted((_xp(r) for r in rows), key=lambda d: -d["xp"])[:10]
    return {"top": [{k: d[k] for k in ("user_id", "name", "niche", "xp", "level", "level_name")} for d in ppl]}


# ─────────────── Модерация ───────────────
REPORT_REASONS = ("Спам", "Оскорбления", "Не по теме", "Мошенничество", "Другое")


async def _after_post(pid, ptype):
    """Проверка нового поста ИИ-модератором, затем первый ответ Neirosetka."""
    from db import get_pool
    from config import ADMIN_ID
    verdict = "OK"
    try:
        async with (await get_pool()).acquire() as conn:
            text = await conn.fetchval("SELECT text FROM community_posts WHERE id=$1", pid)
        verdict = (await _claude(
            "Ты модератор сообщества про нейросети. Ответь ОДНИМ словом: OK — обычный пост (вопрос, кейс, "
            "коллаб, самопиар по теме тоже OK); SPAM — массовая реклама не по теме, казино, крипто-схемы, ставки, "
            "рассылки, подозрительные ссылки; ABUSE — оскорбления, травля, 18+, призывы к насилию, мошенничество.",
            [{"role": "user", "content": text or ""}], max_tokens=5)).strip().upper()
    except Exception:
        logging.exception("community: модерация не сработала (post %s)", pid)
    if verdict.startswith(("SPAM", "ABUSE")):
        async with (await get_pool()).acquire() as conn:
            await conn.execute("UPDATE community_posts SET hidden=TRUE WHERE id=$1", pid)
        await _notify(ADMIN_ID, "🛡 ИИ-модератор скрыл пост #%s (%s). Открой, чтобы проверить — "
                                "вернуть можно кнопкой «Вернуть пост»." % (pid, verdict[:5]), pid)
        return
    if ptype in ("Вопрос", "Разбор"):
        await _ai_first_answer(pid)


async def report(conn, uid, body, user):
    await _need_profile(conn, uid)
    from config import ADMIN_ID
    pid = int(body.get("post_id") or 0)
    reason = body.get("reason") if body.get("reason") in REPORT_REASONS else "Другое"
    if not await conn.fetchval("SELECT 1 FROM community_posts WHERE id=$1", pid):
        return {"error": "not_found"}
    res = await conn.execute(
        "INSERT INTO community_reports (post_id,user_id,reason) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
        pid, uid, reason)
    if res.endswith(" 1"):
        cnt = await conn.fetchval("SELECT count(*) FROM community_reports WHERE post_id=$1", pid)
        if cnt >= 3:  # три жалобы — скрываем до решения админа
            await conn.execute("UPDATE community_posts SET hidden=TRUE WHERE id=$1", pid)
        asyncio.create_task(_notify(ADMIN_ID, "⚑ Жалоба на пост #%s: %s (всего жалоб: %s)%s" % (
            pid, reason, cnt, "\nПост автоматически скрыт." if cnt >= 3 else ""), pid))
    return {"ok": True}


async def mod(conn, uid, body, user):
    from config import ADMIN_ID
    if uid != ADMIN_ID:
        return {"error": "forbidden"}
    action, oid = body.get("action"), int(body.get("id") or 0)
    if action == "hide_post":
        await conn.execute("UPDATE community_posts SET hidden=TRUE WHERE id=$1", oid)
    elif action == "unhide_post":
        await conn.execute("UPDATE community_posts SET hidden=FALSE WHERE id=$1", oid)
        await conn.execute("DELETE FROM community_reports WHERE post_id=$1", oid)
    elif action == "del_comment":
        await conn.execute("DELETE FROM community_comments WHERE id=$1", oid)
    elif action == "ban":
        if oid == ADMIN_ID:
            return {"error": "self"}
        await conn.execute("INSERT INTO community_bans (user_id) VALUES ($1) ON CONFLICT DO NOTHING", oid)
        await conn.execute("UPDATE community_posts SET hidden=TRUE WHERE user_id=$1", oid)
    elif action == "unban":
        await conn.execute("DELETE FROM community_bans WHERE user_id=$1", oid)
    else:
        return {"error": "bad_action"}
    return {"ok": True}


async def open_message(message):
    """Ответ на кнопку «🧠 Сообщество»: инлайн-кнопка открытия мини-аппа.
    Инлайн web_app-кнопка передаёт initData, кнопка нижней клавиатуры — нет."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
    from config import webapp_url, WEBAPP_BASE_URL
    from db import get_pool
    uid = message.from_user.id
    async with (await get_pool()).acquire() as conn:
        ok = await _allowed(conn, uid)
        banned = await conn.fetchval("SELECT 1 FROM community_bans WHERE user_id=$1", uid)
    if banned:
        await message.answer("Доступ к сообществу ограничен.")
        return
    if not ok:
        await message.answer("🧠 Сообщество Neirosetka скоро откроется — следи за новостями!")
        return
    if not WEBAPP_BASE_URL:
        await message.answer("Сообщество временно недоступно.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🧠 Открыть сообщество", web_app=WebAppInfo(url=webapp_url("/webapp/community")))]])
    await message.answer(
        "<b>Сообщество Neirosetka</b>\n\nВопросы по нейросетям, пошаговые рецепты, коллабы и полезные люди. "
        "Neirosetka отвечает первым 👇", reply_markup=kb, parse_mode="HTML")


async def _lifecycle(app):
    from db import get_pool
    try:
        async with (await get_pool()).acquire() as conn:
            await conn.execute(SCHEMA)
        logging.info("community: таблицы готовы")
    except Exception:
        logging.exception("community: не удалось создать таблицы")
    yield


def install(app):
    async def page(request):
        return web.FileResponse(HTML, headers={"Cache-Control": "no-cache"})
    app.router.add_get("/webapp/community", page)
    for name, fn in {"me": me, "profile": save_profile, "feed": feed, "post": create_post,
                     "post_detail": post_detail, "comment": create_comment, "like": like,
                     "people": people, "ask": ask, "collabs": collabs,
                     "report": report, "mod": mod, "best": best, "top": top}.items():
        app.router.add_post(f"/api/community/{name}", _api(fn))
    app.cleanup_ctx.append(_lifecycle)
