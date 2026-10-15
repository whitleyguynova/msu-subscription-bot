import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatJoinRequest, KeyboardButton, Message, ReplyKeyboardMarkup

import config
import db
import manual_pay
import materials
import services as S
from db import now
from services import btn, fmt_date, kb

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.chat.type == "private")

M_MAT, M_SUB, M_GIFT = "📚 Материалы", "💳 Подписка", "🎁 Подарить"
M_REF, M_FAQ, M_SUP = "👥 Пригласить друга", "❓ Вопросы", "✉️ Написать админу"
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=M_MAT), KeyboardButton(text=M_SUB)],
        [KeyboardButton(text=M_GIFT), KeyboardButton(text=M_REF)],
        [KeyboardButton(text=M_FAQ), KeyboardButton(text=M_SUP)],
    ],
    resize_keyboard=True,
)


class Support(StatesGroup):
    waiting = State()


class PromoInput(StatesGroup):
    waiting = State()



async def show(cb: CallbackQuery, text, markup=None):
    """Редактирует сообщение с кнопками, а если нельзя — шлёт новое."""
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=markup)


# ---------- старт ----------

async def register(u, arg: str):
    if await db.get_user(u.id):
        await db.execute("UPDATE users SET username=?, full_name=? WHERE user_id=?", u.username, u.full_name, u.id)
        return
    source = referrer = None
    if arg.startswith("r_") and arg[2:].isdigit():
        ref = int(arg[2:])
        if ref != u.id and await db.get_user(ref):
            referrer, source = ref, "ref"
    elif arg.startswith("src_"):
        source = arg[4:40]
    elif arg.startswith("p_"):
        source = arg[2:40].upper()
    await db.execute(
        "INSERT INTO users(user_id,username,full_name,created_at,source,referrer_id) VALUES(?,?,?,?,?,?)",
        u.id, u.username, u.full_name, now(), source, referrer,
    )
    await db.event(u.id, "start", source)


@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, bot: Bot, state: FSMContext):
    await state.clear()
    arg = command.args or ""
    await register(m.from_user, arg)
    welcome = config.WELCOME_CHANNEL if config.CHANNEL_ID else config.WELCOME
    await m.answer(welcome.format(title=config.PROJECT_TITLE), reply_markup=MAIN_KB)

    if arg.startswith("g_"):
        err = await S.activate_gift(bot, m.from_user.id, arg[2:])
        if err:
            await m.answer(err)
        return
    if arg.startswith("p_"):
        p = await S.valid_promo(arg[2:], m.from_user.id)
        if p:
            await db.execute("UPDATE users SET pending_promo=? WHERE user_id=?", p["code"], m.from_user.id)
            await m.answer(f"🏷 Промокод <b>{p['code']}</b> применён: −{p['percent']}% на оплату.")
    text, markup = await sub_screen(m.from_user.id)
    await m.answer(text, reply_markup=markup)


# ---------- подписка ----------

async def trial_ok(uid: int) -> bool:
    if not config.TRIAL_DAYS:
        return False
    u, s = await db.get_user(uid), await db.get_sub(uid)
    return bool(u and not u["trial_used"] and not s)


async def tariffs_screen(uid: int, kind: str):
    code, pct = await S.user_discount(uid) if kind == "sub" else (None, 0)
    lines, rows = [], []
    if kind == "gift":
        lines.append("🎁 Подписка в подарок. После оплаты пришлю ссылку — перешли её, "
                     "и доступ включится у получателя сам.\n")
    for key, t in config.TARIFFS.items():
        rub = S.apply_discount(t["rub"], pct)
        price = f"<s>{t['rub']}</s> {rub} ₽" if pct else f"{rub} ₽"
        lines.append(f"• {t['title']} — {price}")
        rows.append([btn(f"{t['title']} — {rub} ₽", f"tar:{kind}:{key}")])
    if pct:
        lines.append(f"\n🏷 Промокод <b>{code}</b>: −{pct}%")
    if kind == "sub":
        if await trial_ok(uid):
            days = "1 день" if config.TRIAL_DAYS == 1 else f"{config.TRIAL_DAYS} дн."
            rows.append([btn(f"🆓 Попробовать бесплатно ({days})", "trial")])
        rows.append([btn("🏷 Ввести промокод", "promo")])
    return "\n".join(lines), kb(*rows)


async def sub_screen(uid: int):
    s = await db.get_sub(uid)
    if s and s["status"] == "active" and s["expires_at"] > now():
        text = (f"✅ Подписка активна до <b>{fmt_date(s['expires_at'])}</b>\n"
                f"Тариф: {S.tariff_title(s['tariff'])}")
        open_btn = "📚 Открыть канал" if config.CHANNEL_ID else "📚 Открыть материалы"
        rows = [[btn(open_btn, "catalog")], [btn("Продлить заранее", "buy:sub")]]
        return text, kb(*rows)
    head = "Подписка закончилась. Продлить:" if s else "Доступ ко всем материалам по подписке:"
    text, markup = await tariffs_screen(uid, "sub")
    return f"{head}\n\n{text}", markup


@router.message(F.text == M_SUB)
@router.message(Command("sub"))
async def menu_sub(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await sub_screen(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data == "subs")
async def cb_subs(cb: CallbackQuery):
    text, markup = await sub_screen(cb.from_user.id)
    await show(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery):
    kind = cb.data.split(":")[1]
    text, markup = await tariffs_screen(cb.from_user.id, kind)
    await show(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data.startswith("tar:"))
async def cb_tariff(cb: CallbackQuery):
    _, kind, key = cb.data.split(":")
    t = config.TARIFFS[key]
    _, pct = await S.user_discount(cb.from_user.id) if kind == "sub" else (None, 0)
    rub = S.apply_discount(t["rub"], pct)
    rows = []
    if manual_pay.enabled():
        rows.append([btn(f"🏦 Перевод по СБП — {rub} ₽", f"pay:{kind}:{key}:manual")])
    if config.TEST_PAYMENTS:
        rows.append([btn(f"🧪 Тестовая оплата — {rub} ₽", f"pay:{kind}:{key}:test")])
    rows.append([btn("← Назад", f"buy:{kind}")])
    gift = " в подарок" if kind == "gift" else ""
    no_methods = not manual_pay.enabled() and not config.TEST_PAYMENTS
    note = "\n\nОплата скоро подключится. Пока можно написать админу." if no_methods else ""
    await show(cb, f"<b>{t['title']}</b>{gift} — {rub} ₽{note}", kb(*rows))
    await cb.answer()


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(cb: CallbackQuery, bot: Bot):
    _, kind, key, method = cb.data.split(":")
    uid = cb.from_user.id
    code, pct = await S.user_discount(uid) if kind == "sub" else (None, 0)
    amount = S.apply_discount(config.TARIFFS[key]["rub"], pct)

    if method == "manual" and manual_pay.enabled():
        pid = await manual_pay.create(uid, key, kind, amount, code)
        pay = await db.fetchone("SELECT * FROM payments WHERE id=?", pid)
        text, markup = manual_pay.instructions(pay)
        await show(cb, text, markup)
        return await cb.answer()

    if method != "test" or not config.TEST_PAYMENTS:
        return await cb.answer("Этот способ оплаты пока не подключён", show_alert=True)
    pid = await S.create_payment(uid, "test", key, kind, amount, promo=code)
    await S.on_paid(bot, pid)
    await cb.answer("Тестовая оплата прошла")
    try:
        await cb.message.delete()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "trial")
async def cb_trial(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    if not await trial_ok(uid):
        msg = "Пробный доступ уже был использован" if config.TRIAL_DAYS else "Пробного доступа нет"
        return await cb.answer(msg, show_alert=True)
    await db.execute("UPDATE users SET trial_used=1 WHERE user_id=?", uid)
    await S.extend(bot, uid, config.TRIAL_DAYS, tariff="trial", method="trial", autorenew=False)
    await db.event(uid, "trial")
    if config.CHANNEL_ID:
        # кнопку входа в канал extend() уже прислала отдельным сообщением
        days = "1 день" if config.TRIAL_DAYS == 1 else f"{config.TRIAL_DAYS} дн."
        await show(cb, f"🆓 Пробный доступ на {days} включён. Кнопка входа в канал — в сообщении ниже 👇\n\n"
                       "Когда пробный период закончится, бот уберёт тебя из канала. Продлить можно в «💳 Подписка».")
    else:
        limit = (f"\nНа пробе можно открыть {config.TRIAL_SUBJECTS_LIMIT} предм." if config.TRIAL_SUBJECTS_LIMIT else "")
        await show(cb, f"🆓 Пробный доступ на {config.TRIAL_DAYS} дн. включён.{limit}",
                   kb([btn("📚 Открыть материалы", "catalog")]))
    await cb.answer()


@router.callback_query(F.data == "promo")
async def cb_promo(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PromoInput.waiting)
    await show(cb, "Пришли промокод сообщением.", kb([btn("Отмена", "subs")]))
    await cb.answer()


@router.callback_query(F.data == "invite")
async def cb_invite(cb: CallbackQuery, bot: Bot):
    if not await S.has_access(cb.from_user.id):
        return await cb.answer("Подписка не активна", show_alert=True)
    await S.send_invite(bot, cb.from_user.id, force=True)
    await cb.answer()


# ---------- ручной перевод по СБП ----------

async def own_payment(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    return await db.fetchone("SELECT * FROM payments WHERE id=? AND user_id=? AND provider='manual'",
                             pid, cb.from_user.id)


@router.callback_query(F.data.startswith("mp_paid:"))
async def cb_mp_paid(cb: CallbackQuery, bot: Bot):
    pay = await own_payment(cb)
    if not pay:
        return await cb.answer()
    if not await manual_pay.mark_paid(bot, pay["id"]):
        return await cb.answer("Эта оплата уже учтена", show_alert=True)
    try:
        await cb.message.delete()
    except TelegramBadRequest:
        pass
    await cb.answer("Готово")


@router.callback_query(F.data.startswith("mp_cancel:"))
async def cb_mp_cancel(cb: CallbackQuery):
    pay = await own_payment(cb)
    if pay:
        await db.execute("UPDATE payments SET status='expired' WHERE id=? AND status='pending'", pay["id"])
    await show(cb, "Окей, отменил. Если передумаешь — «💳 Подписка».")
    await cb.answer()


# ---------- материалы ----------

async def catalog_view():
    rows = await db.fetch(
        "SELECT s.id, s.name, COUNT(m.id) n FROM subjects s LEFT JOIN materials m ON m.subject_id=s.id "
        "GROUP BY s.id ORDER BY s.position, s.name"
    )
    rows = [r for r in rows if r["n"]]
    if not rows:
        return "Материалы скоро появятся.", None
    buttons = [[btn(f"{r['name']} · {r['n']}", f"cat:{r['id']}")] for r in rows]
    return "Выбери предмет — пришлю все материалы одним сообщением, по темам:", kb(*buttons)


async def channel_view(bot: Bot, uid: int):
    """«Материалы» в режиме канала: подписчику — кнопка входа, остальным — тарифы."""
    if not await S.has_access(uid):
        text, markup = await tariffs_screen(uid, "sub")
        return f"📚 Все материалы лежат в закрытом канале. Доступ — по подписке:\n\n{text}", markup
    link = await S.channel_link(bot)
    if not link:
        return "Канал сейчас недоступен — напиши админу, он поможет.", None
    if await S.in_channel(bot, uid):
        return "📚 Ты уже в канале, все материалы там.", kb([btn("Открыть канал", url=link)])
    return ("📚 Все материалы — в закрытом канале.\n"
            "Нажми кнопку и отправь заявку, бот примет её сам за пару секунд.",
            kb([btn("Открыть канал", url=link)]))


@router.message(F.text == M_MAT)
@router.message(Command("materials"))
async def menu_materials(m: Message, bot: Bot, state: FSMContext):
    await state.clear()
    if config.CHANNEL_ID:
        text, markup = await channel_view(bot, m.from_user.id)
    else:
        text, markup = await catalog_view()
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data == "catalog")
async def cb_catalog(cb: CallbackQuery, bot: Bot):
    if config.CHANNEL_ID:
        text, markup = await channel_view(bot, cb.from_user.id)
    else:
        text, markup = await catalog_view()
    await show(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data.startswith("cat:"))
async def cb_subject(cb: CallbackQuery, bot: Bot):
    uid, sid = cb.from_user.id, int(cb.data[4:])
    subj = await db.fetchone("SELECT * FROM subjects WHERE id=?", sid)
    if not subj:
        return await cb.answer("Предмет не найден", show_alert=True)

    if not await S.has_access(uid):
        topics = await materials.topics_preview(sid)
        lines = "\n".join(f"• {html.escape(t)} · {n}" for t, n in topics[:40])
        await show(cb, f"📘 <b>{html.escape(subj['name'])}</b>\n\n{lines}\n\nФайлы открываются по подписке.",
                   kb([btn("Оформить подписку", "buy:sub")], [btn("← Предметы", "catalog")]))
        return await cb.answer()

    s = await db.get_sub(uid)
    if s and s["method"] == "trial" and config.TRIAL_SUBJECTS_LIMIT and uid not in config.ADMIN_IDS:
        opened = await db.fetch("SELECT DISTINCT subject_id FROM opens WHERE user_id=? AND at>=?",
                                uid, s["started_at"])
        opened = {r["subject_id"] for r in opened}
        if sid not in opened and len(opened) >= config.TRIAL_SUBJECTS_LIMIT:
            return await cb.answer(
                f"На пробе можно открыть {config.TRIAL_SUBJECTS_LIMIT} предм. "
                "Оформи подписку, чтобы получить все.", show_alert=True)

    await cb.answer("Собираю материалы…")
    n = await materials.send_subject(bot, uid, subj)
    if n:
        await db.execute("INSERT INTO opens(user_id,subject_id,at) VALUES(?,?,?)", uid, sid, now())


# ---------- подарок, рефералка, вопросы ----------

@router.message(F.text == M_GIFT)
async def menu_gift(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await tariffs_screen(m.from_user.id, "gift")
    await m.answer(text, reply_markup=markup)


@router.message(F.text == M_REF)
async def menu_ref(m: Message, bot: Bot, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    link = f"https://t.me/{await S.bot_username(bot)}?start=r_{uid}"
    invited = await db.scalar("SELECT COUNT(*) FROM users WHERE referrer_id=?", uid)
    paid = await db.scalar("SELECT COUNT(*) FROM users WHERE referrer_id=? AND ref_rewarded=1", uid)
    n = config.REF_BIG_EVERY
    big = ""
    if n:
        left = n - paid % n
        big = (f"\n🏆 Каждый {n}-й оплативший друг — <b>доступ до конца курса</b> "
               f"({fmt_date(await S.course_end_ts())}). До него осталось: {left}.")
    await m.answer(
        f"Позови друга: когда он оплатит подписку, тебе <b>+{config.REF_BONUS_DAYS} дней</b> бесплатно.{big}\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        f"Пришли по ссылке: {invited}\nОплатили: {paid}"
    )


@router.message(F.text == M_FAQ)
async def menu_faq(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Частые вопросы:", reply_markup=faq_kb())


def faq_list():
    return config.FAQ_CHANNEL if config.CHANNEL_ID else config.FAQ


def faq_kb():
    return kb(*[[btn(q, f"faq:{i}")] for i, (q, _) in enumerate(faq_list())],
              [btn("✉️ Написать админу", "support")])


@router.callback_query(F.data == "faq")
async def cb_faq_list(cb: CallbackQuery):
    await show(cb, "Частые вопросы:", faq_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("faq:"))
async def cb_faq(cb: CallbackQuery):
    q, a = faq_list()[int(cb.data[4:])]
    await show(cb, f"<b>{q}</b>\n\n{a}", kb([btn("← Все вопросы", "faq")]))
    await cb.answer()


# ---------- связь с админом ----------

SUPPORT_PROMPT = "Напиши вопрос одним сообщением — можно с фото или файлом. Ответ придёт сюда."


@router.message(F.text == M_SUP)
async def menu_support(m: Message, state: FSMContext):
    await state.set_state(Support.waiting)
    await m.answer(SUPPORT_PROMPT, reply_markup=kb([btn("Отмена", "sup_cancel")]))


@router.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Support.waiting)
    await show(cb, SUPPORT_PROMPT, kb([btn("Отмена", "sup_cancel")]))
    await cb.answer()


@router.callback_query(F.data == "sup_cancel")
async def cb_sup_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "Окей, отменил.")
    await cb.answer()


@router.chat_join_request()
async def on_join_request(req: ChatJoinRequest, bot: Bot):
    if not config.CHANNEL_ID or req.chat.id != config.CHANNEL_ID:
        return
    uid = req.from_user.id
    # Telegram разрешает написать человеку из заявки, даже если он не запускал бота, —
    # но только пока заявка не обработана. Поэтому сначала сообщение, потом решение.
    chat = req.user_chat_id or uid
    if await S.has_access(uid):
        await S.safe_send(bot, chat, "✅ Заявка принята — добро пожаловать в канал!")
        await req.approve()
        await db.event(uid, "joined_channel")
    else:
        if not await db.get_user(uid):
            await db.execute("INSERT INTO users(user_id,username,full_name,created_at,source) VALUES(?,?,?,?,?)",
                             uid, req.from_user.username, req.from_user.full_name, now(), "channel_link")
        await S.safe_send(bot, chat, "Заявку в канал отклонил: подписка не активна. Оформить её можно здесь 👇",
                          reply_markup=kb([btn("Оформить подписку", "buy:sub")]))
        await req.decline()


# Эти два хендлера ловят «любое сообщение в состоянии», поэтому стоят последними:
# кнопки меню и команды выше перехватываются раньше.

@router.message(PromoInput.waiting)
async def promo_input(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    p = await S.valid_promo(m.text or "", uid)
    if p:
        await db.execute("UPDATE users SET pending_promo=? WHERE user_id=?", p["code"], uid)
        head = f"🏷 Промокод применён: −{p['percent']}%."
    else:
        head = "Такого промокода нет или он закончился."
    text, markup = await tariffs_screen(uid, "sub")
    await m.answer(f"{head}\n\n{text}", reply_markup=markup)


@router.message(Support.waiting)
async def support_message(m: Message, bot: Bot, state: FSMContext):
    if m.text and m.text.startswith("/"):
        await state.clear()
        return await m.answer("Окей, отменил.")
    await state.clear()
    u = m.from_user
    s = await db.get_sub(u.id)
    status = (f"активна до {fmt_date(s['expires_at'])}"
              if s and s["status"] == "active" and s["expires_at"] > now() else "нет")
    uname = f" @{u.username}" if u.username else ""
    head = await bot.send_message(
        config.SUPPORT_CHAT_ID,
        f"✉️ <b>{html.escape(u.full_name)}</b>{uname} · <code>{u.id}</code>\nПодписка: {status}\n"
        f"<i>Ответь реплаем — ответ уйдёт человеку</i>",
    )
    copy = await m.copy_to(config.SUPPORT_CHAT_ID)
    await db.execute("INSERT OR REPLACE INTO support(admin_msg_id,user_id) VALUES(?,?)", head.message_id, u.id)
    await db.execute("INSERT OR REPLACE INTO support(admin_msg_id,user_id) VALUES(?,?)", copy.message_id, u.id)
    await m.answer("Отправил ✅ Ответ придёт сюда.")
