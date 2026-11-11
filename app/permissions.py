"""Роли и права администраторов (чистая логика, без БД).

Владельцы (OWNER_IDS) могут всё. Остальным админам выдаётся ОДНА роль с ограниченным набором прав:

* support   — поддержка: смотреть пользователей и платежи, помогать (устройства, ссылка, сообщение, заметка, блокировка);
* billing   — биллинг: платежи (в т. ч. ручная проверка спорных), выдача/снятие дней, статистика;
* marketing — маркетинг: промокоды, рефералка, рассылки, тексты, статистика;
* ops       — инфраструктура: узлы и сквады, диагностика, журнал ошибок;
* admin     — расширенный доступ ко всему, кроме действий «только для владельца».

Действия «только для владельца»: возвраты денег, управление админами, удаление пользователей,
бэкап/сверка/массовая пересинхронизация.
"""
from __future__ import annotations

OWNER_ONLY: frozenset[str] = frozenset({"payments.refund", "admins.manage", "users.delete", "system.ops"})

BASE_SCOPES: frozenset[str] = frozenset(
    {
        "stats.view",
        "users.view",
        "users.manage",
        "subs.grant",
        "payments.view",
        "payments.review",
        "plans.edit",
        "promos.manage",
        "referrals.manage",
        "broadcast.send",
        "texts.edit",
        "settings.edit",
        "infra.manage",
        "system.view",
    }
)

ROLES: dict[str, frozenset[str]] = {
    "support": frozenset({"users.view", "users.manage", "payments.view"}),
    "billing": frozenset({"stats.view", "users.view", "payments.view", "payments.review", "subs.grant"}),
    "marketing": frozenset({"stats.view", "promos.manage", "referrals.manage", "broadcast.send", "texts.edit"}),
    "ops": frozenset({"stats.view", "users.view", "infra.manage", "system.view"}),
    "admin": BASE_SCOPES,
}

ROLE_TITLES: dict[str, str] = {
    "support": "Поддержка",
    "billing": "Биллинг",
    "marketing": "Маркетинг",
    "ops": "Инфраструктура",
    "admin": "Администратор",
}
ROLE_ORDER: tuple[str, ...] = ("support", "billing", "marketing", "ops", "admin")

ALL_SCOPES: frozenset[str] = BASE_SCOPES | OWNER_ONLY


def scopes_for(role: str | None, *, owner: bool = False) -> frozenset[str]:
    if owner:
        return ALL_SCOPES
    return ROLES.get(role or "", frozenset())


def allowed(scope: str | None, role: str | None, *, owner: bool = False) -> bool:
    """scope=None — действие доступно любому администратору."""
    if owner or scope is None:
        return True
    return scope in scopes_for(role)


def role_title(role: str | None, *, owner: bool = False) -> str:
    return "Владелец" if owner else ROLE_TITLES.get(role or "", "—")
