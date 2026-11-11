from __future__ import annotations

from aiogram import Router


def get_routers() -> list[Router]:
    from app.bot.handlers.admin import (
        admins,
        broadcast,
        common,
        dashboard,
        infra,
        payments,
        plans,
        promos,
        referrals,
        settings,
        system,
        texts,
        users,
    )

    return [
        common.router,      # /admin и обработчик текстового ввода должны идти первыми
        dashboard.router,
        users.router,
        payments.router,
        plans.router,
        promos.router,
        referrals.router,
        broadcast.router,
        texts.router,
        infra.router,
        settings.router,
        system.router,
        admins.router,
        common.denied_router,   # в самом конце: «Недостаточно прав» вместо молчания
    ]
