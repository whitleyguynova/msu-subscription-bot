"""Оплата переводом по СБП — упрощённый режим для маленького канала.

1. Человек выбирает тариф → бот показывает номер и ровную сумму.
2. Человек переводит и жмёт «Я перевёл» → бот сразу заносит оплату в базу и включает доступ.
3. Владельцу приходит уведомление «занесён в базу» с кнопкой «Отменить» на случай,
   если перевода на самом деле не было.
4. Дальше бот сам следит за сроком, напоминает о продлении и отключает по окончании.

Если человек перевёл, но кнопку не нажал, — владелец заносит его сам: /paid @ник m1
"""
import html

from aiogram import Bot
from aiogram.types import CopyTextButton, InlineKeyboardButton

import config
import db
import services as S
from db import now
from services import btn, fmt_date, kb

DAY = 86400


def enabled() -> bool:
    return config.MANUAL_PAY and bool(config.MANUAL_PAY_PHONE)


async def bank_for(uid: int) -> str | None:
    """Банк, закреплённый за человеком. Новым — тот, за которым сейчас меньше людей."""
    banks = config.MANUAL_PAY_BANKS
    if not banks:
        return None
    u = await db.get_user(uid)
    if u and u["pay_bank"] in banks:
        return u["pay_bank"]
    counts = {b: 0 for b in banks}
    for r in await db.fetch("SELECT pay_bank, COUNT(*) n FROM users WHERE pay_bank IS NOT NULL GROUP BY pay_bank"):
        if r["pay_bank"] in counts:
            counts[r["pay_bank"]] = r["n"]
    bank = min(banks, key=lambda b: counts[b])  # при равенстве — первый по списку
    await db.execute("UPDATE users SET pay_bank=? WHERE user_id=?", bank, uid)
    return bank


async def create(uid: int, tariff: str, kind: str, price: int, promo: str | None) -> int:
    """Заявка на оплату; если уже есть открытая на тот же тариф — возвращает её."""
    same = await db.fetchone(
        "SELECT id FROM payments WHERE user_id=? AND provider='manual' AND status='pending' "
        "AND tariff=? AND kind=? AND amount=? AND created_at>? ORDER BY id DESC",
        uid, tariff, kind, price, now() - config.MANUAL_PAY_TTL_HOURS * 3600)
    bank = await bank_for(uid)
    if same:
        await db.execute("UPDATE payments SET bank=? WHERE id=?", bank, same["id"])
        return same["id"]
    pid = await S.create_payment(uid, "manual", tariff, kind, price, promo=promo)
    await db.execute("UPDATE payments SET bank=? WHERE id=?", bank, pid)
    return pid


def instructions(pay):
    gift = " (подарок)" if pay["kind"] == "gift" else ""
    lines = [
        f"<b>{S.tariff_title(pay['tariff'])}{gift}</b>\n",
        f"Переведи <b>{pay['amount']} ₽</b> по СБП на номер <code>{config.MANUAL_PAY_PHONE}</code>",
    ]
    if pay["bank"]:
        lines.append(f"Банк получателя: <b>{html.escape(pay['bank'])}</b> — выбери его при переводе")
    if config.MANUAL_PAY_NAME:
        lines.append(f"Получатель: {html.escape(config.MANUAL_PAY_NAME)}")
    lines.append("\nПосле перевода нажми «Я перевёл» — доступ включится сразу.")
    markup = kb(
        [InlineKeyboardButton(text="📋 Скопировать номер", copy_text=CopyTextButton(text=config.MANUAL_PAY_PHONE))],
        [btn("✅ Я перевёл", f"mp_paid:{pay['id']}")],
        [btn("Отмена", f"mp_cancel:{pay['id']}")],
    )
    return "\n".join(lines), markup


async def notify_owner(bot: Bot, pay_id: int, by_admin: bool = False):
    pay = await db.fetchone("SELECT * FROM payments WHERE id=?", pay_id)
    u = await db.get_user(pay["user_id"])
    s = await db.get_sub(pay["user_id"])
    uname = f" @{u['username']}" if u and u["username"] else ""
    name = html.escape(S.person(u))
    promo = f" · промокод {pay['promo']}" if pay["promo"] else ""
    if pay["kind"] == "gift":
        what = f"🎁 Подарок «{S.tariff_title(pay['tariff'])}»"
    else:
        what = f"{S.tariff_title(pay['tariff'])}, доступ до {fmt_date(s['expires_at'])}" if s else S.tariff_title(pay["tariff"])
    head = "Занёс оплату" if by_admin else "Новая оплата переводом"
    bank = f" → {html.escape(pay['bank'])}" if pay["bank"] else ""
    await S.safe_send(
        bot, config.MANUAL_PAY_CHAT_ID,
        f"💸 <b>{head}</b> — занесено в базу\n\n"
        f"{name}{uname} · <code>{pay['user_id']}</code>\n"
        f"{pay['amount']} ₽{bank} · {what}{promo}",
        reply_markup=kb([btn("↩️ Отменить", f"mp_undo:{pay_id}")]),
    )


async def mark_paid(bot: Bot, pay_id: int, by_admin: bool = False) -> bool:
    """Заносит оплату и включает доступ. False — заявка уже обработана."""
    pay = await db.fetchone("SELECT status FROM payments WHERE id=?", pay_id)
    if not pay or pay["status"] != "pending":
        return False
    await S.on_paid(bot, pay_id)
    await notify_owner(bot, pay_id, by_admin)
    return True


async def undo(bot: Bot, pay_id: int) -> bool:
    """Отменяет ошибочно занесённую оплату: снимает оплаченные дни и, если срок кончился, отключает."""
    cur = await db.execute(
        "UPDATE payments SET status='canceled' WHERE id=? AND provider='manual' AND status='succeeded'", pay_id)
    if not cur.rowcount:
        return False
    pay = await db.fetchone("SELECT * FROM payments WHERE id=?", pay_id)
    uid = pay["user_id"]
    if pay["promo"]:
        await db.execute("UPDATE promos SET uses=MAX(uses-1,0) WHERE code=?", pay["promo"])

    if pay["kind"] == "gift":
        await db.execute("DELETE FROM gifts WHERE buyer_id=? AND tariff=? AND used_by IS NULL "
                         "AND created_at>=?", uid, pay["tariff"], pay["paid_at"] or 0)
    else:
        days = config.TARIFFS[pay["tariff"]]["days"]
        await db.execute("UPDATE subs SET expires_at=expires_at-? WHERE user_id=?", days * DAY, uid)
        s = await db.get_sub(uid)
        if s and s["status"] == "active" and s["expires_at"] <= now():
            await S.kick(bot, uid)
            await db.execute("UPDATE subs SET status='expired' WHERE user_id=?", uid)

    await db.event(uid, "payment_undone", str(pay_id))
    await S.safe_send(bot, uid, "Оплата не найдена, доступ по ней отменён. Если это ошибка — напиши админу.",
                      reply_markup=kb([btn("✉️ Написать админу", "support")]))
    return True


async def expire_stale():
    await db.execute(
        "UPDATE payments SET status='expired' WHERE provider='manual' AND status='pending' AND created_at<=?",
        now() - config.MANUAL_PAY_TTL_HOURS * 3600)
