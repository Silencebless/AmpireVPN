"""Экраны пользовательской части: каждая функция возвращает (текст, клавиатура)."""
from __future__ import annotations

import asyncio
import html
from typing import TYPE_CHECKING
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import labels
from app.bot.ui import btn, kb
from app.core.pricing import PROMO_DAYS, TERMS, renewal_amount
from app.core.subscription import Tier
from app.db.models import User
from app.integrations.remnawave import RemnaError
from app.legal import CONSENT_DOCS, DOCS
from app.utils import (
    days_left,
    days_word,
    devices_word,
    esc,
    fmt_date,
    fmt_dt,
    money,
    money_btn,
    months_word,
    utcnow,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

Screen = tuple[str, InlineKeyboardMarkup]


def attr(value: str) -> str:
    """Безопасная вставка адреса в атрибут href внутри HTML-текста."""
    return html.escape(value, quote=True)


def support_button(ctx: AppContext) -> InlineKeyboardButton:
    """«Поддержка» открывает раздел с FAQ и чатом — а не сразу чат."""
    return btn(labels.SUPPORT, "sup:menu")


# ───────────────────────── документы и согласия ─────────────────────────
def doc_rows(ctx: AppContext, keys: tuple[str, ...], *, cols: int = 2) -> list[list[InlineKeyboardButton]]:
    """Кнопки-ссылки на документы: по ``cols`` в ряд («Документ | Документ»)."""
    buttons = [btn(DOCS[k].short if cols > 1 else DOCS[k].title, url=ctx.cfg.legal_url(k)) for k in keys]
    return [buttons[i : i + cols] for i in range(0, len(buttons), cols)]


def trial_limits_text(ctx: AppContext) -> str:
    gb = int(ctx.cfg.get("trial_traffic_gb") or 0)
    devices = int(ctx.cfg.get("trial_devices") or 1)
    traffic = f"до {gb} ГБ трафика" if gb > 0 else "безлимитный трафик"
    return f"{traffic.capitalize()} · {devices_word(devices)}"


def welcome_screen(ctx: AppContext, user: User) -> Screen:
    name = esc(user.first_name or "друг")
    if ctx.cfg.get("trial_enabled"):
        text = ctx.texts.render(
            "welcome_new",
            name=name,
            trial_days=days_word(int(ctx.cfg.get("trial_days"))),
            trial_tier=Tier(ctx.cfg.get("trial_tier")).title,
            trial_limits=trial_limits_text(ctx),
        )
    else:
        text = ctx.texts.render("welcome_no_trial", name=name)
    # Документ | Документ / Документ | Документ / Подключить
    return text, kb(*doc_rows(ctx, CONSENT_DOCS), btn(labels.CONNECT, "connect"))


def legal_gate_screen(ctx: AppContext, target: str) -> Screen:
    """Экран согласия для тех, кто доходит до оплаты, не пройдя приветствие (например, пользователи прежней версии)."""
    return ctx.texts.render("legal_gate"), kb(
        *doc_rows(ctx, CONSENT_DOCS), btn("Принимаю и продолжить", f"legal:ok:{target}"), btn(labels.BACK, "menu:main")
    )


def docs_screen(ctx: AppContext) -> Screen:
    rows = [btn(DOCS[k].title, url=ctx.cfg.legal_url(k)) for k in DOCS]
    return ctx.texts.render("docs_menu"), kb(*rows, btn(labels.BACK, "sup:menu"))


def support_screen(ctx: AppContext) -> Screen:
    chat = btn(labels.CHAT, url=ctx.support_url) if ctx.support_url else btn(labels.CHAT, "sup:none")
    return ctx.texts.render("support_menu"), kb(
        btn(labels.FAQ, url=ctx.cfg.faq_url()), chat, btn(labels.DOCUMENTS, "sup:docs"), btn(labels.BACK, "menu:main")
    )


# ───────────────────────── статус ─────────────────────────
def status_line(ctx: AppContext, user: User) -> str:
    now = utcnow()
    st = ctx.subs.view(user, now)
    if st.is_active(now):
        line = f"🟢 <b>{st.tier.title}</b> · до {fmt_date(st.expires_at)} · ещё {days_word(days_left(st.expires_at, now))}"
        if st.queued_lite.total_seconds() > 0:
            line += f"\nДалее LITE — до {fmt_date(st.total_end)}"
        return line
    if user.remna_id or user.trial_used:
        return "🔴 Подписка не активна"
    return "⚪️ Подписки пока нет"


def main_menu(ctx: AppContext, user: User) -> Screen:
    text = ctx.texts.render("main_menu", status=status_line(ctx, user))
    rows = [
        btn(labels.SUBSCRIPTION, "menu:sub"),
        btn(labels.REFERRAL, "ref:menu"),
        support_button(ctx),
    ]
    if ctx.is_admin(user.id):
        rows.append(btn(labels.ADMIN, "a:home"))
    return text, kb(*rows)


# ───────────────────────── моя подписка ─────────────────────────
async def _devices_count(ctx: AppContext, user: User) -> int | None:
    if not user.remna_id:
        return 0
    try:
        return len(await asyncio.wait_for(ctx.devices.list(user), 4))
    except (RemnaError, asyncio.TimeoutError):
        return None


def capacity_text(ctx: AppContext, user: User) -> str:
    """«3 в тарифе + 1 доп.» — из чего складывается лимит устройств."""
    st = ctx.subs.view(user)
    if st.tier is None:
        return ""
    base = ctx.plans.get(st.tier).device_limit
    extra = int(user.extra_devices or 0)
    if user.trial_limited:
        return "лимит пробного периода"
    return f"{base} в тарифе" + (f" + {extra} доп." if extra else "")


def access_pending(user: User, st_active: bool) -> bool:
    """Подписка активна, но ссылки доступа ещё нет (панель была недоступна)."""
    return bool(st_active and not user.sub_url)


async def sub_menu(ctx: AppContext, user: User) -> Screen:
    now = utcnow()
    st = ctx.subs.view(user, now)
    active = st.is_active(now)
    lines: list[str] = []
    if active:
        lines += [
            "Статус — 🟢 активна",
            f"Тариф — <b>{st.tier.title}</b>",
            f"Действует до — <b>{fmt_dt(st.expires_at)}</b> · ещё {days_word(days_left(st.expires_at, now))}",
        ]
        if st.queued_lite.total_seconds() > 0:
            q_days = round(st.queued_lite.total_seconds() / 86400)
            lines.append(f"Далее — LITE ещё {days_word(q_days)}, до {fmt_date(st.total_end)}")
        used = await _devices_count(ctx, user)
        limit = ctx.subs.device_limit_for(user, st)
        lines.append(f"Устройства — {'—' if used is None else used} из {limit} ({capacity_text(ctx, user)})")
        if access_pending(user, True):
            lines.append("Доступ — настраивается, обычно меньше минуты")
        if user.pending_promo_id:
            lines.append("Промокод — скидка применится к следующей оплате")
        method = await ctx.pay.latest_method(user.id)
        if method is not None:
            state = "включено" if user.autopay else "выключено"
            months = int(user.autopay_months or 1)
            term = f" · {months_word(months)}" if user.autopay else ""
            lines.append(f"Автопродление — {state} ({esc(method.title)}{term})")
    elif user.remna_id or user.trial_used:
        lines += ["Статус — 🔴 закончилась", f"Была до — {fmt_dt(st.expires_at)}", "", "Оформите тариф, чтобы вернуть доступ."]
    else:
        lines += ["Статус — ⚪️ подписки пока нет", "", "Выберите тариф — подключение займёт минуту."]

    text = ctx.texts.render("sub_menu", details="\n".join(lines))
    autopay_rows: list = []
    if active or user.autopay:
        method = await ctx.pay.latest_method(user.id)
        if method is not None:
            autopay_rows = [
                btn(labels.AUTOPAY_OFF if user.autopay else labels.AUTOPAY_ON, "autopay:off" if user.autopay else "autopay:on"),
                btn(labels.AUTOPAY_UNLINK, "autopay:unlink"),
            ]
    markup = kb(
        btn(labels.RENEW if active else labels.BUY, "pay:menu"),
        btn(labels.SETUP, "setup:open"),
        btn(labels.DEVICES, "dev:list"),
        *autopay_rows,
        btn(labels.BACK, "menu:main"),
    )
    return text, markup


# ───────────────────────── тарифы, сроки, заказ ─────────────────────────
def pay_menu(ctx: AppContext, user: User) -> Screen:
    plans = ctx.plans.sellable()
    summary = "\n\n".join(ctx.plans.short_line(p) for p in plans) or "Тарифы временно недоступны."
    text = ctx.texts.render("pay_menu", status=status_line(ctx, user), plans=summary)
    rows = [btn(ctx.plans.button_label(p), f"pay:plan:{p.code}") for p in plans]
    rows.append(btn(labels.BACK, "menu:sub"))
    return text, kb(*rows)


def _up(upgrade: bool) -> str:
    return ":up" if upgrade else ""


async def plan_view(ctx: AppContext, user: User, code: str) -> Screen | None:
    """Карточка тарифа: описание и выбор срока (1/3/6/12 месяцев) с итоговой ценой на каждой кнопке."""
    if code not in ("lite", "premium"):
        return None
    plan = ctx.plans.get(code)
    if not ctx.plans.is_sellable(plan):
        return None
    tier = Tier(code)
    quotes, promo = await ctx.pay.quote_terms(user, tier, upgrade=True)
    q1 = quotes[1]
    st = q1.before
    t = ctx.texts
    now = utcnow()

    if q1.kind == "upgrade":
        context = t.render(
            "ctx_upgrade", until=fmt_date(st.expires_at), left=days_word(days_left(st.expires_at, now)), extra=money(q1.extra)
        )
    elif st.is_active(now) and st.tier == Tier.PREMIUM and tier == Tier.LITE:
        context = t.render("ctx_queue", until=fmt_date(st.expires_at))
    elif st.is_active(now) and st.tier == tier:
        context = t.render("ctx_renew", tier=tier.title, until=fmt_date(st.expires_at))
    else:
        context = t.render("ctx_new")
    if promo is not None and q1.discount > 0:
        context += f"\n\nПромокод <b>{esc(promo.code)}</b> применится к заказу."

    text = t.render(
        "plan_view",
        title=esc(plan.title),
        price=money(plan.price),
        features=ctx.plans.features_block(plan, device_price=ctx.cfg.device_price()),
        context=context,
    )
    suffix = _up(q1.kind == "upgrade")
    rows: list = []
    for m in TERMS:
        q = quotes[m]
        label = f"{months_word(m)} — {money_btn(q.amount)}"
        if q.term_discount_pct:
            label += f" · −{q.term_discount_pct}%"
        rows.append(btn(label, f"pay:t:{code}:{m}{suffix}"))
    if q1.kind == "upgrade":
        rows.append(btn(labels.WAIT_LITE_END, "menu:sub"))
    rows.append(btn(labels.BACK, "pay:menu"))
    return text, kb(*rows)


async def order_screen(ctx: AppContext, user: User, code: str, months: int, upgrade: bool) -> Screen | None:
    """Сводка заказа перед оплатой: из чего складывается сумма и до какого числа продлится доступ."""
    if code not in ("lite", "premium") or months not in TERMS:
        return None
    plan = ctx.plans.get(code)
    if not ctx.plans.is_sellable(plan):
        return None
    tier = Tier(code)
    q, promo = await ctx.pay.quote(user, tier, months=months, upgrade=upgrade)
    term = months_word(months)
    lines = [f"◆ Подписка {plan.title} · {term} — {money(q.plan_amount)}"]
    if q.devices_amount > 0:
        lines.append(f"◆ Дополнительные устройства ({q.extra_devices}) · {term} — {money(q.devices_amount)}")
    if q.extra > 0:
        lines.append(f"◆ Доплата за остаток LITE — {money(q.extra)}")
    if q.term_discount_pct:
        lines.append(f"Скидка за срок уже учтена: −{q.term_discount_pct}%")
    promo_line = ""
    if promo is not None and q.discount > 0:
        promo_line = f"\n◆ Промокод {esc(promo.code)} — −{money(q.discount)}"

    now = utcnow()
    if q.kind == "upgrade":
        timeline = f"Остаток LITE перейдёт на PREMIUM. Всего PREMIUM — до <b>{fmt_dt(q.after.total_end)}</b>."
    elif q.before.is_active(now) and q.before.tier == Tier.PREMIUM and tier == Tier.LITE:
        timeline = f"LITE начнётся сразу после PREMIUM и продлится до <b>{fmt_dt(q.after.total_end)}</b>."
    elif q.before.is_active(now):
        timeline = f"После оплаты доступ продлится до <b>{fmt_dt(q.after.total_end)}</b>."
    else:
        timeline = f"Доступ откроется сразу и будет действовать до <b>{fmt_dt(q.after.total_end)}</b>."

    text = ctx.texts.render(
        "order_summary",
        plan=plan.title,
        term=term,
        breakdown="\n".join(lines),
        promo_line=promo_line,
        total=money(q.amount),
        timeline=timeline,
    )
    markup = kb(
        btn(f"{labels.PAY} {money_btn(q.amount)}", f"pay:go:{code}:{months}{_up(upgrade)}"),
        btn("Изменить срок", f"pay:plan:{code}"),
    )
    return text, markup


# ───────────────────────── способы оплаты ─────────────────────────
# ключ кнопки -> (провайдер, сохранить способ оплаты для автопродления)
PAY_OPTIONS = {"yk": ("yookassa", False), "yka": ("yookassa", True), "cb": ("cryptobot", False)}
PAY_LABELS = {"yk": labels.PAY_CARD, "yka": labels.PAY_CARD_AUTO, "cb": labels.PAY_CRYPTO}


def payment_options(ctx: AppContext, *, autopay: bool = True) -> list[str]:
    """Какие кнопки способов оплаты показывать сейчас (автопродление не предлагается для устройств)."""
    codes = {p.code for p in ctx.pay.available_providers()}
    out: list[str] = []
    if "yookassa" in codes:
        out.append("yk")
        if autopay and ctx.pay.autopay_possible():
            out.append("yka")
    if "cryptobot" in codes:
        out.append("cb")
    return out


async def pay_method_screen(ctx: AppContext, user: User, code: str, months: int, upgrade: bool) -> Screen:
    tier = Tier(code)
    q, _ = await ctx.pay.quote(user, tier, months=months, upgrade=upgrade)
    suffix = _up(upgrade)
    rows = [btn(PAY_LABELS[o], f"pay:m:{o}:{code}:{months}{suffix}") for o in payment_options(ctx)]
    rows.append(btn(labels.BACK, f"pay:t:{code}:{months}{suffix}"))
    text = ctx.texts.render(
        "pay_method",
        plan=tier.title,
        term=months_word(months),
        amount=money(q.amount),
        offer_url=attr(ctx.cfg.legal_url("offer")),
        refund_url=attr(ctx.cfg.legal_url("refund")),
    )
    return text, kb(*rows)


async def autopay_consent_screen(ctx: AppContext, user: User, code: str, months: int, upgrade: bool) -> Screen:
    """Условия автопродления — показываем ДО привязки карты и берём явное согласие."""
    tier = Tier(code)
    plan = ctx.plans.get(tier)
    now = utcnow()
    extras = int(user.extra_devices or 0) if ctx.subs.view(user, now).is_active(now) else 0
    _p, _d, recurring = renewal_amount(
        plan.price, months, ctx.cfg.term_discounts()[months], extras, ctx.cfg.device_price()
    )
    devices_line = f" (включая {devices_word(extras)} сверх тарифа)" if extras else ""
    text = ctx.texts.render(
        "autopay_consent", plan=plan.title, amount=money(recurring), term=months_word(months), devices_line=devices_line
    )
    suffix = _up(upgrade)
    markup = kb(
        btn(DOCS["autopay"].title, url=ctx.cfg.legal_url("autopay")),
        btn(labels.AGREE_CONTINUE, f"pay:mc:yka:{code}:{months}{suffix}"),
        btn(labels.BACK, f"pay:go:{code}:{months}{suffix}"),
    )
    return text, markup


# ───────────────────────── настройка ─────────────────────────
def pending_screen(ctx: AppContext, back_cb: str = "menu:sub") -> Screen:
    """Оплата/пробный период получены, но панель ещё не выдала ссылку: «настраиваем доступ» + «Повторить»."""
    return ctx.texts.render("setup_pending"), kb(btn(labels.RETRY, "setup:retry"), support_button(ctx), btn(labels.BACK, back_cb))


def setup_screen(ctx: AppContext, user: User, back_cb: str = "menu:sub", back_text: str = labels.BACK) -> Screen:
    active = ctx.subs.view(user).is_active(utcnow())
    if active and (not user.sub_url or not user.short_uuid):
        return pending_screen(ctx, back_cb)
    if not user.sub_url or not user.short_uuid:
        return ctx.texts.render("setup_no_sub"), kb(btn(labels.BUY, "pay:menu"), btn(back_text, back_cb))
    text = ctx.texts.render("setup", sub_block=f"<code>{esc(user.sub_url)}</code>")
    if not active:
        text += "\n\nПодписка сейчас не активна — продлите её, чтобы VPN заработал."
    markup = kb(
        btn("Скачать Happ", url=ctx.dl_url()) if ctx.web_ready else None,
        btn("Добавить подписку", url=ctx.add_url(user.short_uuid)) if ctx.web_ready else None,
        support_button(ctx),
        btn(back_text, back_cb),
    )
    return text, markup


# ───────────────────────── устройства ─────────────────────────
async def devices_screen(ctx: AppContext, user: User) -> Screen:
    now = utcnow()
    st = ctx.subs.view(user, now)
    if not st.is_active(now):
        return ctx.texts.render("setup_no_sub"), kb(btn(labels.BUY, "pay:menu"), btn(labels.BACK, "menu:sub"))
    if not user.remna_id:
        return pending_screen(ctx)
    try:
        devices = await asyncio.wait_for(ctx.devices.list(user), 8)
    except (RemnaError, asyncio.TimeoutError):
        return ctx.texts.render("panel_unavailable"), kb(btn(labels.BACK, "menu:sub"))

    limit = ctx.devices.limit_for(user)
    if devices:
        lines = "\n".join(f"{i}. {esc(d.label)}" for i, d in enumerate(devices, 1))
    else:
        lines = ctx.texts.render("devices_empty")
    text = ctx.texts.render("devices_menu", used=len(devices), limit=limit, capacity=capacity_text(ctx, user), list=lines)
    rows: list = [btn(d.button, f"dev:v:{d.key}") for d in devices]
    rows.append(btn(labels.ADD_DEVICE, "dev:add"))
    if not user.trial_limited and ctx.cfg.device_price() > 0:
        if ctx.pay.devices_can_buy(user) > 0:
            rows.append(btn(labels.MORE_DEVICES, "dv:buy:1"))
        if int(user.extra_devices or 0) > 0:
            rows.append(btn(labels.LESS_DEVICES, "dv:less"))
    rows.append(btn(labels.BACK, "menu:sub"))
    return text, kb(*rows)


async def device_limit_screen(ctx: AppContext, user: User) -> Screen:
    """Лимит исчерпан: предлагаем увеличить лимит, удалить устройство или перейти на PREMIUM."""
    devices = await ctx.devices.list(user)
    limit = ctx.devices.limit_for(user)
    st = ctx.subs.view(user)
    premium = ctx.plans.get(Tier.PREMIUM)
    can_upgrade = st.tier == Tier.LITE and ctx.plans.is_sellable(premium) and premium.device_limit > ctx.plans.get(Tier.LITE).device_limit and not user.trial_limited
    hint = ctx.texts.render("device_limit_hint_premium", premium_limit=devices_word(premium.device_limit)) if can_upgrade else ""
    text = ctx.texts.render("device_limit", used=len(devices), limit=limit, hint=hint)
    rows: list = []
    if not user.trial_limited and ctx.cfg.device_price() > 0 and ctx.pay.devices_can_buy(user) > 0:
        rows.append(btn(f"{labels.MORE_DEVICES} · {money_btn(ctx.cfg.device_price())} в месяц", "dv:buy:1"))
    if can_upgrade:
        rows.append(btn("Перейти на PREMIUM", "pay:plan:premium"))
    rows += [btn(f"{labels.DELETE}: {d.label}", f"dev:del:{d.key}") for d in devices]
    rows.append(btn(labels.BACK, "dev:list"))
    return text, kb(*rows)


async def device_buy_screen(ctx: AppContext, user: User, count: int) -> Screen:
    """Выбор числа дополнительных устройств: [−] N [+] и живая цена."""
    now = utcnow()
    st = ctx.subs.view(user, now)
    back = kb(btn(labels.BACK, "dev:list"))
    if not st.is_active(now):
        return ctx.texts.render("device_buy_blocked_none"), kb(btn(labels.BUY, "pay:menu"), btn(labels.BACK, "dev:list"))
    if user.trial_limited:
        return ctx.texts.render("device_buy_blocked_trial"), kb(btn(labels.BUY, "pay:menu"), btn(labels.BACK, "dev:list"))
    room = ctx.pay.devices_can_buy(user)
    if room <= 0:
        return ctx.texts.render("device_buy_max", extra=int(user.extra_devices or 0)), back
    count = max(1, min(room, count))
    dq = ctx.pay.devices_quote(user, count)
    if dq is None:
        return ctx.texts.render("device_buy_blocked_none"), back
    limit_now = ctx.devices.limit_for(user)
    text = ctx.texts.render(
        "device_buy",
        limit_now=limit_now,
        count=count,
        limit_after=limit_now + count,
        amount=money(dq.amount),
        days=days_word(dq.days),
        unit_price=money(ctx.cfg.device_price()),
    )
    stepper = [
        btn("−1", f"dv:buy:{count - 1}") if count > 1 else btn("·", "noop"),
        btn(f"+{count}", "noop"),
        btn("+1", f"dv:buy:{count + 1}") if count < room else btn("·", "noop"),
    ]
    markup = kb(stepper, btn(f"{labels.PAY} {money_btn(dq.amount)}", f"dv:go:{count}"), btn(labels.BACK, "dev:list"))
    return text, markup


async def device_pay_method_screen(ctx: AppContext, user: User, count: int) -> Screen:
    dq = ctx.pay.devices_quote(user, count)
    amount = money(dq.amount) if dq else "—"
    text = ctx.texts.render(
        "pay_method_devices",
        what=f"Дополнительно {devices_word(count)}",
        amount=amount,
        offer_url=attr(ctx.cfg.legal_url("offer")),
        refund_url=attr(ctx.cfg.legal_url("refund")),
    )
    rows = [btn(PAY_LABELS[o], f"dv:m:{o}:{count}") for o in payment_options(ctx, autopay=False)]
    rows.append(btn(labels.BACK, f"dv:buy:{count}"))
    return text, kb(*rows)


# ───────────────────────── приглашения ─────────────────────────
async def referral_screen(ctx: AppContext, user: User) -> Screen:
    if not ctx.cfg.get("referral_enabled"):
        return ctx.texts.render("ref_disabled"), kb(btn(labels.PROMO, "promo:ask"), btn(labels.BACK, "menu:main"))
    stats = await ctx.refs.stats(user.id)
    link = ctx.ref_link(user.ref_code)
    tier = Tier(ctx.cfg.get("referral_tier"))
    bonus_days = int(ctx.cfg.get("referral_days"))
    text = ctx.texts.render(
        "ref_menu",
        bonus=days_word(bonus_days),
        tier=tier.title,
        link=link,
        invited=stats["invited"],
        connected=stats["connected"],
        earned=days_word(stats["days"]),
    )
    trial = (
        f"{days_word(int(ctx.cfg.get('trial_days')))} {Tier(ctx.cfg.get('trial_tier')).title}"
        if ctx.cfg.get("trial_enabled")
        else "доступ"
    )
    share_text = ctx.texts.render("ref_share", trial_days=trial)
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(share_text, safe='')}"
    markup = kb(
        btn("Пригласить друга", url=share_url),
        btn(labels.PROMO, "promo:ask"),
        btn(labels.BACK, "menu:main"),
    )
    return text, markup


def promo_ok_text(ctx: AppContext, promo, new_state) -> str:
    from app.services.promos import describe_value

    if promo.kind == PROMO_DAYS:
        return ctx.texts.render(
            "promo_ok_days",
            days=int(promo.value),
            tier=str(promo.days_tier).upper(),
            until=fmt_dt(new_state.total_end),
        )
    tiers = ""
    if promo.tiers:
        tiers = " (тариф " + ", ".join(str(x).upper() for x in promo.tiers) + ")"
    return ctx.texts.render("promo_ok_discount", discount=describe_value(promo), tiers=tiers)
