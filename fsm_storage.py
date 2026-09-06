# -*- coding: utf-8 -*-
"""Хранилище FSM-состояний в PostgreSQL.

Зачем: со штатным MemoryStorage состояние диалога живёт в памяти процесса, и
каждый деплой выбрасывает клиентов из середины сценария («⚠️ Сессия сброшена»).
Здесь состояние лежит в той же базе, что и всё остальное, и переживает рестарт.

Безопасность прежде всего: ЛЮБАЯ ошибка базы не должна ломать диалоги, поэтому
каждый метод при сбое молча уходит в резервное хранилище в памяти — то есть в
худшем случае поведение возвращается к нынешнему, а не к неработающему боту.
"""
import json
import logging
from typing import Any, Dict, Optional

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.fsm.storage.memory import MemoryStorage


def _key_str(key: StorageKey) -> str:
    """Строковый ключ записи. Собираем через getattr — состав полей StorageKey
    менялся между версиями aiogram (thread_id, business_connection_id)."""
    return ":".join(str(x) for x in (
        getattr(key, "bot_id", 0) or 0,
        getattr(key, "chat_id", 0) or 0,
        getattr(key, "user_id", 0) or 0,
        getattr(key, "thread_id", "") or "",
        getattr(key, "business_connection_id", "") or "",
        getattr(key, "destiny", "default") or "default",
    ))


class PostgresStorage(BaseStorage):
    """FSM-хранилище на существующем пуле asyncpg."""

    def __init__(self):
        self._fallback = MemoryStorage()
        self._degraded = False          # был ли сбой БД (для одноразового лога)

    async def _pool(self):
        from db import get_pool        # импорт внутри — иначе circular import
        return await get_pool()

    def _degrade(self, where: str, exc: Exception):
        if not self._degraded:
            self._degraded = True
            logging.error(f"FSM-хранилище: сбой БД в {where} — работаю на памяти: {exc}")
        else:
            logging.debug(f"FSM-хранилище {where}: {exc}")

    # ── состояние ────────────────────────────────────────────────────────────
    async def set_state(self, key: StorageKey, state: Optional[Any] = None) -> None:
        _s = state.state if isinstance(state, State) else state
        try:
            pool = await self._pool()
            async with pool.acquire() as conn:
                if _s is None:
                    await conn.execute(
                        "UPDATE fsm_storage SET state=NULL, updated_at=NOW() WHERE key=$1",
                        _key_str(key))
                else:
                    await conn.execute(
                        "INSERT INTO fsm_storage (key, state) VALUES ($1,$2) "
                        "ON CONFLICT (key) DO UPDATE SET state=$2, updated_at=NOW()",
                        _key_str(key), str(_s))
        except Exception as e:
            self._degrade("set_state", e)
            await self._fallback.set_state(key, state)

    async def get_state(self, key: StorageKey) -> Optional[str]:
        try:
            pool = await self._pool()
            async with pool.acquire() as conn:
                return await conn.fetchval(
                    "SELECT state FROM fsm_storage WHERE key=$1", _key_str(key))
        except Exception as e:
            self._degrade("get_state", e)
            return await self._fallback.get_state(key)

    # ── данные ───────────────────────────────────────────────────────────────
    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        try:
            _payload = json.dumps(data or {}, ensure_ascii=False, default=str)
        except Exception as e:
            # Данные не сериализуются — не теряем диалог, кладём в память
            self._degrade("set_data:json", e)
            await self._fallback.set_data(key, data)
            return
        try:
            pool = await self._pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO fsm_storage (key, data) VALUES ($1,$2::jsonb) "
                    "ON CONFLICT (key) DO UPDATE SET data=$2::jsonb, updated_at=NOW()",
                    _key_str(key), _payload)
        except Exception as e:
            self._degrade("set_data", e)
            await self._fallback.set_data(key, data)

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        try:
            pool = await self._pool()
            async with pool.acquire() as conn:
                raw = await conn.fetchval(
                    "SELECT data FROM fsm_storage WHERE key=$1", _key_str(key))
            if not raw:
                return {}
            if isinstance(raw, str):
                raw = json.loads(raw)
            return dict(raw or {})
        except Exception as e:
            self._degrade("get_data", e)
            return await self._fallback.get_data(key)

    async def close(self) -> None:
        try:
            await self._fallback.close()
        except Exception:
            pass
