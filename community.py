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


async def me(conn, uid, body, user):
    p = await conn.fetchrow("SELECT * FROM community_profiles WHERE user_id=$1", uid)
    stats = await conn.fetchrow(
        "SELECT (SELECT count(*) FROM community_posts WHERE user_id=$1 AND NOT hidden) AS posts,"
        " (SELECT count(*) FROM community_comments WHERE user_id=$1 AND NOT is_ai) AS comments", uid)
    return {"profile": _row(p) if p else None, "stats": _row(stats),
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
    return {"ok": True, "id": pid}


async def post_detail(conn, uid, body, user):
    pid = int(body.get("id") or 0)
    p = await conn.fetchrow(
        """SELECT p.id,p.type,p.tag,p.text,p.created_at,coalesce(pr.name,'Участник') AS author,
           (SELECT count(*) FROM community_likes l WHERE l.post_id=p.id) AS likes,
           EXISTS(SELECT 1 FROM community_likes l WHERE l.post_id=p.id AND l.user_id=$2) AS liked
           FROM community_posts p LEFT JOIN community_profiles pr ON pr.user_id=p.user_id
           WHERE p.id=$1 AND NOT p.hidden""", pid, uid)
    if not p:
        return {"error": "not_found"}
    cs = await conn.fetch(
        """SELECT c.id,c.text,c.is_ai,c.created_at,
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
        """SELECT user_id,name,niche,looking,offering,open_collab,username FROM community_profiles
           WHERE ($1='' OR name ILIKE '%'||$1||'%' OR niche ILIKE '%'||$1||'%'
                  OR looking ILIKE '%'||$1||'%' OR offering ILIKE '%'||$1||'%')
           ORDER BY updated_at DESC LIMIT 100""", q)
    return {"people": [_row(r) for r in rows]}


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
                     "people": people}.items():
        app.router.add_post(f"/api/community/{name}", _api(fn))
    app.cleanup_ctx.append(_lifecycle)
