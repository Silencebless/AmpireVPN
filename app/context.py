"""Контейнер зависимостей приложения (передаётся в хендлеры как ``ctx``)."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.infra.bus import Bus
from app.infra.cache import Cache
from app.infra.locks import LockManager
from app.infra.ratelimit import RateLimiter
from app.integrations.cryptobot import CryptoBotClient
from app.integrations.remnawave import RemnaClient
from app.integrations.yookassa import YooKassaClient
from app.permissions import allowed, role_title
from app.utils import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.services.audit import AuditService
    from app.services.backup import BackupService
    from app.services.broadcast import BroadcastService
    from app.services.devices import DeviceService
    from app.services.linkcheck import LinkChecker
    from app.services.notifier import Notifier
    from app.services.payments import PaymentService
    from app.services.plans import PlanService
    from app.services.promos import PromoService
    from app.services.referrals import ReferralService
    from app.services.runtime_settings import SettingsService
    from app.services.stats import StatsService
    from app.services.subscriptions import SubscriptionService
    from app.services.text_service import TextService
    from app.services.users import UserService

HEARTBEAT_KEY = "ampire:hb"


class AppContext:
    cfg: SettingsService
    texts: TextService
    users: UserService
    plans: PlanService
    subs: SubscriptionService
    pay: PaymentService
    promos: PromoService
    refs: ReferralService
    devices: DeviceService
    stats: StatsService
    bc: BroadcastService
    audit: AuditService
    notify: Notifier
    backup: BackupService
    links: LinkChecker

    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: Redis,
        bot: Bot,
        remna: RemnaClient,
        yk: YooKassaClient | None,
        crypto: CryptoBotClient | None = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.bot = bot
        self.remna = remna
        self.yk = yk
        self.crypto = crypto
        # инфраструктура масштабирования
        self.locks = LockManager(redis)
        self.limiter = RateLimiter(redis)
        self.cache = Cache(redis)
        self.bus = Bus(redis)
        self.instance_id = uuid.uuid4().hex[:8]
        self.bot_username: str = ""
        self.admin_ids: set[int] = set()
        self.admin_roles: dict[int, str] = {}
        self.started_at: datetime = utcnow()
        self.health_engine = None  # отдельный маленький пул для /readyz (не делит соединения с ботом)

    def session(self) -> AsyncSession:
        return self.sessionmaker()

    # --- права ---
    def is_owner(self, user_id: int) -> bool:
        return user_id in self.settings.owners

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.owners or user_id in self.admin_ids

    def all_admin_ids(self) -> set[int]:
        return set(self.settings.owners) | set(self.admin_ids)

    def role_of(self, user_id: int) -> str | None:
        """Роль администратора: 'owner' для владельцев, иначе роль из БД (None — не админ)."""
        if self.is_owner(user_id):
            return "owner"
        return self.admin_roles.get(user_id) if user_id in self.admin_ids else None

    def has_scope(self, user_id: int, scope: str | None) -> bool:
        """Есть ли у администратора право на действие (scope=None — любому админу)."""
        if not self.is_admin(user_id):
            return False
        return allowed(scope, self.admin_roles.get(user_id), owner=self.is_owner(user_id))

    def role_label(self, user_id: int) -> str:
        return role_title(self.admin_roles.get(user_id), owner=self.is_owner(user_id))

    async def reload_admins(self) -> None:
        from sqlalchemy import select

        from app.db.models import Admin

        async with self.session() as s:
            rows = (await s.scalars(select(Admin))).all()
        self.admin_ids = {a.user_id for a in rows}
        self.admin_roles = {a.user_id: (a.role or "admin") for a in rows}

    # --- ссылки ---
    @property
    def support_url(self) -> str:
        raw = (self.cfg.get("support_url") or self.settings.support_url or "").strip()
        if raw.startswith("@"):
            raw = "https://t.me/" + raw[1:]
        elif raw.startswith(("t.me/", "telegram.me/")):
            raw = "https://" + raw
        return raw if raw.startswith(("http://", "https://", "tg://")) else ""

    @property
    def web_ready(self) -> bool:
        """Публичный адрес настроен (Telegram не принимает localhost в URL-кнопках)."""
        u = self.settings.base_url.lower()
        return u.startswith(("http://", "https://")) and not any(h in u for h in ("localhost", "127.0.0.1", "0.0.0.0"))

    def ref_link(self, ref_code: str) -> str:
        return f"https://t.me/{self.bot_username}?start=ref_{ref_code}"

    def dl_url(self) -> str:
        return f"{self.settings.base_url}/dl"

    def add_url(self, short_uuid: str) -> str:
        return f"{self.settings.base_url}/add/{short_uuid}"

    # --- общие для всех реплик «сигналы жизни» (хранятся в Redis) ---
    async def beat(self, name: str) -> None:
        try:
            await self.redis.hset(HEARTBEAT_KEY, name, int(time.time()))
        except Exception:  # noqa: BLE001
            pass

    async def heartbeats(self) -> dict[str, datetime]:
        try:
            raw = await self.redis.hgetall(HEARTBEAT_KEY)
        except Exception:  # noqa: BLE001
            return {}
        return {
            (k.decode() if isinstance(k, bytes) else k): datetime.fromtimestamp(int(v), tz=timezone.utc)
            for k, v in raw.items()
        }

    async def mark_webhook(self, name: str) -> None:
        try:
            await self.redis.set(f"ampire:wh:{name}", int(time.time()))
        except Exception:  # noqa: BLE001
            pass

    async def last_webhook(self, name: str) -> datetime | None:
        try:
            v = await self.redis.get(f"ampire:wh:{name}")
        except Exception:  # noqa: BLE001
            return None
        return datetime.fromtimestamp(int(v), tz=timezone.utc) if v else None
