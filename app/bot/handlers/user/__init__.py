from __future__ import annotations

from aiogram import Router


def get_routers() -> list[Router]:
    from app.bot.handlers.user import billing, devices, referral, start

    # start.router содержит «ловушку» для произвольного текста — подключаем последним
    return [billing.router, devices.router, referral.router, start.router]
