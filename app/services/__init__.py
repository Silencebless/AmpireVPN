from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext


def build_services(ctx: AppContext) -> None:
    """Создаёт сервисы и привязывает их к контексту (сервисы обращаются друг к другу через ctx)."""
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

    ctx.cfg = SettingsService(ctx)
    ctx.texts = TextService(ctx)
    ctx.audit = AuditService(ctx)
    ctx.notify = Notifier(ctx)
    ctx.users = UserService(ctx)
    ctx.plans = PlanService(ctx)
    ctx.subs = SubscriptionService(ctx)
    ctx.devices = DeviceService(ctx)
    ctx.pay = PaymentService(ctx)
    ctx.promos = PromoService(ctx)
    ctx.refs = ReferralService(ctx)
    ctx.stats = StatsService(ctx)
    ctx.bc = BroadcastService(ctx)
    ctx.backup = BackupService(ctx)
    ctx.links = LinkChecker(ctx)
