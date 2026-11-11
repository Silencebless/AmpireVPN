"""Хранилище FSM, которое переживает падение Redis.

RedisStorage при недоступном Redis бросает исключение на КАЖДОМ апдейте (aiogram читает состояние ещё до хендлера),
из-за чего бот «молчит», хотя база цела. Здесь при сбое Redis состояние временно живёт в памяти процесса —
бот отвечает, а незавершённые диалоги (ввод email, промокода) могут сброситься. Это осознанная деградация.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.infra.metrics import metrics

log = logging.getLogger(__name__)


class FailOpenStorage(BaseStorage):
    def __init__(self, primary: BaseStorage) -> None:
        self.primary = primary
        self.fallback = MemoryStorage()
        self._last_warn = 0.0
        self.degraded = False

    def _failed(self, op: str, exc: Exception) -> None:
        metrics.inc("fsm_storage_errors_total", op=op)
        self.degraded = True
        now = time.monotonic()
        if now - self._last_warn > 30:
            self._last_warn = now
            log.warning("FSM: Redis недоступен (%s: %s) — состояние диалогов временно в памяти", type(exc).__name__, exc)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        try:
            await self.primary.set_state(key, state)
        except Exception as e:  # noqa: BLE001
            self._failed("set_state", e)
            await self.fallback.set_state(key, state)

    async def get_state(self, key: StorageKey) -> str | None:
        try:
            value = await self.primary.get_state(key)
            self.degraded = False
            return value
        except Exception as e:  # noqa: BLE001
            self._failed("get_state", e)
            return await self.fallback.get_state(key)

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        try:
            await self.primary.set_data(key, data)
        except Exception as e:  # noqa: BLE001
            self._failed("set_data", e)
            await self.fallback.set_data(key, data)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        try:
            return await self.primary.get_data(key)
        except Exception as e:  # noqa: BLE001
            self._failed("get_data", e)
            return await self.fallback.get_data(key)

    async def update_data(self, key: StorageKey, data: dict[str, Any]) -> dict[str, Any]:
        current = dict(await self.get_data(key))
        current.update(data)
        await self.set_data(key, current)
        return dict(current)

    async def close(self) -> None:
        for st in (self.primary, self.fallback):
            try:
                await st.close()
            except Exception:  # noqa: BLE001
                log.debug("ошибка при закрытии хранилища FSM", exc_info=True)
