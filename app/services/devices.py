from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import DeviceAlias, User
from app.integrations.remnawave import RemnaError
from app.utils import esc, fmt_dt, parse_iso, truncate

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)


def platform_name(platform: str | None) -> str:
    """Короткое «человеческое» название платформы для подписи устройства."""
    p = (platform or "").lower()
    if "ios" in p or "iphone" in p or "ipad" in p:
        return "iOS"
    if "android" in p:
        return "Android"
    if "win" in p:
        return "Windows"
    if "mac" in p or "darwin" in p:
        return "macOS"
    if "linux" in p:
        return "Linux"
    return ""


@dataclass(slots=True)
class Device:
    hwid: str
    platform: str
    os_version: str
    model: str
    user_agent: str
    created_at: datetime | None
    alias: str | None = None

    @property
    def key(self) -> str:
        """Короткий ключ для callback_data."""
        return hashlib.sha1(self.hwid.encode()).hexdigest()[:10]

    @property
    def label(self) -> str:
        base = self.alias or self.model or " ".join(x for x in (platform_name(self.platform) or self.platform, self.os_version) if x) or "Устройство"
        return truncate(base, 32)

    @property
    def button(self) -> str:
        return self.label

    def info(self) -> str:
        rows = []
        if self.platform:
            rows.append(f"Платформа: <b>{esc(self.platform)}</b>")
        if self.os_version:
            rows.append(f"Версия ОС: {esc(self.os_version)}")
        if self.model:
            rows.append(f"Модель: {esc(self.model)}")
        if self.user_agent:
            rows.append(f"Приложение: {esc(truncate(self.user_agent, 60))}")
        rows.append(f"Добавлено: {fmt_dt(self.created_at)}")
        return "\n".join(rows)


class DeviceService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def limit_for(self, user: User) -> int:
        """Сколько устройств разрешено: включено в тариф + докупленные (на пробном периоде — меньше)."""
        return self.ctx.subs.device_limit_for(user)

    async def _aliases(self, user_id: int) -> dict[str, str]:
        async with self.ctx.session() as s:
            rows = (await s.execute(select(DeviceAlias).where(DeviceAlias.user_id == user_id))).scalars().all()
        return {r.hwid: r.alias for r in rows}

    @staticmethod
    def _key(user: User) -> str:
        return f"dev:{user.remna_id}"

    async def list(self, user: User, *, fresh: bool = False) -> list[Device]:
        """Список устройств пользователя. Бросает RemnaError, если панель недоступна.

        По умолчанию — из кэша на 20 секунд (меню открывают часто, а панель одна на всех).
        """
        if not user.remna_id:
            return []
        raw = await self.ctx.cache.remember(
            self._key(user), 20, lambda: self.ctx.remna.list_devices(user.remna_id), force=fresh
        )
        aliases = await self._aliases(user.id)
        out = [
            Device(
                hwid=str(d.get("hwid", "")),
                platform=str(d.get("platform") or ""),
                os_version=str(d.get("osVersion") or ""),
                model=str(d.get("deviceModel") or ""),
                user_agent=str(d.get("userAgent") or ""),
                created_at=parse_iso(d.get("createdAt")),
                alias=aliases.get(str(d.get("hwid", ""))),
            )
            for d in raw
            if d.get("hwid")
        ]
        out.sort(key=lambda x: x.created_at.timestamp() if x.created_at else 0)
        return out

    async def find(self, user: User, key: str) -> Device | None:
        for d in await self.list(user, fresh=True):
            if d.key == key:
                return d
        return None

    async def delete(self, user: User, key: str) -> bool:
        dev = await self.find(user, key)
        if dev is None or not user.remna_id:
            return False
        await self.ctx.remna.delete_device(user.remna_id, dev.hwid)
        await self.ctx.cache.delete(self._key(user))
        async with self.ctx.session() as s:
            await s.execute(delete(DeviceAlias).where(DeviceAlias.user_id == user.id, DeviceAlias.hwid == dev.hwid))
            await s.commit()
        return True

    async def delete_all(self, user: User) -> None:
        if user.remna_id:
            await self.ctx.remna.delete_all_devices(user.remna_id)
            await self.ctx.cache.delete(self._key(user))
            async with self.ctx.session() as s:
                await s.execute(delete(DeviceAlias).where(DeviceAlias.user_id == user.id))
                await s.commit()

    async def rename(self, user: User, key: str, name: str) -> bool:
        dev = await self.find(user, key)
        if dev is None:
            return False
        name = name.strip()[:24]
        async with self.ctx.session() as s:
            stmt = pg_insert(DeviceAlias).values(user_id=user.id, hwid=dev.hwid, alias=name)
            stmt = stmt.on_conflict_do_update(
                index_elements=[DeviceAlias.user_id, DeviceAlias.hwid], set_={"alias": stmt.excluded.alias}
            )
            await s.execute(stmt)
            await s.commit()
        return True

    async def trim_to_limit(self, user_id: int) -> list[str]:
        """Оставляет самые старые устройства в пределах лимита тарифа, остальные отключает.

        Возвращает названия отключённых устройств.
        """
        user = await self.ctx.users.get(user_id)
        if user is None or not user.remna_id:
            return []
        limit = self.limit_for(user)
        if limit <= 0:
            return []
        try:
            devices = await self.list(user, fresh=True)
            removed: list[str] = []
            for dev in devices[limit:]:
                await self.ctx.remna.delete_device(user.remna_id, dev.hwid)
                removed.append(dev.label)
            if removed:
                await self.ctx.cache.delete(self._key(user))
            return removed
        except RemnaError:
            log.exception("trim_to_limit %s", user_id)
            return []
