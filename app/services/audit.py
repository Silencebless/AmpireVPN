from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, select

from app.db.models import AuditLog

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)


class AuditService:
    """Журнал действий администраторов."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def log(self, actor_id: int | None, action: str, target: str | int | None = None, **details: Any) -> None:
        try:
            async with self.ctx.session() as s:
                s.add(
                    AuditLog(
                        actor_id=actor_id,
                        action=action[:64],
                        target=str(target)[:64] if target is not None else None,
                        details={k: (v if isinstance(v, (int, float, bool, str, type(None))) else str(v)) for k, v in details.items()},
                    )
                )
                await s.commit()
        except Exception:  # noqa: BLE001 — аудит не должен ломать основное действие
            log.exception("audit log failed")

    async def recent(self, limit: int = 30, offset: int = 0) -> list[AuditLog]:
        async with self.ctx.session() as s:
            res = await s.execute(select(AuditLog).order_by(desc(AuditLog.id)).limit(limit).offset(offset))
            return list(res.scalars().all())
