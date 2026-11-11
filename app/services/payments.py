"""Платежи: ЮKassa (карты, СБП, автопродление) и CryptoBot (крипта).

Принципы:
* Уведомлениям не доверяем слепо — по id всегда запрашиваем платёж у провайдера и сверяем сумму.
* Применение платежа идемпотентно: строка платежа блокируется FOR UPDATE, флаг ``applied`` ставится
  в той же транзакции, что и изменение подписки.
* Платёж ищем не только по external_id, но и по metadata.payment_id: даже если мы не успели сохранить id
  (таймаут, перезапуск между шагами), деньги всё равно дойдут до подписки.
* При таймауте/5xx автосписание НЕ отменяется — повторяется с тем же ключом идемпотентности (дубля списания не будет).
* Спорные платежи (иная сумма и т. п.) получают статус needs_review: алерт один раз, повторных проверок нет.
* Потерянные уведомления добирают воркер reconcile_pending (ЮKassa/CryptoBot → БД) и ежедневная сверка
  reconcile_yookassa (список платежей ЮKassa → БД).
* Сетевые вызовы к провайдерам не выполняются внутри транзакций БД.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, desc, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from app.bot import labels
from app.bot.ui import btn, kb
from app.core import subscription as core
from app.core.autopay import (
    CHARGE_BEFORE,
    NOTICE_BEFORE,
    PENDING_GUARD,
    autopay_phase,
    grace_extension,
    is_permanent_decline,
    max_attempts,
    next_attempt_at,
    renewal_tier,
)
from app.core.pricing import (
    PROMO_DAYS,
    TERMS,
    DevicesQuote,
    Quote,
    build_devices_quote,
    build_quote,
    q2,
    renewal_amount,
    term_days,
)
from app.core.provider_status import Status, map_yookassa
from app.core.settlement import (
    CANCELED,
    FINAL_STATUSES,
    NEEDS_REVIEW,
    PENDING,
    REFUNDED,
    REFUNDING,
    SUCCEEDED,
    Refs,
    extract_cryptobot_refs,
    extract_yookassa_refs,
    settle_action,
)
from app.core.subscription import SubState, Tier, normalize
from app.db.models import Payment, PaymentMethod, Promo, PromoUse, User
from app.infra.metrics import metrics
from app.integrations.yookassa import YooKassaError
from app.legal import CONSENT_VERSION
from app.logging_setup import log_user
from app.services.providers import CryptoBotProvider, ProviderError, YooKassaProvider, is_stale
from app.utils import devices_word, esc, fmt_dt, iso_z, money, months_word, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

BRAND = "AmpireVPN"
NO_EXTERNAL_ID_CANCEL_AFTER = timedelta(minutes=10)      # обычный платёж без external_id: ссылку никто не видел — отменяем
AUTOPAY_KEY_LIFETIME = timedelta(hours=20)               # ключ идемпотентности ЮKassa живёт 24 ч — повторяем с запасом
MISSED_CYCLE_WINDOW = timedelta(days=2)                  # подписка истекла, пока воркер не работал — всё равно пробуем списать
REFUND_STUCK_AFTER = timedelta(minutes=10)
RESERVATION_TTL = timedelta(days=7)


class PaymentError(Exception):
    """reason: disabled | no_provider | inactive_plan | provider | not_refundable | need_consent |
    need_autopay_consent | bad_term | devices_unavailable | devices_limit"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class NeedEmail(PaymentError):
    def __init__(self) -> None:
        super().__init__("need_email")


class PaymentService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.providers: dict[str, YooKassaProvider | CryptoBotProvider] = {
            "yookassa": YooKassaProvider(ctx),
            "cryptobot": CryptoBotProvider(ctx),
        }

    # ------------------------------------------------------------------ доступность
    def available_providers(self) -> list[YooKassaProvider | CryptoBotProvider]:
        if not self.ctx.cfg.get("payments_enabled"):
            return []
        return [p for p in self.providers.values() if p.available()]

    def available(self) -> bool:
        return bool(self.available_providers())

    def autopay_possible(self) -> bool:
        return bool(self.ctx.cfg.get("autopay_enabled")) and self.providers["yookassa"].available()

    # ------------------------------------------------------------------ расчёт
    async def pending_promo(self, user: User, tier: Tier) -> Promo | None:
        if not user.pending_promo_id:
            return None
        async with self.ctx.session() as s:
            promo = await s.get(Promo, user.pending_promo_id)
        if promo is None or not promo.is_active or promo.kind == PROMO_DAYS:
            return None
        if promo.expires_at and promo.expires_at <= utcnow():
            return None
        if promo.tiers and tier.value not in promo.tiers:
            return None
        return promo

    def _billed_devices(self, user: User, now: datetime) -> int:
        """Сколько доп. устройств войдёт в продление: только если подписка сейчас активна."""
        st = self.ctx.subs.view(user, now)
        return int(user.extra_devices or 0) if st.is_active(now) else 0

    def _quote(self, user: User, tier: Tier, promo: Promo | None, *, months: int, upgrade: bool) -> Quote:
        plans, cfg = self.ctx.plans, self.ctx.cfg
        now = utcnow()
        return build_quote(
            self.ctx.subs.state_of(user),
            tier,
            price_lite=plans.get(Tier.LITE).price,
            price_premium=plans.get(Tier.PREMIUM).price,
            period_days=plans.get(tier).period_days,
            lite_period_days=plans.get(Tier.LITE).period_days,
            premium_period_days=plans.get(Tier.PREMIUM).period_days,
            now=now,
            want_upgrade=upgrade,
            promo_kind=promo.kind if promo else None,
            promo_value=promo.value if promo else None,
            months=months,
            term_discount_pct=cfg.term_discounts()[months],
            extra_devices=self._billed_devices(user, now),
            device_price=cfg.device_price(),
        )

    async def quote(self, user: User, tier: Tier, *, months: int = 1, upgrade: bool = False) -> tuple[Quote, Promo | None]:
        if months not in TERMS:
            raise PaymentError("bad_term", str(months))
        promo = await self.pending_promo(user, tier)
        return self._quote(user, tier, promo, months=months, upgrade=upgrade), promo

    async def quote_terms(self, user: User, tier: Tier, *, upgrade: bool = False) -> tuple[dict[int, Quote], Promo | None]:
        """Расчёт сразу для всех сроков (1/3/6/12) — один запрос за промокодом."""
        promo = await self.pending_promo(user, tier)
        return {m: self._quote(user, tier, promo, months=m, upgrade=upgrade) for m in TERMS}, promo

    def devices_quote(self, user: User, count: int) -> DevicesQuote | None:
        st = self.ctx.subs.view(user)
        if st.tier is None:
            return None
        return build_devices_quote(
            self.ctx.subs.state_of(user),
            count=count,
            device_price=self.ctx.cfg.device_price(),
            month_days=self.ctx.plans.get(st.tier).period_days,
            now=utcnow(),
        )

    def devices_can_buy(self, user: User) -> int:
        """Сколько ещё доп. устройств можно докупить (0 — нельзя: нет подписки, пробный период или достигнут потолок)."""
        st = self.ctx.subs.view(user)
        if st.tier is None or user.trial_limited or self.ctx.cfg.device_price() <= 0:
            return 0
        return max(0, int(self.ctx.cfg.get("max_extra_devices") or 0) - int(user.extra_devices or 0))

    # ------------------------------------------------------------------ создание счёта
    async def _issue(
        self,
        *,
        user: User,
        provider: str,
        plan_code: str,
        kind: str,
        period_days: int,
        amount: Decimal,
        base: Decimal,
        discount: Decimal,
        promo_id: int | None,
        description: str,
        meta: dict,
        receipts: bool,
        save_method: bool,
    ) -> Payment:
        """Создаёт строку платежа, затем счёт у провайдера (вне транзакции БД) и сохраняет ссылку."""
        prov = self.providers[provider]
        now = utcnow()
        async with self.ctx.session() as s:
            candidates = (
                await s.scalars(
                    select(Payment)
                    .where(
                        Payment.user_id == user.id,
                        Payment.status == PENDING,
                        Payment.provider == provider,
                        Payment.plan_code == plan_code,
                        Payment.kind == kind,
                        Payment.amount == amount,
                        Payment.period_days == period_days,
                        Payment.expires_at > now,
                        Payment.confirmation_url.is_not(None),
                    )
                    .order_by(desc(Payment.id))
                    .limit(5)
                )
            ).all()
            for c in candidates:
                m = c.meta or {}
                if m.get("sig") == meta.get("sig") and not m.get("autopay"):
                    return c  # тот же счёт уже выставлен и ещё действует
            pay = Payment(
                user_id=user.id,
                provider=provider,
                plan_code=plan_code,
                kind=kind,
                period_days=period_days,
                amount=amount,
                base_amount=base,
                discount=discount,
                status=PENDING,
                promo_id=promo_id,
                description=description,
                receipt_email=user.email if receipts else None,
                created_at=now,
                expires_at=now + timedelta(minutes=55),
                meta=meta,
            )
            s.add(pay)
            await s.flush()
            pay_id = pay.id
            await s.commit()

        try:
            inv = await prov.create(pay, user, save_method=save_method)
        except ProviderError as e:
            log.error("%s: не удалось создать платёж #%s: %s", provider, pay_id, e)
            metrics.inc("payments_total", provider=provider, status="create_failed")
            # пользователю ссылку не выдавали, оплатить он не мог — спокойно отменяем (даже при таймауте)
            await self._set_status(pay_id, CANCELED, cancel_reason="create_failed")
            raise PaymentError("provider", str(e)) from e

        async with self.ctx.session() as s:
            pay = await s.get(Payment, pay_id)
            assert pay is not None
            pay.external_id = inv.external_id
            pay.confirmation_url = inv.pay_url
            await s.commit()
        metrics.inc("payments_total", provider=provider, status="created")
        return pay

    async def create(
        self,
        user_id: int,
        tier: Tier,
        *,
        months: int = 1,
        upgrade: bool = False,
        provider: str = "yookassa",
        save_method: bool = False,
        autopay_consent: bool = False,
    ) -> Payment:
        if months not in TERMS:
            raise PaymentError("bad_term", str(months))
        prov = self.providers.get(provider)
        if prov is None or not prov.available():
            raise PaymentError("no_provider")
        plan = self.ctx.plans.get(tier)
        if not self.ctx.plans.is_sellable(plan):
            raise PaymentError("inactive_plan")
        save_method = bool(save_method and provider == "yookassa" and self.autopay_possible())
        if save_method and not autopay_consent:
            raise PaymentError("need_autopay_consent")  # рекуррентные списания — только с явного согласия

        async with self.ctx.subs.lock(user_id):
            user = await self.ctx.users.get(user_id)
            if user is None:
                raise LookupError(user_id)
            if user.consent_at is None:
                raise PaymentError("need_consent")  # нет акцепта оферты и политики
            receipts = bool(self.ctx.cfg.get("receipts_enabled")) and provider == "yookassa"
            if receipts and not user.email:
                raise NeedEmail()

            q, promo = await self.quote(user, tier, months=months, upgrade=upgrade)
            title = f"{BRAND} — {plan.title}"
            if q.kind == "upgrade":
                title = f"{BRAND} — переход на {plan.title}"
            description = f"{title}, {months_word(months)}"
            if q.extra_devices:
                description += f" + {devices_word(q.extra_devices)}"
            meta: dict[str, Any] = {
                "upgrade_extra": str(q.extra),
                "save": save_method,
                "months": months,
                "extra_devices": q.extra_devices,
                "sig": f"{q.kind}:{months}:{q.extra_devices}:{int(save_method)}",
            }
            if save_method:
                meta["autopay_consent_at"] = iso_z(utcnow())
                meta["autopay_consent_version"] = CONSENT_VERSION
            return await self._issue(
                user=user,
                provider=provider,
                plan_code=tier.value,
                kind=q.kind,
                period_days=q.period_days,
                amount=q.amount,
                base=q.base,
                discount=q.discount,
                promo_id=promo.id if promo else None,
                description=description,
                meta=meta,
                receipts=receipts,
                save_method=save_method,
            )

    async def create_devices(self, user_id: int, count: int, *, provider: str = "yookassa") -> Payment:
        """Счёт на дополнительные устройства: доплата пропорциональна оставшемуся сроку подписки."""
        prov = self.providers.get(provider)
        if prov is None or not prov.available():
            raise PaymentError("no_provider")
        async with self.ctx.subs.lock(user_id):
            user = await self.ctx.users.get(user_id)
            if user is None:
                raise LookupError(user_id)
            if user.consent_at is None:
                raise PaymentError("need_consent")
            room = self.devices_can_buy(user)
            if room <= 0:
                raise PaymentError("devices_unavailable")
            if count < 1 or count > room:
                raise PaymentError("devices_limit", str(room))
            dq = self.devices_quote(user, count)
            st = self.ctx.subs.view(user)
            if dq is None or st.tier is None:
                raise PaymentError("devices_unavailable")
            receipts = bool(self.ctx.cfg.get("receipts_enabled")) and provider == "yookassa"
            if receipts and not user.email:
                raise NeedEmail()
            description = f"{BRAND} — {devices_word(count)} до {fmt_dt(st.total_end)}"
            meta = {"devices": count, "days": dq.days, "months": 0, "sig": f"devices:{count}:{dq.amount}"}
            return await self._issue(
                user=user,
                provider=provider,
                plan_code=st.tier.value,
                kind="devices",
                period_days=0,
                amount=dq.amount,
                base=dq.amount,
                discount=Decimal("0"),
                promo_id=None,
                description=description,
                meta=meta,
                receipts=receipts,
                save_method=False,
            )

    async def _set_status(self, pay_id: int, status: str, **fields) -> None:
        async with self.ctx.session() as s:
            pay = await s.get(Payment, pay_id)
            if pay:
                pay.status = status
                for k, v in fields.items():
                    setattr(pay, k, v)
                await s.commit()

    # ------------------------------------------------------------------ чтение
    async def get(self, pay_id: int) -> Payment | None:
        async with self.ctx.session() as s:
            return await s.get(Payment, pay_id)

    async def find(self, provider: str, external_id: str) -> int | None:
        async with self.ctx.session() as s:
            return await s.scalar(
                select(Payment.id).where(Payment.provider == provider, Payment.external_id == external_id)
            )

    async def attach_external(self, provider: str, pay_id: int, external_id: str, *, user_id: int | None = None) -> int | None:
        """Привязывает id платежа провайдера к нашей строке (если мы его не успели сохранить). Возвращает id платежа или None."""
        try:
            async with self.ctx.session() as s:
                pay = await s.scalar(select(Payment).where(Payment.id == pay_id).with_for_update())
                if pay is None or pay.provider != provider:
                    return None
                if user_id is not None and pay.user_id != user_id:
                    log.warning("metadata платежа #%s не совпала по user_id (%s ≠ %s) — игнорирую", pay_id, user_id, pay.user_id)
                    return None
                if pay.external_id and pay.external_id != external_id:
                    log.warning("платёж #%s уже привязан к %s, а пришёл %s — игнорирую", pay_id, pay.external_id, external_id)
                    return None
                if not pay.external_id:
                    pay.external_id = external_id
                    await s.commit()
                    metrics.inc("payments_total", provider=provider, status="external_id_attached")
                    log.info("платёж #%s: external_id %s восстановлен по metadata", pay_id, external_id)
                return pay.id
        except IntegrityError:
            log.warning("external_id %s уже занят другим платежом", external_id)
            return None

    async def page(
        self, status: str | None, page: int, size: int = 8, user_id: int | None = None, provider: str | None = None
    ) -> tuple[list[Payment], int]:
        conds = []
        if status:
            conds.append(Payment.status == status)
        if user_id is not None:
            conds.append(Payment.user_id == user_id)
        if provider:
            conds.append(Payment.provider == provider)
        async with self.ctx.session() as s:
            total = int(await s.scalar(select(func.count()).select_from(Payment).where(*conds)) or 0)
            res = await s.execute(
                select(Payment).where(*conds).order_by(desc(Payment.id)).limit(size).offset(page * size)
            )
            return list(res.scalars().all()), total

    # ------------------------------------------------------------------ обработка статуса
    async def check(self, pay_id: int, user_id: int) -> Payment | None:
        """Кнопка «Проверить оплату»."""
        pay = await self.get(pay_id)
        if pay is None or pay.user_id != user_id:
            return None
        if pay.status == PENDING:
            await self.settle(pay_id)
            pay = await self.get(pay_id)
        return pay

    async def settle(self, pay_id: int) -> Payment | None:
        """Запрашивает статус у провайдера и приводит наше состояние в соответствие.

        Обрабатываются ожидающие платежи и ошибочно отменённые (если провайдер говорит «оплачено» — начисляем).
        Платежи на ручной проверке (needs_review) автоматически не трогаем.
        """
        pay = await self.get(pay_id)
        if pay is None or pay.status not in (PENDING, CANCELED) or not pay.external_id:
            return pay
        prov = self.providers.get(pay.provider)
        if prov is None or not prov.ready():
            return pay
        try:
            status = await prov.status(pay)
        except ProviderError as e:
            log.warning("статус платежа #%s не получен: %s", pay_id, e)
            return pay
        if pay.status == CANCELED and status.state != "paid":
            return pay
        return await self._resolve(pay_id, status)

    async def _resolve(self, pay_id: int, status: Status) -> Payment | None:
        if status.state == "paid":
            return await self._apply_success(pay_id, status)
        if status.state == "failed":
            return await self._apply_canceled(pay_id, status.reason or "canceled")
        return await self.get(pay_id)

    async def _to_review(self, pay_id: int, reason: str, **extra: Any) -> Payment | None:
        """Переводит платёж на ручную проверку и один раз предупреждает владельцев."""
        async with self.ctx.session() as s:
            pay = await s.scalar(select(Payment).where(Payment.id == pay_id).with_for_update())
            if pay is None or pay.status in (NEEDS_REVIEW, *FINAL_STATUSES):
                return pay
            pay.status = NEEDS_REVIEW
            pay.meta = {**(pay.meta or {}), "review_reason": reason, **extra}
            await s.commit()
        metrics.inc("payments_total", provider=pay.provider, status="needs_review")
        await self.ctx.notify.to_admins(
            f"⚠️ <b>Платёж #{pay.id}</b> ({pay.provider}, {money(pay.amount)}) требует проверки: <code>{esc(reason)}</code>.\n"
            + ("; ".join(f"{esc(str(k))}={esc(str(v))}" for k, v in extra.items()) + "\n" if extra else "")
            + "Подписка не начислена. Админка → Платежи → «Требуют проверки»."
        )
        return pay

    async def _apply_success(self, pay_id: int, status: Status, *, force: bool = False) -> Payment | None:
        pay0 = await self.get(pay_id)
        if pay0 is None:
            return None
        if pay0.applied or pay0.status in FINAL_STATUSES:
            return pay0
        user_id = pay0.user_id
        if user_id is None:  # пользователь удалён — фиксируем платёж для учёта
            await self._set_status(pay_id, SUCCEEDED, paid_at=utcnow(), applied=True)
            return await self.get(pay_id)

        subs = self.ctx.subs
        pm = (status.meta or {}).get("payment_method") or {}
        extra_meta = {k: v for k, v in (status.meta or {}).items() if k != "payment_method" and v is not None}
        saved_method = False
        review: tuple[str, dict] | None = None
        recovered = False
        with log_user(user_id):
            async with subs.lock(user_id):
                async with self.ctx.session() as s:
                    pay = await s.scalar(select(Payment).where(Payment.id == pay_id).with_for_update())
                    assert pay is not None
                    if pay.applied or pay.status in FINAL_STATUSES:
                        return pay
                    action = "apply" if force else settle_action(
                        status=pay.status, applied=pay.applied, provider_state="paid",
                        provider_amount=status.amount, expected=pay.amount,
                    )
                    if action == "noop":
                        return pay
                    if action == "review":
                        log.error("Сумма платежа #%s не совпала: ждали %s, пришло %s", pay.id, pay.amount, status.amount)
                        review = ("amount_mismatch", {"ожидали": str(pay.amount), "пришло": str(status.amount)})
                    else:
                        recovered = pay.status == CANCELED
                        user = await s.get(User, user_id, with_for_update=True)
                        assert user is not None
                        meta = pay.meta or {}
                        months = int(meta.get("months") or 1)
                        now = utcnow()
                        if pay.kind == "devices":
                            count = int(meta.get("devices") or 0)
                            if count <= 0 or not normalize(subs.state_of(user), now).is_active(now):
                                # деньги пришли, а подписка уже закончилась — расширять нечего
                                review = ("devices_without_subscription", {"устройств": count})
                            else:
                                subs.add_extra_devices(
                                    s, user, count, kind="devices", amount=pay.amount,
                                    meta={"payment_id": pay.id, "provider": pay.provider},
                                )
                                new = subs.view(user, now)
                        else:
                            tier = Tier(pay.plan_code)
                            period = timedelta(days=pay.period_days)
                            upgrade = pay.kind == "upgrade"
                            _old, new = subs.apply_op(
                                s,
                                user,
                                lambda st, now_, tier=tier, period=period, upgrade=upgrade: core.purchase(st, tier, period, now_, upgrade=upgrade),
                                kind=pay.kind,
                                tier=tier,
                                seconds=pay.period_days * 86400,
                                amount=pay.amount,
                                meta={"payment_id": pay.id, "provider": pay.provider, "months": months},
                            )
                            # новый оплаченный период — цикл автопродления начинается заново
                            user.autopay_failures = 0
                            user.autopay_cycle_end = None
                            user.autopay_next_at = None
                        if review is None:
                            pay.status = SUCCEEDED
                            pay.paid_at = now
                            pay.applied = True
                            pay.meta = {**meta, **extra_meta}
                            if user.first_paid_at is None:
                                user.first_paid_at = now
                            if pay.promo_id:
                                use = await s.scalar(
                                    select(PromoUse).where(PromoUse.promo_id == pay.promo_id, PromoUse.user_id == user_id)
                                )
                                if use is not None:
                                    use.status = "used"
                                    use.payment_id = pay.id
                                if user.pending_promo_id == pay.promo_id:
                                    user.pending_promo_id = None
                            # первый платёж с согласием на автопродление -> запоминаем способ оплаты
                            if meta.get("save") and pm.get("saved") and pm.get("id"):
                                existing = await s.scalar(select(PaymentMethod).where(PaymentMethod.method_id == str(pm["id"])))
                                title = str(pm.get("title") or (pm.get("card") or {}).get("last4") or "Карта")[:90]
                                if existing is None:
                                    s.add(PaymentMethod(user_id=user_id, provider="yookassa", method_id=str(pm["id"]), title=title, is_active=True))
                                else:
                                    existing.user_id, existing.is_active, existing.title = user_id, True, title
                                user.autopay = True
                                user.autopay_months = months if months in TERMS else 1
                                saved_method = True
                    if review is None:
                        await s.commit()
                    else:
                        await s.rollback()   # ничего не начисляем, статус выставит _to_review
                if review is not None:
                    pass
                else:
                    synced = await subs.sync_inside_lock(user_id)

        if review is not None:
            await self._to_review(pay_id, review[0], **review[1])
            return await self.get(pay_id)

        metrics.inc("payments_total", provider=pay.provider, status="succeeded")
        if recovered:
            await self.ctx.notify.to_admins(
                f"♻️ Платёж #{pay.id} был отменён у нас, но оплачен у провайдера — подписка начислена."
            )
        await self._after_success(pay, user, new, saved_method, synced)
        return pay

    async def _after_success(self, pay: Payment, user: User, new: SubState, saved_method: bool, synced: bool) -> None:
        texts = self.ctx.texts
        tier = Tier(pay.plan_code)
        if pay.kind == "devices":
            limit = self.ctx.subs.device_limit_for(user, new)
            text = texts.render("pay_success_devices", count=(pay.meta or {}).get("devices", 0), limit=limit, until=fmt_dt(new.total_end))
        else:
            notes: list[str] = []
            if pay.kind == "upgrade":
                notes.append(texts.render("pay_note_upgrade"))
            elif tier == Tier.LITE and new.tier == Tier.PREMIUM and new.queued_lite > core.ZERO:
                notes.append(texts.render("pay_note_queue", until=fmt_dt(new.expires_at)))
            if (pay.meta or {}).get("autopay"):
                notes.append(texts.render("pay_note_auto"))
            if saved_method:
                notes.append(texts.render("pay_note_autopay_on"))
            if not synced:
                notes.append(texts.render("pay_note_setup"))
            extra = "".join(f"\n\n{n}" for n in notes)
            text = texts.render("pay_success", plan=tier.title, until=fmt_dt(new.total_end), extra=extra)
        await self.ctx.notify.send(
            user.id, text, kb([btn(labels.SETUP, "setup:open"), btn(labels.SUBSCRIPTION, "menu:sub")]), critical=True
        )

        if self.ctx.cfg.get("notify_sales"):
            who = f"@{user.username}" if user.username else esc(user.display_name)
            prov = self.providers.get(pay.provider)
            emoji = prov.emoji if prov else "💰"
            asset = f" · {esc((pay.meta or {}).get('asset'))}" if (pay.meta or {}).get("asset") else ""
            what = f"{tier.title}"
            if pay.kind == "upgrade":
                what += " (апгрейд)"
            elif pay.kind == "devices":
                what = f"+{devices_word(int((pay.meta or {}).get('devices', 0)))}"
            months = int((pay.meta or {}).get("months") or 0)
            term = f" · {months_word(months)}" if months else ""
            await self.ctx.notify.to_admins(
                f"{emoji} <b>Оплата {money(pay.amount)}</b> · {what}{term}"
                f"{' · авто' if (pay.meta or {}).get('autopay') else ''}{asset}"
                f"\n{who} · <code>{user.id}</code>"
            )

    async def _apply_canceled(self, pay_id: int, reason: str) -> Payment | None:
        async with self.ctx.session() as s:
            pay = await s.scalar(select(Payment).where(Payment.id == pay_id).with_for_update())
            if pay is None or pay.status != PENDING:
                return pay
            pay.status = CANCELED
            pay.cancel_reason = str(reason)[:128]
            await s.commit()
        metrics.inc("payments_total", provider=pay.provider, status="canceled")
        if pay.user_id:
            if (pay.meta or {}).get("autopay"):
                await self._autopay_failed(pay.user_id, reason)
            elif reason not in ("expired_on_confirmation", "expired"):
                await self.ctx.notify.send(
                    pay.user_id, self.ctx.texts.render("pay_canceled"), kb(btn(labels.BUY, "pay:menu"))
                )
        return pay

    # ------------------------------------------------------------------ вебхуки
    async def handle_yookassa_event(self, payload: dict) -> None:
        refs = extract_yookassa_refs(payload)
        if refs.event.startswith("payment.") and refs.external_id:
            pid = await self.find("yookassa", refs.external_id)
            if pid is None and refs.payment_id is not None:
                # id платежа мы не успели сохранить (таймаут/сбой между шагами) — ищем по metadata.payment_id
                pid = await self.attach_external("yookassa", refs.payment_id, refs.external_id, user_id=refs.user_id)
            if pid:
                await self.settle(pid)
            else:
                metrics.inc("payments_total", provider="yookassa", status="webhook_unknown")
                log.warning("Уведомление по неизвестному платежу ЮKassa %s (metadata.payment_id=%s)", refs.external_id, refs.payment_id)
        elif refs.event == "refund.succeeded" and refs.refund_of:
            await self._on_external_refund(refs)

    async def handle_cryptobot_update(self, payload: dict) -> None:
        refs = extract_cryptobot_refs(payload)
        if refs.event != "invoice_paid" or not refs.external_id:
            return
        pid = await self.find("cryptobot", refs.external_id)
        if pid is None and refs.payment_id is not None:
            pid = await self.attach_external("cryptobot", refs.payment_id, refs.external_id)
        if pid:
            await self.settle(pid)  # статус перепроверяется через getInvoices
        else:
            log.warning("Уведомление по неизвестному счёту CryptoBot %s", refs.external_id)

    async def _on_external_refund(self, refs: Refs) -> None:
        async with self.ctx.session() as s:
            pay = await s.scalar(
                select(Payment).where(Payment.provider == "yookassa", Payment.external_id == refs.refund_of).with_for_update()
            )
            if pay is None or pay.status in (REFUNDED, REFUNDING):
                return  # свой возврат (REFUNDING) доведёт refund() — второй раз не снимаем и не алертим
            pay.status = REFUNDED
            pay.refunded_at = utcnow()
            pay.refund_id = refs.external_id[:64]
            await s.commit()
        await self.ctx.notify.to_admins(
            f"↩️ По платежу #{pay.id} ({money(pay.amount)}) оформлен возврат в ЮKassa. "
            "Дни подписки автоматически не снимались — при необходимости снимите их в карточке пользователя."
        )

    # ------------------------------------------------------------------ ручная проверка спорных платежей
    async def approve_review(self, pay_id: int, actor_id: int) -> Payment | None:
        """Владелец подтверждает платёж из needs_review: начисляем то, что было куплено."""
        pay = await self.get(pay_id)
        if pay is None or pay.status != NEEDS_REVIEW:
            raise PaymentError("not_refundable", "платёж не на проверке")
        async with self.ctx.session() as s:  # снимаем статус, чтобы _apply_success принял платёж
            row = await s.scalar(select(Payment).where(Payment.id == pay_id).with_for_update())
            if row is None or row.status != NEEDS_REVIEW:
                raise PaymentError("not_refundable", "платёж уже обработан")
            row.status = PENDING
            row.meta = {**(row.meta or {}), "approved_by": actor_id, "approved_at": iso_z(utcnow())}
            await s.commit()
        res = await self._apply_success(pay_id, Status("paid", amount=pay.amount), force=True)
        await self.ctx.audit.log(actor_id, "payment.approve", pay_id, amount=str(pay.amount))
        return res

    async def reject_review(self, pay_id: int, actor_id: int) -> Payment | None:
        async with self.ctx.session() as s:
            row = await s.scalar(select(Payment).where(Payment.id == pay_id).with_for_update())
            if row is None or row.status != NEEDS_REVIEW:
                raise PaymentError("not_refundable", "платёж не на проверке")
            row.status = CANCELED
            row.cancel_reason = "review_rejected"
            row.meta = {**(row.meta or {}), "rejected_by": actor_id}
            await s.commit()
        await self.ctx.audit.log(actor_id, "payment.reject", pay_id)
        return await self.get(pay_id)

    # ------------------------------------------------------------------ возврат
    async def refund(self, pay_id: int, actor_id: int, *, remove_days: bool) -> Payment:
        """Возврат: платёж «захватывается» (succeeded → refunding) под блокировкой строки — два админа не смогут
        запустить возврат и снять дни дважды."""
        async with self.ctx.session() as s:
            pay = await s.scalar(select(Payment).where(Payment.id == pay_id).with_for_update())
            if pay is None or pay.status != SUCCEEDED or not pay.external_id:
                raise PaymentError("not_refundable")
            if pay.provider != "yookassa" or self.ctx.yk is None:
                raise PaymentError(
                    "not_refundable",
                    "Возврат через бота есть только для ЮKassa. Крипто-платёж верните вручную в CryptoBot, "
                    "а дни снимите в карточке пользователя.",
                )
            pay.status = REFUNDING
            pay.meta = {**(pay.meta or {}), "refund_started": iso_z(utcnow()), "refund_actor": actor_id, "remove_days": bool(remove_days)}
            await s.commit()
        try:
            res = await self.ctx.yk.create_refund(
                payment_id=pay.external_id,
                amount=pay.amount,
                idempotence_key=f"ampire-refund-{pay.id}",
                description=f"Возврат платежа #{pay.id}",
            )
        except YooKassaError as e:
            if not e.transient:
                await self._set_status(pay_id, SUCCEEDED)  # ЮKassa однозначно отказала — возвращаем как было
            # при временной ошибке исход неизвестен: платёж остаётся в REFUNDING, его доведёт finalize_refunds
            raise PaymentError("provider", str(e)) from e
        if res.get("status") == "canceled":
            await self._set_status(pay_id, SUCCEEDED)
            raise PaymentError("provider", "возврат отклонён ЮKassa")
        await self._finish_refund(pay_id, str(res.get("id", "")), actor_id, remove_days)
        await self.ctx.audit.log(actor_id, "payment.refund", pay_id, remove_days=remove_days, amount=str(pay.amount))
        return await self.get(pay_id)  # type: ignore[return-value]

    async def _finish_refund(self, pay_id: int, refund_id: str, actor_id: int | None, remove_days: bool) -> bool:
        """Завершает возврат ровно один раз: только тот, кто перевёл refunding → refunded, снимает дни."""
        async with self.ctx.session() as s:
            res = await s.execute(
                update(Payment)
                .where(Payment.id == pay_id, Payment.status == REFUNDING)
                .values(status=REFUNDED, refunded_at=utcnow(), refund_id=refund_id[:64])
                .returning(Payment.id)
            )
            won = res.first() is not None
            await s.commit()
        if not won:
            return False
        pay = await self.get(pay_id)
        if remove_days and pay and pay.user_id:
            if pay.kind == "devices":
                count = int((pay.meta or {}).get("devices") or 0)
                if count:
                    await self.ctx.subs.change_extra_devices(pay.user_id, -count, kind="refund", actor_id=actor_id)
            else:
                await self.ctx.subs.remove_days(pay.user_id, Tier(pay.plan_code), pay.period_days, actor_id=actor_id, kind="refund")
        return True

    async def finalize_refunds(self) -> int:
        """Воркер: добивает «зависшие» возвраты (процесс упал между запросом и записью результата)."""
        if self.ctx.yk is None:
            return 0
        cutoff = utcnow() - REFUND_STUCK_AFTER
        async with self.ctx.session() as s:
            rows = list((await s.scalars(select(Payment).where(Payment.status == REFUNDING).limit(50))).all())
        done = 0
        for p in rows:
            started = (p.meta or {}).get("refund_started")
            if started and datetime.fromisoformat(str(started).replace("Z", "+00:00")) > cutoff:
                continue
            try:
                obj = await self.ctx.yk.get_payment(p.external_id or "")
            except YooKassaError:
                continue
            refunded = Decimal(str((obj.get("refunded_amount") or {}).get("value") or "0"))
            if refunded >= Decimal(p.amount):
                if await self._finish_refund(p.id, "", (p.meta or {}).get("refund_actor"), bool((p.meta or {}).get("remove_days"))):
                    done += 1
            else:
                await self._set_status(p.id, SUCCEEDED)   # возврат не состоялся — снова доступен
                done += 1
        return done

    # ------------------------------------------------------------------ воркер: сверка статусов
    async def reconcile_pending(self, batch: int = 300, budget: float = 45.0) -> int:
        """Добирает статусы платежей, по которым не пришло уведомление (постранично, по провайдерам).

        Платежи needs_review здесь не участвуют — поэтому спорный платёж не порождает бесконечных проверок
        и не занимает место в пачке.
        """
        t0 = time.monotonic()
        done = 0
        last_id = 0
        await self.finalize_refunds()
        while time.monotonic() - t0 < budget:
            now = utcnow()
            async with self.ctx.session() as s:
                rows = list(
                    (
                        await s.scalars(
                            select(Payment)
                            .where(Payment.status == PENDING, Payment.created_at < now - timedelta(seconds=75), Payment.id > last_id)
                            .order_by(Payment.id)
                            .limit(batch)
                        )
                    ).all()
                )
            if not rows:
                break
            last_id = rows[-1].id
            done += await self._reconcile_rows(rows, now)
            if len(rows) < batch:
                break
        return done

    async def _reconcile_rows(self, rows: list[Payment], now: datetime) -> int:
        by_provider: dict[str, list[Payment]] = {}
        done = 0
        for p in rows:
            if p.external_id:
                by_provider.setdefault(p.provider, []).append(p)
                continue
            # id платежа у провайдера не сохранён
            age = now - p.created_at
            if (p.meta or {}).get("autopay"):
                if age <= AUTOPAY_KEY_LIFETIME:
                    # деньги могли уйти — НЕ отменяем: повторяем запрос с тем же ключом идемпотентности
                    if await self._retry_autopay_create(p):
                        done += 1
                else:
                    await self._to_review(p.id, "autopay_no_external_id", возраст=str(age).split(".")[0])
            elif age > NO_EXTERNAL_ID_CANCEL_AFTER:
                await self._set_status(p.id, CANCELED, cancel_reason="no_external_id")

        for code, pays in by_provider.items():
            prov = self.providers.get(code)
            if prov is None or not prov.ready():
                continue
            try:
                statuses = await prov.statuses(pays)
            except ProviderError as e:
                log.warning("reconcile %s: %s", code, e)
                continue
            for p in pays:
                st = statuses.get(p.id)
                if st is None:
                    continue
                if st.state == "pending" and is_stale(p.created_at, now):
                    st = Status("failed", reason="expired")
                try:
                    res = await self._resolve(p.id, st)
                except Exception:  # noqa: BLE001
                    log.exception("reconcile #%s", p.id)
                    continue
                if res is not None and res.status != PENDING:
                    done += 1
        return done

    # ------------------------------------------------------------------ ежедневная сверка «ЮKassa → БД»
    async def reconcile_yookassa(self, hours: int = 48) -> dict[str, int]:
        """Берёт у ЮKassa ВСЕ успешные платежи за период и проверяет, что каждый учтён у нас.

        Ловит то, что не видно сверкой «БД → ЮKassa»: платёж есть у ЮKassa, а в БД его нет или он не начислен
        (потерянный вебхук + потерянный external_id, откат БД из бэкапа и т. п.).
        """
        yk = self.ctx.yk
        if yk is None:
            return {}
        now = utcnow()
        since, until = now - timedelta(hours=hours), now - timedelta(minutes=5)
        stats = {"seen": 0, "ok": 0, "fixed": 0, "missing": 0, "review": 0}
        missing: list[str] = []
        cursor: str | None = None
        pages = 0
        while pages < 500:
            page = await yk.list_payments(created_from=iso_z(since), created_to=iso_z(until), status="succeeded", cursor=cursor)
            pages += 1
            for obj in page.get("items") or []:
                stats["seen"] += 1
                st = map_yookassa(obj)
                if st.state != "paid":
                    continue
                ext = str(obj.get("id") or "")
                meta = obj.get("metadata") or {}
                pid = await self.find("yookassa", ext)
                if pid is None and str(meta.get("payment_id", "")).isdigit():
                    pid = await self.attach_external("yookassa", int(meta["payment_id"]), ext, user_id=int(meta["user_id"]) if str(meta.get("user_id", "")).isdigit() else None)
                if pid is None:
                    created = await self._recover_missing(obj)
                    stats["review" if created else "missing"] += 1
                    missing.append(f"{ext} {st.amount} ₽")
                    continue
                pay = await self.get(pid)
                if pay is None or pay.applied or pay.status in (REFUNDED, REFUNDING, NEEDS_REVIEW):
                    stats["ok"] += 1
                    continue
                await self._resolve(pid, st)
                fresh = await self.get(pid)
                if fresh is not None and fresh.applied:
                    stats["fixed"] += 1
                    log.warning("сверка ЮKassa: платёж #%s был не начислен — исправлено", pid)
                else:
                    stats["review"] += 1
            cursor = page.get("next_cursor")
            if not cursor:
                break
        metrics.inc("yookassa_recon_runs_total")
        metrics.set_gauge("yookassa_recon_last_run_timestamp", time.time())
        if stats["fixed"] or stats["missing"] or stats["review"]:
            await self.ctx.notify.to_admins(
                f"🔎 <b>Сверка ЮKassa → БД</b> за {hours} ч: платежей {stats['seen']}, "
                f"исправлено {stats['fixed']}, на проверке {stats['review']}, неизвестных {stats['missing']}."
                + (("\n" + "\n".join(f"• <code>{esc(m)}</code>" for m in missing[:10])) if missing else "")
            )
        return stats

    async def _recover_missing(self, obj: dict) -> bool:
        """Платежа нет в БД вообще. Если в metadata есть всё нужное — заводим строку на ручную проверку."""
        meta = obj.get("metadata") or {}
        if not (str(meta.get("user_id", "")).isdigit() and meta.get("plan") in ("lite", "premium")):
            return False
        user = await self.ctx.users.get(int(meta["user_id"]))
        if user is None:
            return False
        kind = str(meta.get("kind") or "buy")
        months = int(meta.get("months") or 1)
        try:
            amount = Decimal(str((obj.get("amount") or {}).get("value")))
        except Exception:  # noqa: BLE001
            return False
        plan = self.ctx.plans.get(meta["plan"])
        days = 0 if kind == "devices" else term_days(months if months in TERMS else 1, plan.period_days)
        now = utcnow()
        try:
            async with self.ctx.session() as s:
                s.add(
                    Payment(
                        user_id=user.id, provider="yookassa", external_id=str(obj["id"]), plan_code=str(meta["plan"]), kind=kind,
                        period_days=days, amount=amount, base_amount=amount, discount=Decimal("0"), status=NEEDS_REVIEW,
                        description=str(obj.get("description") or "")[:200], created_at=now, expires_at=now,
                        meta={"recovered": True, "months": months, "devices": int(meta.get("devices") or 0),
                              "review_reason": "recovered_from_yookassa"},
                    )
                )
                await s.commit()
        except IntegrityError:
            return False
        return True

    # ------------------------------------------------------------------ автопродление
    async def latest_method(self, user_id: int) -> PaymentMethod | None:
        async with self.ctx.session() as s:
            return await s.scalar(
                select(PaymentMethod)
                .where(PaymentMethod.user_id == user_id, PaymentMethod.is_active.is_(True))
                .order_by(desc(PaymentMethod.id))
                .limit(1)
            )

    async def set_autopay(self, user_id: int, on: bool) -> bool:
        """Включить/выключить автопродление. Включить можно только при сохранённом способе оплаты."""
        if on and await self.latest_method(user_id) is None:
            return False
        async with self.ctx.session() as s:
            await s.execute(
                update(User).where(User.id == user_id).values(autopay=on, autopay_failures=0, autopay_next_at=None, autopay_cycle_end=None)
            )
            await s.commit()
        return True

    async def unlink_methods(self, user_id: int) -> None:
        async with self.ctx.session() as s:
            await s.execute(update(PaymentMethod).where(PaymentMethod.user_id == user_id).values(is_active=False))
            await s.execute(
                update(User).where(User.id == user_id).values(autopay=False, autopay_next_at=None, autopay_cycle_end=None)
            )
            await s.commit()

    @staticmethod
    def _ends():
        """Конец подписки с учётом очереди LITE: expires_at + queued_lite_s."""
        return User.expires_at + func.make_interval(text("0"), text("0"), text("0"), text("0"), text("0"), text("0"), User.queued_lite_s)

    async def _autopay_ids(self, phase: str, now: datetime, after_id: int, limit: int) -> list[int]:
        ends = self._ends()
        base = [User.autopay.is_(True), User.is_banned.is_(False), User.id > after_id]
        if phase == "charge":
            first = and_(
                User.autopay_next_at.is_(None),
                User.autopay_failures == 0,
                ends <= now + CHARGE_BEFORE,
                ends > now - MISSED_CYCLE_WINDOW,
            )
            retry = and_(User.autopay_next_at.is_not(None), User.autopay_next_at <= now)
            cond = or_(first, retry)
        else:
            cond = and_(
                User.autopay_failures == 0,
                User.tier.is_not(None),
                ends <= now + NOTICE_BEFORE,
                ends > now + CHARGE_BEFORE,
                User.autopay_notified_for.is_distinct_from(ends),
            )
        async with self.ctx.session() as s:
            return list((await s.scalars(select(User.id).where(*base, cond).order_by(User.id).limit(limit))).all())

    async def autopay_run(self, budget: float = 240.0, batch: int = 200, concurrency: int = 8) -> int:
        """Воркер: списания и предупреждения. Без жёсткого потолка «300 за прогон»: идём постранично, пока есть кого
        обрабатывать и не вышел бюджет времени. Сначала деньги (списания), потом уведомления."""
        if not self.autopay_possible():
            return 0
        t0 = time.monotonic()
        sem = asyncio.Semaphore(concurrency)
        total = 0

        async def one(uid: int, phase: str) -> bool:
            async with sem:
                with log_user(uid):
                    try:
                        return await self._autopay_one(uid, phase)
                    except Exception:  # noqa: BLE001
                        log.exception("autopay %s", uid)
                        return False

        for phase in ("charge", "notice"):
            last_id = 0
            while time.monotonic() - t0 < budget:
                ids = await self._autopay_ids(phase, utcnow(), last_id, batch)
                if not ids:
                    break
                last_id = ids[-1]
                total += sum(await asyncio.gather(*(one(u, phase) for u in ids)))
                if len(ids) < batch:
                    break
        metrics.set_gauge("autopay_last_run_timestamp", time.time())
        return total

    async def _autopay_one(self, user_id: int, want: str) -> bool:
        user = await self.ctx.users.get(user_id)
        if user is None or not user.autopay or user.is_banned:
            return False
        now = utcnow()
        st = self.ctx.subs.view(user, now)
        failures = int(user.autopay_failures or 0)
        phase = autopay_phase(
            st, now, user.autopay_notified_for, user.autopay_last_at, next_at=user.autopay_next_at, failures=failures
        )
        if (
            phase is None
            and want == "charge"
            and failures == 0
            and not st.is_active(now)
            and user.expires_at is not None
            and now - user.expires_at < MISSED_CYCLE_WINDOW
            and (user.autopay_last_at is None or now - user.autopay_last_at > timedelta(hours=1))
        ):
            phase = "charge"  # воркер простаивал, подписка истекла до списания — списываем сейчас
        if phase != want:
            return False
        if phase == "notice":
            await self._autopay_notice(user, st)
        else:
            await self.autopay_charge(user_id)
        return True

    def _autopay_amounts(self, user: User, tier: Tier, months: int, st: SubState, now: datetime) -> tuple[Decimal, Decimal, Decimal]:
        plan = self.ctx.plans.get(tier)
        extras = int(user.extra_devices or 0) if st.is_active(now) else 0
        disc = self.ctx.cfg.term_discounts()[months]
        return renewal_amount(plan.price, months, disc, extras, self.ctx.cfg.device_price())

    @staticmethod
    def _autopay_months(user: User) -> int:
        m = int(user.autopay_months or 1)
        return m if m in TERMS else 1

    async def _autopay_notice(self, user: User, st: SubState) -> None:
        tier = renewal_tier(st)
        method = await self.latest_method(user.id)
        if tier is None or method is None:
            return
        months = self._autopay_months(user)
        _plan_part, _dev, amount = self._autopay_amounts(user, tier, months, st, utcnow())
        text = self.ctx.texts.render(
            "autopay_notice",
            tier=tier.title,
            term=months_word(months),
            amount=money(amount),
            until=fmt_dt(st.total_end),
            card=esc(method.title),
        )
        await self.ctx.notify.send(
            user.id, text, kb(btn(labels.AUTOPAY_OFF, "autopay:off"), btn(labels.SUBSCRIPTION, "menu:sub")), paced=True
        )
        async with self.ctx.session() as s:
            await s.execute(update(User).where(User.id == user.id).values(autopay_notified_for=st.total_end))
            await s.commit()

    async def _has_pending_autopay(self, user_id: int, now: datetime) -> bool:
        """Есть ли свежий автоплатёж «в обработке» (например, завис 3-D Secure) — тогда новое списание не создаём."""
        async with self.ctx.session() as s:
            rows = (
                await s.scalars(
                    select(Payment)
                    .where(
                        Payment.user_id == user_id,
                        Payment.provider == "yookassa",
                        Payment.status == PENDING,
                        Payment.created_at > now - max(PENDING_GUARD, AUTOPAY_KEY_LIFETIME),
                    )
                    .order_by(desc(Payment.id))
                    .limit(10)
                )
            ).all()
        for p in rows:
            if (p.meta or {}).get("autopay"):
                # без external_id — запрос мог дойти до ЮKassa: ждём повтора с тем же ключом, нового списания не создаём
                return True
        return False

    async def _last_paid_tier(self, user_id: int) -> Tier | None:
        async with self.ctx.session() as s:
            code = await s.scalar(
                select(Payment.plan_code)
                .where(Payment.user_id == user_id, Payment.status == SUCCEEDED, Payment.kind.in_(("buy", "upgrade")))
                .order_by(desc(Payment.id))
                .limit(1)
            )
        return Tier(code) if code else None

    async def autopay_charge(self, user_id: int) -> None:
        prov = self.providers["yookassa"]
        if self.ctx.yk is None:
            return
        outcome: tuple[str, bool] | None = None   # (причина, постоянный ли отказ) — обрабатываем ПОСЛЕ снятия блокировки
        resolve: tuple[int, Status] | None = None
        async with self.ctx.subs.lock(user_id):
            user = await self.ctx.users.get(user_id)
            if user is None or not user.autopay or user.is_banned:
                return
            now = utcnow()
            st = self.ctx.subs.view(user, now)
            if await self._has_pending_autopay(user_id, now):
                return  # защита от двойного списания: предыдущая попытка ещё не завершена
            tier = renewal_tier(st)
            if tier is None and user.expires_at and now - user.expires_at < MISSED_CYCLE_WINDOW:
                tier = await self._last_paid_tier(user_id)
            method = await self.latest_method(user_id)
            if tier is None or method is None:
                async with self.ctx.session() as s:
                    await s.execute(update(User).where(User.id == user_id).values(autopay=False, autopay_next_at=None, autopay_cycle_end=None))
                    await s.commit()
                return
            plan = self.ctx.plans.get(tier)
            if not self.ctx.plans.is_sellable(plan):
                log.warning("автопродление %s: тариф %s недоступен", user_id, plan.title)
                return
            months = self._autopay_months(user)
            _p, _d, amount = self._autopay_amounts(user, tier, months, st, now)
            extras = int(user.extra_devices or 0) if st.is_active(now) else 0
            cycle_end = user.autopay_cycle_end or st.total_end or user.expires_at or now
            description = f"{BRAND} — {plan.title}, {months_word(months)} (автопродление)"
            async with self.ctx.session() as s:
                pay = Payment(
                    user_id=user_id,
                    provider="yookassa",
                    plan_code=tier.value,
                    kind="buy",
                    period_days=term_days(months, plan.period_days),
                    amount=amount,
                    base_amount=amount,
                    discount=Decimal("0"),
                    status=PENDING,
                    description=description,
                    receipt_email=user.email,
                    created_at=now,
                    expires_at=now + PENDING_GUARD,
                    meta={
                        "autopay": True,
                        "save": False,
                        "months": months,
                        "extra_devices": extras,
                        "method_id": method.method_id,
                        "cycle_end": iso_z(cycle_end),
                        "sig": f"autopay:{months}:{extras}",
                    },
                )
                s.add(pay)
                await s.flush()
                pay_id = pay.id
                await s.execute(
                    update(User).where(User.id == user_id).values(autopay_last_at=now, autopay_cycle_end=cycle_end)
                )
                await s.commit()
            try:
                inv = await prov.charge_saved(pay, user, method.method_id)
            except ProviderError as e:
                log.error("автосписание #%s не удалось: %s", pay_id, e)
                if e.forbidden:
                    # проблема с ключами/правами магазина — затрагивает всех, а не конкретного пользователя.
                    # Автопродление НЕ выключаем глобально: шлём алерт и пробуем снова через час.
                    metrics.inc("payments_total", provider="yookassa", status="autopay_forbidden")
                    await self._set_status(pay_id, CANCELED, cancel_reason="autopay_forbidden")
                    await self._postpone_autopay(user_id, timedelta(hours=1))
                    await self.ctx.notify.alert_once(
                        "autopay_forbidden",
                        "🚨 <b>ЮKassa отклонила автосписание (401/403)</b>: проверьте ключи магазина и включённые автоплатежи. "
                        "Автопродление не выключено — попытки продолжатся автоматически, пользователи не затронуты.",
                    )
                    return
                if e.transient:
                    # исход неизвестен (таймаут/5xx): платёж могли провести. НЕ отменяем и НЕ пугаем пользователя —
                    # воркер повторит запрос с тем же ключом идемпотентности, дубля не будет.
                    metrics.inc("payments_total", provider="yookassa", status="autopay_unknown_outcome")
                    return
                await self._set_status(pay_id, CANCELED, cancel_reason="autopay_error")
                outcome = (str(e), True)       # окончательный отказ API: повторы бессмысленны
            else:
                async with self.ctx.session() as s:
                    p = await s.get(Payment, pay_id)
                    assert p is not None
                    p.external_id = inv.external_id
                    await s.commit()
                if inv.status is not None:
                    resolve = (pay_id, inv.status)
        # блокировка снята: дальше можно брать её снова (начисление, льготное продление)
        if outcome is not None:
            await self._autopay_failed(user_id, outcome[0], permanent=outcome[1])
        elif resolve is not None:
            await self._resolve(*resolve)  # paid -> сразу начисляем; pending -> добёрет вебхук/воркер

    async def _retry_autopay_create(self, pay: Payment) -> bool:
        """Автоплатёж без external_id: повторяем запрос с ТЕМ ЖЕ ключом — ЮKassa вернёт уже созданный платёж или создаст его."""
        meta = pay.meta or {}
        method_id = meta.get("method_id")
        if not method_id or pay.user_id is None or self.ctx.yk is None:
            await self._to_review(pay.id, "autopay_cannot_retry")
            return False
        user = await self.ctx.users.get(pay.user_id)
        if user is None:
            return False
        try:
            inv = await self.providers["yookassa"].charge_saved(pay, user, str(method_id))
        except ProviderError as e:
            if e.forbidden or e.transient:
                return False                       # попробуем в следующую итерацию
            await self._set_status(pay.id, CANCELED, cancel_reason="autopay_error")
            await self._autopay_failed(pay.user_id, str(e), permanent=True)
            return True
        async with self.ctx.session() as s:
            row = await s.get(Payment, pay.id)
            if row is not None and not row.external_id:
                row.external_id = inv.external_id
                await s.commit()
        metrics.inc("payments_total", provider="yookassa", status="autopay_retry_ok")
        if inv.status is not None:
            await self._resolve(pay.id, inv.status)
        return True

    async def _postpone_autopay(self, user_id: int, delay: timedelta) -> None:
        async with self.ctx.session() as s:
            await s.execute(update(User).where(User.id == user_id).values(autopay_next_at=utcnow() + delay))
            await s.commit()

    async def _autopay_failed(self, user_id: int, reason: str, *, permanent: bool | None = None) -> None:
        """Неудачное автосписание: назначаем повтор (D+1, D+3) с льготным доступом либо выключаем автопродление."""
        if permanent is None:
            permanent = is_permanent_decline(reason)
        grace = int(self.ctx.cfg.get("autopay_grace_days") or 0)
        now = utcnow()
        ext = core.ZERO
        nxt: datetime | None = None
        renew_tier: Tier | None = None
        async with self.ctx.session() as s:
            u = await s.get(User, user_id, with_for_update=True)
            if u is None:
                return
            st = self.ctx.subs.view(u, now)
            failures = (u.autopay_failures or 0) + 1
            cycle_end = u.autopay_cycle_end or st.total_end or u.expires_at or now
            nxt = None if permanent else next_attempt_at(cycle_end, failures, grace)
            u.autopay_failures = failures
            if nxt is None:
                u.autopay = False
                u.autopay_next_at = None
                u.autopay_cycle_end = None
            else:
                u.autopay_next_at = nxt
                u.autopay_cycle_end = cycle_end
                renew_tier = renewal_tier(st)
                if renew_tier is not None and st.total_end is not None:
                    ext = grace_extension(cycle_end, failures, st.total_end, grace)
            await s.commit()
        # льготный период: продлеваем подписку, чтобы доступ дожил до следующей попытки
        if ext > core.ZERO and renew_tier is not None:
            await self.ctx.subs.grant_delta(user_id, renew_tier, ext, kind="grace", meta={"reason": str(reason)[:80], "failures": failures})
        async with self.ctx.session() as s:
            fresh = await s.get(User, user_id)
        until = fmt_dt(self.ctx.subs.view(fresh, now).total_end) if fresh else "—"
        texts = self.ctx.texts
        markup = kb(btn(labels.RENEW, "pay:menu"), btn(labels.SUBSCRIPTION, "menu:sub"))
        if nxt is not None:
            text = texts.render("autopay_retry", retry_at=fmt_dt(nxt), until=until)
        elif permanent:
            text = texts.render("autopay_card_problem")
        else:
            text = texts.render("autopay_disabled", attempts=max_attempts(grace))
        await self.ctx.notify.send(user_id, text, markup, critical=True)
