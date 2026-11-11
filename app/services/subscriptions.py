"""Сервис подписок.

Источник истины — наша БД (users.tier / expires_at / queued_lite_s). Remnawave — «исполнитель»:
после каждого изменения мы синхронизируем в панель срок, лимит устройств, лимит трафика и сквады тарифа.
Если панель недоступна, пользователь помечается sync_pending и воркер повторяет синхронизацию.

Важно: HTTP-запросы к панели НИКОГДА не выполняются внутри транзакции БД (пока открыто соединение и держится
блокировка строки). Схема везде одна: коротко читаем → закрываем транзакцию → идём в панель →
коротко записываем результат. Иначе медленная панель выедает пул соединений, и встаёт весь бот.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select, update

from app.bot import labels
from app.bot.ui import btn, kb
from app.core import subscription as core
from app.core.backoff import sync_backoff
from app.core.limits import device_limit as calc_device_limit
from app.core.limits import traffic_limit_bytes
from app.core.subscription import SubState, Tier, normalize
from app.db.models import Plan, SubEvent, User
from app.integrations.remnawave import RemnaError, RemnaNotFound
from app.utils import fmt_dt, iso_z, truncate, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.context import AppContext

log = logging.getLogger(__name__)

Op = Callable[[SubState, datetime], SubState]

PANEL_DEGRADED_KEY = "ampire:panel:degraded"
# операции, после которых пробные ограничения (трафик, устройства) снимаются: человек платит или получил доступ от админа
LIFT_TRIAL_LIMIT_KINDS = frozenset({"buy", "upgrade", "admin_add", "admin_set"})


class TrialError(Exception):
    """reason: disabled | used | active | unavailable"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class UserSnap:
    """Снимок полей пользователя, нужных для общения с панелью (чтобы не держать ORM-объект и транзакцию)."""

    id: int
    username: str | None
    first_name: str
    is_banned: bool
    remna_id: int | None
    extra_devices: int
    trial_limited: bool
    state: SubState

    def key(self) -> tuple:
        """Если за время похода в панель это изменилось — результат синхронизации уже устарел."""
        s = self.state
        return (s.tier, s.expires_at, s.queued_lite, self.is_banned, self.extra_devices, self.trial_limited)


class SubscriptionService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ блокировки
    def lock(self, user_id: int):
        """Распределённая (Redis) блокировка пользователя — работает между репликами и процессами."""
        return self.ctx.locks.user(user_id)

    # ------------------------------------------------------------------ состояние
    @staticmethod
    def state_of(u: User) -> SubState:
        return SubState(
            Tier(u.tier) if u.tier else None,
            u.expires_at,
            timedelta(seconds=int(u.queued_lite_s or 0)),
        )

    @classmethod
    def view(cls, u: User, now: datetime | None = None) -> SubState:
        """Актуальное состояние на текущий момент (даже если воркер ещё не продвинул строку)."""
        return normalize(cls.state_of(u), now or utcnow())

    @staticmethod
    def apply_to_row(u: User, st: SubState) -> None:
        u.tier = st.tier.value if st.tier else None
        u.expires_at = st.expires_at
        u.queued_lite_s = int(st.queued_lite.total_seconds())
        if st.tier is None:
            u.extra_devices = 0  # доп. устройства живут вместе с подпиской

    @staticmethod
    def snapshot(u: User, now: datetime | None = None) -> UserSnap:
        return UserSnap(
            id=u.id,
            username=u.username,
            first_name=u.first_name or "",
            is_banned=bool(u.is_banned),
            remna_id=u.remna_id,
            extra_devices=int(u.extra_devices or 0),
            trial_limited=bool(u.trial_limited),
            state=normalize(SubscriptionService.state_of(u), now or utcnow()),
        )

    # ------------------------------------------------------------------ лимиты
    def device_limit_for(self, user: User | UserSnap, st: SubState | None = None) -> int:
        """Сколько устройств разрешено сейчас: включено в тариф + докупленные (на пробном периоде — не больше trial_devices)."""
        if st is None:
            st = user.state if isinstance(user, UserSnap) else self.view(user)
        if st.tier is None:
            return 0
        plan = self.ctx.plans.get(st.tier)
        return calc_device_limit(
            plan.device_limit,
            int(user.extra_devices or 0),
            trial_limited=bool(user.trial_limited),
            trial_devices=int(self.ctx.cfg.get("trial_devices") or 1),
        )

    def traffic_limit_for(self, user: User | UserSnap) -> int:
        return traffic_limit_bytes(bool(user.trial_limited), int(self.ctx.cfg.get("trial_traffic_gb") or 0))

    # ------------------------------------------------------------------ изменение подписки
    def apply_op(
        self,
        session: AsyncSession,
        user: User,
        op: Op,
        *,
        kind: str,
        tier: Tier | None = None,
        seconds: int = 0,
        amount: Decimal | None = None,
        actor_id: int | None = None,
        meta: dict | None = None,
    ) -> tuple[SubState, SubState]:
        """Применяет операцию к строке пользователя внутри чужой транзакции (без commit)."""
        now = utcnow()
        old = normalize(self.state_of(user), now)
        new = op(old, now)
        self.apply_to_row(user, new)
        user.sync_pending = True
        user.sync_next_at = None
        user.sync_attempts = 0
        if kind in LIFT_TRIAL_LIMIT_KINDS:
            user.trial_limited = False
        session.add(
            SubEvent(
                user_id=user.id,
                kind=kind,
                tier=tier.value if tier else None,
                seconds=seconds,
                amount=amount,
                actor_id=actor_id,
                meta=meta or {},
            )
        )
        return old, new

    def add_extra_devices(
        self,
        session: AsyncSession,
        user: User,
        count: int,
        *,
        kind: str = "devices",
        amount: Decimal | None = None,
        actor_id: int | None = None,
        meta: dict | None = None,
    ) -> int:
        """Меняет число доп. устройств (count может быть отрицательным) внутри чужой транзакции. Возвращает новое число."""
        user.extra_devices = max(0, int(user.extra_devices or 0) + int(count))
        user.sync_pending = True
        user.sync_next_at = None
        user.sync_attempts = 0
        session.add(
            SubEvent(
                user_id=user.id,
                kind=kind,
                tier=user.tier,
                seconds=0,
                amount=amount,
                actor_id=actor_id,
                meta={**(meta or {}), "devices": int(count), "total": user.extra_devices},
            )
        )
        return user.extra_devices

    async def mutate(
        self,
        user_id: int,
        op: Op,
        *,
        kind: str,
        tier: Tier | None = None,
        seconds: int = 0,
        amount: Decimal | None = None,
        actor_id: int | None = None,
        meta: dict | None = None,
        sync: bool = True,
    ) -> tuple[SubState, SubState]:
        async with self.lock(user_id):
            async with self.ctx.session() as s:
                user = await s.get(User, user_id, with_for_update=True)
                if user is None:
                    raise LookupError(f"user {user_id} not found")
                old, new = self.apply_op(
                    s, user, op, kind=kind, tier=tier, seconds=seconds, amount=amount, actor_id=actor_id, meta=meta
                )
                await s.commit()
            if sync:
                await self._sync_locked(user_id)
        return old, new

    # ------------------------------------------------------------------ публичные операции
    async def grant_days(
        self, user_id: int, tier: Tier, days: int, *, kind: str, actor_id: int | None = None, meta: dict | None = None
    ) -> tuple[SubState, SubState]:
        delta = timedelta(days=days)
        return await self.mutate(
            user_id,
            lambda st, now: core.grant(st, tier, delta, now),
            kind=kind,
            tier=tier,
            seconds=int(delta.total_seconds()),
            actor_id=actor_id,
            meta=meta,
        )

    async def grant_delta(
        self, user_id: int, tier: Tier, delta: timedelta, *, kind: str, meta: dict | None = None
    ) -> tuple[SubState, SubState]:
        """Продление на произвольный интервал (льготный период автопродления)."""
        return await self.mutate(
            user_id,
            lambda st, now: core.grant(st, tier, delta, now),
            kind=kind,
            tier=tier,
            seconds=int(delta.total_seconds()),
            meta=meta,
        )

    async def remove_days(
        self, user_id: int, tier: Tier, days: int, *, actor_id: int | None = None, kind: str = "admin_remove"
    ) -> tuple[SubState, SubState]:
        delta = timedelta(days=days)
        return await self.mutate(
            user_id,
            lambda st, now: core.remove(st, tier, delta, now),
            kind=kind,
            tier=tier,
            seconds=-int(delta.total_seconds()),
            actor_id=actor_id,
        )

    async def set_exact(
        self, user_id: int, tier: Tier | None, days_from_now: int, *, actor_id: int | None = None
    ) -> tuple[SubState, SubState]:
        def op(_: SubState, now: datetime) -> SubState:
            return core.set_exact(tier, now + timedelta(days=days_from_now) if tier else None, now)

        return await self.mutate(
            user_id, op, kind="admin_set", tier=tier, seconds=days_from_now * 86400, actor_id=actor_id
        )

    async def change_extra_devices(
        self, user_id: int, delta: int, *, kind: str, actor_id: int | None = None, meta: dict | None = None
    ) -> int:
        """Меняет число доп. устройств у пользователя (с блокировкой и синхронизацией). Возвращает новое число."""
        async with self.lock(user_id):
            async with self.ctx.session() as s:
                user = await s.get(User, user_id, with_for_update=True)
                if user is None:
                    raise LookupError(user_id)
                total = self.add_extra_devices(s, user, delta, kind=kind, actor_id=actor_id, meta=meta)
                await s.commit()
            await self._sync_locked(user_id)
        return total

    async def record_consent(self, user_id: int, version: str) -> None:
        """Фиксирует акцепт оферты и политики (время и версия документов)."""
        async with self.ctx.session() as s:
            await s.execute(update(User).where(User.id == user_id).values(consent_at=utcnow(), consent_version=version))
            await s.commit()

    # ------------------------------------------------------------------ пробный период
    async def activate_trial(self, user_id: int) -> SubState:
        """Выдаёт пробный период. Состояние пишем в БД сразу, а в панель — после (вне транзакции).

        Если панель сейчас недоступна, пользователь всё равно получает пробный период: sync_pending=True,
        воркер доведёт дело до конца, а бот покажет «настраиваем доступ» и кнопку «Повторить».
        """
        cfg = self.ctx.cfg
        if not cfg.get("trial_enabled"):
            raise TrialError("disabled")
        tier = Tier(cfg.get("trial_tier"))
        plan = self.ctx.plans.get(tier)
        if not self.ctx.plans.is_sellable(plan):
            raise TrialError("unavailable")
        days = int(cfg.get("trial_days"))
        async with self.lock(user_id):
            async with self.ctx.session() as s:
                user = await s.get(User, user_id, with_for_update=True)
                if user is None:
                    raise LookupError(user_id)
                if user.trial_used:
                    raise TrialError("used")
                now = utcnow()
                old = normalize(self.state_of(user), now)
                if old.is_active(now):
                    raise TrialError("active")
                new = core.grant(old, tier, timedelta(days=days), now)
                self.apply_to_row(user, new)
                user.sync_pending = True
                user.sync_next_at = None
                user.sync_attempts = 0
                user.trial_used = True
                user.trial_at = now
                user.trial_limited = True
                s.add(SubEvent(user_id=user_id, kind="trial", tier=tier.value, seconds=days * 86400, meta={}))
                await s.commit()
            await self._sync_locked(user_id)  # вне транзакции; при сбое останется sync_pending
        return new

    # ------------------------------------------------------------------ синхронизация с Remnawave
    @staticmethod
    def _description(user: User | UserSnap) -> str:
        parts = [f"tg:{user.id}"]
        if user.username:
            parts.append(f"@{user.username}")
        if user.first_name:
            parts.append(user.first_name)
        return truncate(" ".join(parts), 240)

    @staticmethod
    def _extract(data: dict) -> dict:
        info: dict[str, Any] = {}
        if data.get("id") is not None:
            info["remna_id"] = int(data["id"])
        if data.get("shortUuid"):
            info["short_uuid"] = data["shortUuid"]
        if data.get("subscriptionUrl"):
            info["sub_url"] = data["subscriptionUrl"]
        if data.get("username"):
            info["remna_username"] = data["username"]
        return info

    @staticmethod
    def _apply_info(user: User, info: dict) -> None:
        for k, v in info.items():
            setattr(user, k, v)

    def _remna_username(self, user_id: int) -> str:
        return f"{self.ctx.settings.remna_username_prefix.strip() or 'ampire'}_{user_id}"

    def _username_candidates(self, user_id: int) -> list[str]:
        """Имена, под которыми пользователь мог быть создан ранее (включая прежний бренд) — чтобы не плодить дубли."""
        names = [self._remna_username(user_id)]
        legacy = f"celeste_{user_id}"
        if legacy not in names:
            names.append(legacy)
        return names

    def _patch_fields(self, user: User | UserSnap, st: SubState, plan: Plan | None, now: datetime) -> dict:
        f: dict[str, Any] = {"telegramId": user.id, "description": self._description(user)}
        if st.is_active(now) and st.total_end is not None:
            f["expireAt"] = iso_z(st.total_end)
            f["status"] = "DISABLED" if user.is_banned else "ACTIVE"
            f["trafficLimitBytes"] = self.traffic_limit_for(user)
            f["trafficLimitStrategy"] = "NO_RESET"
            if plan is not None:
                f["hwidDeviceLimit"] = self.device_limit_for(user, st)
                if plan.squads:
                    f["activeInternalSquads"] = list(plan.squads)
        else:
            f["expireAt"] = iso_z(min(st.expires_at or now, now))
            if user.is_banned:
                f["status"] = "DISABLED"
        return f

    @staticmethod
    def _is_conflict(e: RemnaError) -> bool:
        msg = str(e).lower()
        return e.code == "A019" or e.status == 409 or "already exist" in msg

    async def _push(self, snap: UserSnap, st: SubState, plan: Plan | None) -> dict:
        """Приводит пользователя в панели к состоянию ``st``. Возвращает данные для users.*

        Работает только со снимком и сетью — соединения с БД здесь нет.
        """
        remna = self.ctx.remna
        now = utcnow()
        if snap.remna_id is None:
            if not st.is_active(now) or plan is None:
                return {}  # создавать в панели пока нечего
            username = self._remna_username(snap.id)
            try:
                data = await remna.create_user(
                    username=username,
                    expire_at=st.total_end or now,
                    hwid_limit=self.device_limit_for(snap, st),
                    squads=list(plan.squads or []),
                    telegram_id=snap.id,
                    description=self._description(snap),
                    traffic_limit_bytes=self.traffic_limit_for(snap),
                )
            except RemnaError as e:
                if not self._is_conflict(e):
                    raise
                found = None  # уже есть (например, после сбоя или под прежним именем) — подхватываем
                for name in self._username_candidates(snap.id):
                    found = await remna.resolve_user(username=name)
                    if found and found.get("id") is not None:
                        break
                if not found or found.get("id") is None:
                    raise
                data = await remna.update_user(int(found["id"]), **self._patch_fields(snap, st, plan, now))
                data = {**found, **data}
            info = self._extract(data)
            if snap.is_banned and info.get("remna_id"):
                await remna.disable_user(info["remna_id"])
            return info

        try:
            data = await remna.update_user(snap.remna_id, **self._patch_fields(snap, st, plan, now))
        except RemnaNotFound:
            log.warning("пользователь %s удалён из панели — создаю заново", snap.id)
            snap.remna_id = None
            info = await self._push(snap, st, plan)
            # сбрасываем устаревшие данные подписки, даже если создавать пока нечего
            return {"remna_id": None, "short_uuid": None, "sub_url": None, "remna_username": None, **info}
        return self._extract(data) if data else {}

    async def _mark_panel_degraded(self) -> None:
        try:
            await self.ctx.redis.set(PANEL_DEGRADED_KEY, int(time.time()), ex=3600)
        except Exception:  # noqa: BLE001
            pass

    async def _sync_failed(self, user_id: int, err: Exception, now: datetime) -> None:
        async with self.ctx.session() as s:
            user = await s.get(User, user_id, with_for_update=True)
            if user is None:
                return
            user.sync_pending = True
            user.sync_error = str(err)[:500]
            user.sync_attempts = (user.sync_attempts or 0) + 1
            user.sync_next_at = now + sync_backoff(user.sync_attempts)
            attempts = user.sync_attempts
            await s.commit()
        await self._mark_panel_degraded()
        log.warning("синхронизация %s не удалась (попытка %s): %s", user_id, attempts, err)

    async def _sync_done(self, user_id: int, key: tuple, info: dict, now: datetime) -> None:
        async with self.ctx.session() as s:
            user = await s.get(User, user_id, with_for_update=True)
            if user is None:
                return
            self._apply_info(user, info)
            current = self.snapshot(user, now)
            if current.key() == key:
                user.sync_pending = False
                user.sync_error = None
                user.sync_attempts = 0
                user.sync_next_at = None
                user.synced_at = now
            else:
                # пока мы говорили с панелью, у пользователя что-то изменилось — нужен ещё один проход
                user.sync_pending = True
                user.sync_next_at = None
            await s.commit()

    async def _sync_locked(self, user_id: int) -> bool:
        """Синхронизация; вызывать, удерживая self.lock(user_id). Три шага — и ни одного HTTP внутри транзакции."""
        now = utcnow()
        # 1) короткое чтение
        async with self.ctx.session() as s:
            user = await s.get(User, user_id)
            if user is None:
                return False
            snap = self.snapshot(user, now)
        key = snap.key()
        plan = self.ctx.plans.get(snap.state.tier) if snap.state.tier else None
        # 2) сеть — соединение с БД не держим
        try:
            info = await self._push(snap, snap.state, plan)
        except RemnaError as e:
            await self._sync_failed(user_id, e, now)
            return False
        # 3) короткая запись результата (ключ — тот, что был ДО похода в панель)
        await self._sync_done(user_id, key, info, now)
        return True

    async def sync_now(self, user_id: int) -> bool:
        async with self.lock(user_id):
            return await self._sync_locked(user_id)

    async def sync_inside_lock(self, user_id: int) -> bool:
        return await self._sync_locked(user_id)

    async def resync_many(self, user_ids: list[int], *, concurrency: int = 5) -> tuple[int, int]:
        sem = asyncio.Semaphore(concurrency)
        ok = fail = 0

        async def one(uid: int) -> None:
            nonlocal ok, fail
            async with sem:
                try:
                    good = await self.sync_now(uid)
                except Exception:  # noqa: BLE001
                    log.exception("resync %s", uid)
                    good = False
                if good:
                    ok += 1
                else:
                    fail += 1

        for i in range(0, len(user_ids), 200):
            await asyncio.gather(*(one(u) for u in user_ids[i : i + 200]))
        return ok, fail

    async def _panel_recovered(self) -> bool:
        """Панель ожила после сбоя? Тогда пауза (бэкофф) у всех ожидающих синхронизации сбрасывается.

        Раньше пауза доходила до часа и не сбрасывалась: оплативший новичок ждал доступ, хотя панель давно работала.
        """
        local = self.ctx.remna.take_recovered()
        try:
            degraded = await self.ctx.redis.get(PANEL_DEGRADED_KEY)
        except Exception:  # noqa: BLE001
            degraded = None
        if not local and not degraded:
            return False
        if not local:
            # метка сбоя стоит, но «ожила» ли панель — проверяем лёгким запросом (раз в итерацию воркера)
            try:
                await self.ctx.remna.system_stats()
            except RemnaError:
                return False
        try:
            await self.ctx.redis.delete(PANEL_DEGRADED_KEY)
        except Exception:  # noqa: BLE001
            pass
        async with self.ctx.session() as s:
            await s.execute(
                update(User).where(User.sync_pending.is_(True), User.sync_next_at.is_not(None)).values(sync_next_at=None, sync_attempts=0)
            )
            await s.commit()
        log.info("панель Remnawave снова доступна — бэкофф синхронизации сброшен")
        return True

    async def retry_pending(self, limit: int = 300, concurrency: int = 10) -> int:
        """Повторяет синхронизацию (с экспоненциальной паузой до 10 мин). Пока панель «лежит» — не долбим её.

        Первыми обслуживаются оплатившие новички без ссылки подписки: им доступ нужен прямо сейчас.
        """
        if self.ctx.remna.circuit_open:
            return 0
        await self._panel_recovered()
        now = utcnow()
        async with self.ctx.session() as s:
            ids = list(
                (
                    await s.scalars(
                        select(User.id)
                        .where(User.sync_pending.is_(True), or_(User.sync_next_at.is_(None), User.sync_next_at <= now))
                        .order_by(User.sub_url.is_(None).desc(), User.sync_next_at.asc().nulls_first())
                        .limit(limit)
                    )
                ).all()
            )
        if not ids:
            return 0
        ok, _ = await self.resync_many(ids, concurrency=concurrency)
        return ok

    # ------------------------------------------------------------------ ссылка подписки
    async def revoke_link(self, user_id: int) -> dict:
        """Выпускает новую ссылку подписки (старая перестаёт работать). Запрос в панель — вне транзакции."""
        async with self.lock(user_id):
            async with self.ctx.session() as s:
                user = await s.get(User, user_id)
                if user is None or not user.remna_id:
                    raise LookupError("у пользователя нет учётной записи в панели")
                remna_id = user.remna_id
            data = await self.ctx.remna.revoke_subscription(remna_id)
            info = self._extract(data or {})
            if not info.get("sub_url") or not info.get("short_uuid"):
                info = self._extract(await self.ctx.remna.get_user(remna_id))
            info.pop("remna_id", None)
            async with self.ctx.session() as s:
                user = await s.get(User, user_id, with_for_update=True)
                if user is not None:
                    self._apply_info(user, info)
                    await s.commit()
        return info

    # ------------------------------------------------------------------ истечение сроков
    async def advance_user(self, user_id: int) -> str | None:
        """Переводит пользователя на следующий отрезок (PREMIUM -> LITE) или снимает подписку.

        Возвращает "downgrade" | "expire" | None и сразу уведомляет пользователя.
        """
        kind: str | None = None
        new: SubState | None = None
        async with self.lock(user_id):
            async with self.ctx.session() as s:
                user = await s.get(User, user_id, with_for_update=True)
                now = utcnow()
                if user is None or user.tier is None or user.expires_at is None or user.expires_at > now:
                    return None
                old = self.state_of(user)
                new = normalize(old, now)
                kind = "downgrade" if (new.tier == Tier.LITE and old.tier == Tier.PREMIUM) else "expire"
                self.apply_to_row(user, new)
                user.sync_pending = True
                user.sync_next_at = None
                s.add(SubEvent(user_id=user_id, kind=kind, tier=old.tier.value if old.tier else None, meta={}))
                await s.commit()
            await self._sync_locked(user_id)

        texts, notify = self.ctx.texts, self.ctx.notify
        if kind == "downgrade" and new is not None:
            note = ""
            if self.ctx.cfg.get("trim_devices_on_downgrade"):
                removed = await self.ctx.devices.trim_to_limit(user_id)
                if removed:
                    async with self.ctx.session() as s:
                        fresh = await s.get(User, user_id)
                    limit = self.device_limit_for(fresh, new) if fresh else self.ctx.plans.get(Tier.LITE).device_limit
                    note = texts.render("n_devices_trimmed", limit=limit, removed=", ".join(removed))
            await notify.send(
                user_id,
                texts.render("n_downgraded", until=fmt_dt(new.expires_at), devices_note=note),
                kb(btn(labels.SUBSCRIPTION, "menu:sub")),
                paced=True,
            )
        elif kind == "expire":
            await notify.send(user_id, texts.render("n_expired"), kb(btn(labels.BUY, "pay:menu")), paced=True)
        return kind

    async def advance_due(self, batch: int = 300, concurrency: int = 8, budget: float = 25.0) -> int:
        """Переводит всех «просроченных» пользователей на следующий отрезок. Постранично, с параллелизмом и бюджетом времени."""
        t0 = time.monotonic()
        total = 0
        last_id = 0
        sem = asyncio.Semaphore(concurrency)

        async def one(uid: int) -> bool:
            async with sem:
                try:
                    return bool(await self.advance_user(uid))
                except Exception:  # noqa: BLE001
                    log.exception("advance_user %s", uid)
                    return False

        while time.monotonic() - t0 < budget:
            now = utcnow()
            async with self.ctx.session() as s:
                ids = list(
                    (
                        await s.scalars(
                            select(User.id)
                            .where(User.tier.is_not(None), User.expires_at <= now, User.id > last_id)
                            .order_by(User.id)
                            .limit(batch)
                        )
                    ).all()
                )
            if not ids:
                break
            last_id = ids[-1]
            total += sum(await asyncio.gather(*(one(u) for u in ids)))
            if len(ids) < batch:
                break
        return total
