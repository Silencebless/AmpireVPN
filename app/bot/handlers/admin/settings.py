from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.admin.common import ask, make_router, on_input
from app.bot.ui import btn, kb, show
from app.legal import is_http_url
from app.services.runtime_settings import INT_BOUNDS, LEGAL_META, META_BY_KEY, PRICING_META, SETTING_META, TRIAL_META
from app.utils import esc, parse_int

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("settings", scope="settings.edit")

BACKS = {"s": "a:s", "r": "a:r", "p": "a:sp", "z": "a:sz", "d": "a:sd"}
HINTS = {
    "trial_enabled": "Если выключить, новые пользователи не получат пробный доступ.",
    "trial_days": "Длительность пробного периода для новых пользователей.",
    "trial_tier": "На каком тарифе выдаётся пробный период.",
    "registration_open": "Если выключить, новые пользователи не смогут начать пользоваться ботом (старые — смогут).",
    "payments_enabled": "Если выключить, кнопки оплаты покажут «временно недоступно».",
    "receipts_enabled": "Передавать в ЮKassa данные чека (нужен email клиента). Отключай, только если чеки у тебя не нужны.",
    "maintenance": "Бот отвечает «технические работы» всем, кроме админов.",
    "reminders_enabled": "Напоминать пользователям об окончании подписки.",
    "reminder_days": "За сколько дней напоминать, через запятую: например «3,1».",
    "trim_devices_on_downgrade": "При переходе PREMIUM → LITE отключать самые новые устройства сверх лимита LITE.",
    "notify_sales": "Присылать владельцам уведомление о каждой оплате.",
    "alerts_nodes": "Присылать владельцам алерт, если нода пропала из сети (нужен вебхук Remnawave).",
    "support_url": "Ссылка для кнопки «Написать в поддержку», например https://t.me/your_support. Пусто — берётся из .env.",
    "referral_enabled": "Включает/выключает реферальную программу.",
    "referral_days": "Сколько дней получает пригласивший.",
    "referral_tier": "Какой тариф получает пригласивший.",
    "referral_min_mb": "Сколько МБ трафика должен потратить друг, чтобы считаться «воспользовался VPN» (или первое подключение).",
    "referral_max_rewards": "Максимум наград на одного пригласившего. 0 — без ограничений.",
    "referral_daily_cap": "Сколько наград один пригласивший может получить за сутки. Защита от накрутки. 0 — без лимита.",
    "referral_min_age_h": "Приглашённый должен «прожить» столько часов, прежде чем пригласивший получит награду. 0 — сразу.",
    "referral_check_hwid": "Если у пригласившего и приглашённого совпадает устройство (HWID) — награда отклоняется как самореферал.",
    "pay_yookassa_enabled": "Показывать оплату картой/СБП (нужны ключи ЮKassa в .env).",
    "pay_crypto_enabled": "Показывать оплату криптовалютой через CryptoBot (нужен CRYPTOBOT_TOKEN в .env).",
    "autopay_enabled": "Автопродление картой. Сработает, только если автоплатежи включены в ЮKassa (напиши менеджеру ЮKassa). Пользователь соглашается сам — кнопкой «Картой + автопродление».",
    "trial_channel": "Подписка на этот канал станет условием пробного периода. Укажи @имя (бот должен быть админом канала). Пусто — не требовать.",
    "trial_traffic_gb": "Лимит трафика на пробном периоде в ГБ. 0 — без лимита. Снимается автоматически после первой оплаты. Защита от фарма аккаунтов.",
    "trial_devices": "Сколько устройств разрешено на пробном периоде (даже на PREMIUM). Снимается после первой оплаты.",
    "device_price": "Цена одного дополнительного устройства в ₽ за месяц. При докупке считается пропорционально остатку подписки, затем входит в стоимость продления.",
    "max_extra_devices": "Сколько дополнительных устройств можно докупить сверх тарифа. 0 — докупка выключена.",
    "term_discount_3": "Скидка при оплате на 3 месяца, %. Применяется к тарифу и доп. устройствам.",
    "term_discount_6": "Скидка при оплате на 6 месяцев, %.",
    "term_discount_12": "Скидка при оплате на 12 месяцев, % (12 месяцев = 365 дней).",
    "autopay_grace_days": "Льготный период автопродления: сколько суток сохраняем доступ, пока повторяем неудавшееся списание (повторы через 1 и 3 суток). 0 — одна попытка без повторов.",
    "faq_url": "Ссылка на страницу «Вопросы и ответы» (Telegraph). Кнопка «Вопросы и ответы» в разделе «Поддержка».",
    "legal_offer_url": "Ссылка на публичную оферту (показывается при первом запуске и перед оплатой).",
    "legal_privacy_url": "Ссылка на политику конфиденциальности.",
    "legal_terms_url": "Ссылка на пользовательское соглашение.",
    "legal_pd_url": "Ссылка на согласие на обработку персональных данных.",
    "legal_autopay_url": "Ссылка на условия автопродления (показывается до привязки карты).",
    "legal_refund_url": "Ссылка на условия возврата.",
}


@router.callback_query(F.data == "a:s")
async def cb_menu(cb: CallbackQuery, ctx: AppContext) -> None:
    rows = [btn(f"{title}: {ctx.cfg.format_value(key)}"[:62], f"a:sv:{key}:s") for key, title, _ in SETTING_META]
    rows += [
        btn("💰 Цены, сроки и устройства", "a:sp"),
        btn("🧪 Пробный период: лимиты", "a:sz"),
        btn("📄 Документы и FAQ", "a:sd"),
        btn("← Назад", "a:home"),
    ]
    await show(cb, "⚙️ <b>Настройки</b>\n\nВсё применяется сразу, без перезапуска.", kb(*rows))


async def _submenu(cb: CallbackQuery, ctx: AppContext, title: str, meta: list, back: str, note: str = "") -> None:
    rows = [btn(f"{t}: {ctx.cfg.format_value(key)}"[:62], f"a:sv:{key}:{back}") for key, t, _ in meta]
    rows.append(btn("← Назад", "a:s"))
    await show(cb, f"{title}\n\n{note}Применяется сразу, без перезапуска.", kb(*rows))


@router.callback_query(F.data == "a:sp")
async def cb_pricing(cb: CallbackQuery, ctx: AppContext) -> None:
    await _submenu(
        cb, ctx, "💰 <b>Цены, сроки и устройства</b>", PRICING_META, "p",
        "Цена тарифа — в разделе «Тарифы». Здесь: скидки за срок (3/6/12 мес.), цена и потолок доп. устройств, льготный период автопродления.\n\n",
    )


@router.callback_query(F.data == "a:sz")
async def cb_trial(cb: CallbackQuery, ctx: AppContext) -> None:
    await _submenu(
        cb, ctx, "🧪 <b>Пробный период: защита от фарма</b>", TRIAL_META, "z",
        "Ограничения действуют до первой оплаты. Рефералка (возраст аккаунта, потолок наград) — в разделе «Рефералка».\n\n",
    )


@router.callback_query(F.data == "a:sd")
async def cb_docs(cb: CallbackQuery, ctx: AppContext) -> None:
    await _submenu(
        cb, ctx, "📄 <b>Документы и FAQ</b>", LEGAL_META, "d",
        "Сейчас стоят ссылки-заглушки — замените их на боевые страницы (оферту, политику и условия должен согласовать юрист).\n\n",
    )


async def view_setting(event: CallbackQuery | Message, ctx: AppContext, key: str, back: str, flash: str = "") -> None:
    title, kind = META_BY_KEY[key]
    back_cb = BACKS.get(back, "a:s")
    text = (
        (f"{flash}\n\n" if flash else "")
        + f"⚙️ <b>{esc(title)}</b>\n\nСейчас: <b>{esc(ctx.cfg.format_value(key))}</b>\n\n<i>{esc(HINTS.get(key, ''))}</i>"
    )
    rows = []
    if kind == "bool":
        rows.append(btn("Выключить" if ctx.cfg.get(key) else "Включить", f"a:sb:{key}:{back}"))
    elif kind == "tier":
        rows.append([btn("LITE", f"a:sst:{key}:lite:{back}"), btn("PREMIUM", f"a:sst:{key}:premium:{back}")])
    else:
        rows.append(btn("✏️ Изменить", f"a:se:{key}:{back}"))
    if ctx.cfg.is_overridden(key):
        rows.append(btn("♻️ Вернуть по умолчанию", f"a:sr:{key}:{back}"))
    rows.append(btn("← Назад", back_cb))
    await show(event, text, kb(*rows))


@router.callback_query(F.data.startswith("a:sv:"))
async def cb_view(cb: CallbackQuery, ctx: AppContext) -> None:
    _, _, key, back = (cb.data or "").split(":")
    await view_setting(cb, ctx, key, back)


@router.callback_query(F.data.startswith("a:sb:"))
async def cb_toggle(cb: CallbackQuery, ctx: AppContext) -> None:
    _, _, key, back = (cb.data or "").split(":")
    new = not ctx.cfg.get(key)
    await ctx.cfg.set(key, new)
    await ctx.audit.log(cb.from_user.id, "setting.set", key, value=new)
    await view_setting(cb, ctx, key, back, "✅ Сохранено")


@router.callback_query(F.data.startswith("a:sst:"))
async def cb_tier(cb: CallbackQuery, ctx: AppContext) -> None:
    _, _, key, tier, back = (cb.data or "").split(":")
    await ctx.cfg.set(key, tier)
    await ctx.audit.log(cb.from_user.id, "setting.set", key, value=tier)
    await view_setting(cb, ctx, key, back, "✅ Сохранено")


@router.callback_query(F.data.startswith("a:sr:"))
async def cb_reset(cb: CallbackQuery, ctx: AppContext) -> None:
    _, _, key, back = (cb.data or "").split(":")
    await ctx.cfg.reset(key)
    await ctx.audit.log(cb.from_user.id, "setting.reset", key)
    await view_setting(cb, ctx, key, back, "♻️ Возвращено значение по умолчанию")


@router.callback_query(F.data.startswith("a:se:"))
async def cb_edit(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, key, back = (cb.data or "").split(":")
    title, kind = META_BY_KEY[key]
    hint = {
        "int": "Отправь число.",
        "text": "Отправь текст (или «-», чтобы очистить).",
        "days_list": "Отправь числа дней через запятую, например: 3,1",
    }[kind]
    await ask(cb, state, "s_edit", f"✏️ <b>{esc(title)}</b>\n\n{hint}", f"a:sv:{key}:{back}", key=key, back=back)


@on_input("s_edit", "settings.edit")
async def in_edit(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    key, back = str(data["key"]), str(data["back"])
    _, kind = META_BY_KEY[key]
    raw = (message.text or "").strip()
    value: object
    if kind == "int":
        lo, hi = INT_BOUNDS.get(key, (0, 100000))
        parsed = parse_int(raw, lo=lo, hi=hi)
        if parsed is None:
            await message.answer(f"Нужно целое число от {lo} до {hi}.")
            return
        value = parsed
    elif kind == "days_list":
        try:
            nums = sorted({int(x) for x in raw.replace(" ", "").split(",") if x}, reverse=True)
        except ValueError:
            nums = []
        if not nums or len(nums) > 5 or any(n < 1 or n > 30 for n in nums):
            await message.answer("Нужно от 1 до 5 чисел (1–30) через запятую, например: 3,1")
            return
        value = nums
    else:
        value = "" if raw == "-" else raw[:500]
        if value and key.endswith("_url") and not is_http_url(str(value)):
            await message.answer("Нужна ссылка вида https://… без пробелов (например https://t.me/your_support).")
            return
        if value and key == "trial_channel" and not (str(value).startswith("@") or str(value).lstrip("-").isdigit()):
            await message.answer("Укажи канал как @имя (или числовой ID вида -100…).")
            return
    await state.clear()
    await ctx.cfg.set(key, value)
    await ctx.audit.log(message.from_user.id, "setting.set", key, value=str(value)[:100])
    await view_setting(message, ctx, key, back, "✅ Сохранено")
