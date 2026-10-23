import logging
import math
import secrets
import uuid
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import db
from db import now

log = logging.getLogger(__name__)
DAY = 86400

SPECIAL_TARIFFS = {
    "trial": "пробный",
    "manual": "выдан админом",
    "gift": "подарок",
    "bonus": "бонус за друга",
}


# ---------- мелочи ----------

def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, config.TZ).strftime("%d.%m.%Y")


def btn(text, data=None, url=None):
    if url:
        return InlineKeyboardButton(text=text, url=url)
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def person(u) -> str:
    """Как показывать человека владельцу: ФИО и группа, если введены, иначе имя из Telegram."""
    if not u:
        return "—"
    if u["fio"]:
        grp = f", {u['study_group']}" if u["study_group"] else ""
        return f"{u['fio']}{grp}"
    return u["full_name"] or "—"


def tariff_title(key: str) -> str:
    if key in config.TARIFFS:
        return config.TARIFFS[key]["title"]
    return SPECIAL_TARIFFS.get(key, key or "—")


async def safe_send(bot: Bot, uid: int, text: str, **kw):
    try:
        return await bot.send_message(uid, text, **kw)
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        log.info("can't message %s: %s", uid, e)
        return None


_username = None


async def bot_username(bot: Bot) -> str:
    global _username
    if not _username:
        _username = (await bot.get_me()).username
    return _username


# ---------- промокоды ----------

async def valid_promo(code: str, uid: int):
    p = await db.fetchone("SELECT * FROM promos WHERE code=?", code.strip().upper())
    if not p:
        return None
    if p["expires_at"] and p["expires_at"] < now():
        return None
    if p["max_uses"] and p["uses"] >= p["max_uses"]:
        return None
    if p["user_id"] and p["user_id"] != uid:
        return None
    return p


async def user_discount(uid: int):
    """(код, процент) промокода, который висит на пользователе, или (None, 0)."""
    u = await db.get_user(uid)
    if u and u["pending_promo"]:
        p = await valid_promo(u["pending_promo"], uid)
        if p:
            return p["code"], p["percent"]
        await db.execute("UPDATE users SET pending_promo=NULL WHERE user_id=?", uid)
    return None, 0


def apply_discount(amount: int, percent: int) -> int:
    return max(1, round(amount * (100 - percent) / 100))


# ---------- подписка ----------

async def is_active(uid: int) -> bool:
    s = await db.get_sub(uid)
    return bool(s and s["status"] == "active" and s["expires_at"] > now())


async def has_access(uid: int) -> bool:
    return uid in config.ADMIN_IDS or await is_active(uid)


async def in_channel(bot: Bot, uid: int) -> bool:
    try:
        m = await bot.get_chat_member(config.CHANNEL_ID, uid)
        return m.status in ("member", "administrator", "creator", "restricted")
    except TelegramBadRequest:
        return False


async def channel_link(bot: Bot) -> str | None:
    """Общая ссылка-заявка в канал. По ней нельзя войти напрямую: каждую заявку бот проверяет
    и одобряет только тем, у кого активна подписка. Поэтому ссылку не страшно слить."""
    if not config.CHANNEL_ID:
        return None
    saved = await db.get_setting("channel_link")
    if saved and await db.get_setting("channel_link_for") == str(config.CHANNEL_ID):
        return saved
    try:
        link = await bot.create_chat_invite_link(config.CHANNEL_ID, name="Подписчики (через бота)",
                                                 creates_join_request=True)
    except TelegramBadRequest as e:
        log.error("can't create channel link: %s", e)
        return None
    await db.set_setting("channel_link", link.invite_link)
    await db.set_setting("channel_link_for", config.CHANNEL_ID)
    return link.invite_link


async def send_invite(bot: Bot, uid: int, force: bool = False) -> bool:
    """Шлёт кнопку входа в канал. Без force — только тем, кто ещё не в канале."""
    if not config.CHANNEL_ID:
        return False
    member = await in_channel(bot, uid)
    if member and not force:
        return False
    link = await channel_link(bot)
    if not link:
        await safe_send(bot, uid, "Не получилось создать ссылку в канал — напиши админу, он поможет.")
        return False
    text = ("Ты уже в канале 👌 Кнопка ниже его откроет." if member else
            "Вход в канал с материалами 👇\nНажми кнопку и отправь заявку — бот примет её сам за пару секунд.")
    await safe_send(bot, uid, text, reply_markup=kb([btn("📚 Открыть канал", url=link)]))
    return True


async def kick(bot: Bot, uid: int):
    if not config.CHANNEL_ID or uid in config.ADMIN_IDS:
        return
    try:
        m = await bot.get_chat_member(config.CHANNEL_ID, uid)
        if m.status in ("member", "restricted"):
            await bot.ban_chat_member(config.CHANNEL_ID, uid)
            await bot.unban_chat_member(config.CHANNEL_ID, uid, only_if_banned=True)
    except TelegramBadRequest as e:
        log.warning("kick %s failed: %s", uid, e)
    s = await db.get_sub(uid)
    if s and s["invite_link"]:
        try:
            await bot.revoke_chat_invite_link(config.CHANNEL_ID, s["invite_link"])
        except TelegramBadRequest:
            pass


async def extend(bot: Bot, uid: int, days: int, *, tariff=None, method=None,
                 default="manual", autorenew=None, text=None) -> int:
    """Продлевает доступ на days дней (от конца текущего срока, если он ещё идёт).
    tariff/method перезаписываются только если переданы: бонусы и подарки не ломают
    тариф и способ оплаты основной подписки. default — что ставить новой подписке."""
    t = now()
    s = await db.get_sub(uid)
    active = bool(s and s["status"] == "active" and s["expires_at"] > t)
    new_exp = (s["expires_at"] if active else t) + days * DAY

    if not s:
        await db.execute(
            "INSERT INTO subs(user_id,status,tariff,method,started_at,expires_at,autorenew) "
            "VALUES(?,?,?,?,?,?,?)",
            uid, "active", tariff or default, method or default, t, new_exp, int(bool(autorenew)),
        )
    else:
        sets = {"status": "active", "expires_at": new_exp, "reminded3": 0, "reminded1": 0}
        if not active:
            sets.update(started_at=t, tariff=tariff or default, method=method or default,
                        autorenew=int(bool(autorenew)))
        else:
            if tariff:
                sets["tariff"] = tariff
            if method:
                sets["method"] = method
            if autorenew is not None:
                sets["autorenew"] = int(autorenew)
        cols = ", ".join(f"{k}=?" for k in sets)
        await db.execute(f"UPDATE subs SET {cols} WHERE user_id=?", *sets.values(), uid)

    if text:
        await safe_send(bot, uid, f"{text}\nДоступ до <b>{fmt_date(new_exp)}</b>.")
    await send_invite(bot, uid)
    return new_exp


async def make_winback(uid: int) -> str:
    code = "BACK" + secrets.token_hex(3).upper()
    await db.execute(
        "INSERT INTO promos(code,percent,max_uses,expires_at,user_id,created_at) VALUES(?,?,?,?,?,?)",
        code, config.WINBACK_PERCENT, 1, now() + config.WINBACK_DAYS * DAY, uid, now(),
    )
    await db.execute("UPDATE users SET pending_promo=? WHERE user_id=?", code, uid)
    return code


async def expire(bot: Bot, s):
    uid = s["user_id"]
    await kick(bot, uid)
    await db.execute("UPDATE subs SET status='expired', autorenew=0 WHERE user_id=?", uid)
    await db.event(uid, "expired", s["method"])

    # скидка «вернись» — один раз на человека, иначе люди привыкнут ждать её каждый месяц
    already = await db.scalar("SELECT COUNT(*) FROM promos WHERE user_id=? AND code LIKE 'BACK%'", uid)
    offer = ""
    if config.WINBACK_PERCENT and not already:
        await make_winback(uid)
        verb = "оформишь подписку" if s["method"] == "trial" else "продлишь"
        offer = (f"\n\nЕсли {verb} в течение {config.WINBACK_DAYS} дн. — скидка "
                 f"{config.WINBACK_PERCENT}%, она уже применена.")
    head = "Пробный доступ закончился." if s["method"] == "trial" else "Подписка закончилась, доступ к материалам закрыт."
    await safe_send(bot, uid, head + offer, reply_markup=kb([btn("Оформить подписку", "buy:sub")]))


# ---------- оплата (пока только тестовая) ----------

async def create_payment(uid: int, provider: str, tariff: str, kind: str, amount: int,
                         currency="RUB", promo=None, ext_id=None) -> int:
    cur = await db.execute(
        "INSERT INTO payments(user_id,provider,ext_id,amount,currency,tariff,kind,promo,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        uid, provider, ext_id or f"{provider}-{uuid.uuid4().hex}", amount, currency, tariff, kind, promo,
        "pending", now(),
    )
    return cur.lastrowid


async def on_paid(bot: Bot, payment_id: int, *, autorenew=False):
    """Единая точка «деньги пришли». Сюда потом подключатся ЮKassa и звёзды."""
    cur = await db.execute(
        "UPDATE payments SET status='succeeded', paid_at=? WHERE id=? AND status!='succeeded'",
        now(), payment_id,
    )
    if not cur.rowcount:
        return  # уже обработан
    pay = await db.fetchone("SELECT * FROM payments WHERE id=?", payment_id)
    uid, tariff, kind = pay["user_id"], pay["tariff"], pay["kind"]

    if pay["promo"]:
        await db.execute("UPDATE promos SET uses=uses+1 WHERE code=?", pay["promo"])
        await db.execute("UPDATE users SET pending_promo=NULL WHERE user_id=? AND pending_promo=?", uid, pay["promo"])

    if kind == "gift":
        code = "GIFT-" + secrets.token_hex(4).upper()
        await db.execute("INSERT INTO gifts(code,tariff,buyer_id,created_at) VALUES(?,?,?,?)",
                         code, tariff, uid, now())
        link = f"https://t.me/{await bot_username(bot)}?start=g_{code}"
        await safe_send(
            bot, uid,
            f"🎁 Подарок готов: {tariff_title(tariff)}.\n\nПерешли эту ссылку тому, кому даришь — "
            f"он откроет бота и доступ включится сам:\n{link}",
        )
        await db.event(uid, "gift_bought", tariff)
        return

    if kind == "renew":
        title = "🔄 Подписка продлена."
    elif pay["provider"] == "manual":
        title = "✅ Оплата учтена, подписка активна."
    else:
        title = "✅ Оплата прошла, подписка активна."
    await extend(bot, uid, config.TARIFFS[tariff]["days"], tariff=tariff, method=pay["provider"],
                 autorenew=autorenew, text=title)
    await db.event(uid, kind, f"{pay['amount']} {pay['currency']}")
    await reward_referrer(bot, uid)


async def course_end_ts() -> int:
    """Конец курса — 23:59 по Москве указанного дня."""
    raw = await db.get_setting("course_end", config.COURSE_END)
    d = datetime.strptime(raw, "%d.%m.%Y").replace(hour=23, minute=59, tzinfo=config.TZ)
    return int(d.timestamp())


async def reward_referrer(bot: Bot, uid: int):
    u = await db.get_user(uid)
    if not u or not u["referrer_id"] or u["ref_rewarded"]:
        return
    await db.execute("UPDATE users SET ref_rewarded=1 WHERE user_id=?", uid)
    ref = u["referrer_id"]
    count = await db.scalar("SELECT COUNT(*) FROM users WHERE referrer_id=? AND ref_rewarded=1", ref)

    if config.REF_BIG_EVERY and count % config.REF_BIG_EVERY == 0:
        s = await db.get_sub(ref)
        base = s["expires_at"] if s and s["status"] == "active" and s["expires_at"] > now() else now()
        end = await course_end_ts()
        days = math.ceil((end - base) / DAY)
        if days > 0:
            await extend(bot, ref, days, default="bonus")
            await db.execute("UPDATE subs SET expires_at=? WHERE user_id=?", end, ref)  # ровно до конца курса
            await safe_send(bot, ref, f"🏆 Уже {count} друзей оформили подписку по твоей ссылке! "
                                      f"Дарим доступ до конца курса — до <b>{fmt_date(end)}</b>.")
            await db.event(ref, "ref_big_bonus", str(uid))
            return
    left = config.REF_BIG_EVERY - count % config.REF_BIG_EVERY if config.REF_BIG_EVERY else 0
    more = (f"\nЕщё {left} — и получишь доступ до конца курса." if left else "")
    await extend(bot, ref, config.REF_BONUS_DAYS, default="bonus",
                 text=f"🎉 Друг оформил подписку по твоей ссылке — держи +{config.REF_BONUS_DAYS} дней.{more}")
    await db.event(ref, "ref_bonus", str(uid))


async def activate_gift(bot: Bot, uid: int, code: str) -> str | None:
    """Возвращает текст ошибки или None, если всё ок."""
    code = code.strip().upper()
    g = await db.fetchone("SELECT * FROM gifts WHERE code=?", code)
    if not g:
        return "Такого подарочного кода нет."
    cur = await db.execute("UPDATE gifts SET used_by=?, used_at=? WHERE code=? AND used_by IS NULL",
                           uid, now(), code)
    if not cur.rowcount:
        return "Этот подарок уже активировали."
    await extend(bot, uid, config.TARIFFS[g["tariff"]]["days"], default="gift",
                 text=f"🎁 Тебе подарили подписку: {tariff_title(g['tariff'])}.")
    await db.event(uid, "gift_used", code)
    if g["buyer_id"] != uid:
        await safe_send(bot, g["buyer_id"], "🎁 Твой подарок только что активировали.")
    return None
