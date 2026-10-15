"""Фоновые задачи: напоминания о конце подписки и отключение тех, у кого она закончилась."""
import asyncio
import logging
import math

from aiogram import Bot

import config
import db
import manual_pay
import services as S
from db import now

log = logging.getLogger(__name__)


async def run(bot: Bot):
    while True:
        try:
            await tick(bot)
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(config.TICK_SECONDS)


async def tick(bot: Bot):
    t = now()
    await remind(bot, t)
    await manual_pay.expire_stale()
    expired = await db.fetch("SELECT * FROM subs WHERE status='active' AND expires_at<=?", t)
    for s in expired:
        await S.expire(bot, s)


async def remind(bot: Bot, t: int):
    for days in config.REMIND_DAYS:
        col = f"reminded{days}"
        rows = await db.fetch(
            f"SELECT * FROM subs WHERE status='active' AND method!='trial' AND {col}=0 "
            f"AND expires_at>? AND expires_at-?<=?", t, t, days * 86400)
        for s in rows:
            left = max(1, math.ceil((s["expires_at"] - t) / 86400))
            # чтобы не прислать два напоминания подряд, гасим и более поздние
            await db.execute(
                "UPDATE subs SET " + ", ".join(f"reminded{d}=1" for d in config.REMIND_DAYS if d >= left or d == days)
                + " WHERE user_id=?", s["user_id"])
            if s["autorenew"]:
                continue  # для автопродления будет своё уведомление, когда подключим оплату
            word = "день" if left == 1 else "дня" if left < 5 else "дней"
            await S.safe_send(
                bot, s["user_id"],
                f"⏳ Подписка закончится через {left} {word} — {S.fmt_date(s['expires_at'])}. "
                "Продли, чтобы не потерять доступ к материалам.",
                reply_markup=S.kb([S.btn("Продлить", "buy:sub")]),
            )
