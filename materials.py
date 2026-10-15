"""Выдача материалов одним rich-сообщением (Bot API 10.1+).

Структура сообщения:
  📘 Предмет              — заголовок
  N файлов · M тем        — подзаголовок
  ▸ Тема 1                — сворачиваемый блок (details), внутри файлы темы
  ▸ Тема 2
  ...
  подвал

Если файлов больше RICH_FILES_PER_MESSAGE, бот режет предмет на части по границам тем.
Если Telegram не принял rich-сообщение, бот шлёт файлы обычными сообщениями,
чтобы человек в любом случае получил материалы.
"""
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo, InputMediaVoiceNote,
    InputRichBlockAudio, InputRichBlockDetails, InputRichBlockDocument, InputRichBlockFooter,
    InputRichBlockParagraph, InputRichBlockPhoto, InputRichBlockSectionHeading, InputRichBlockVideo,
    InputRichBlockVoiceNote, InputRichMessage, RichBlockCaption, RichTextBold,
)

import config
import db

log = logging.getLogger(__name__)
NO_TOPIC = "Без темы"


def plural(n: int, forms: tuple[str, str, str]) -> str:
    if n % 10 == 1 and n % 100 != 11:
        w = forms[0]
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        w = forms[1]
    else:
        w = forms[2]
    return f"{n} {w}"


def media_block(m):
    cap = RichBlockCaption(text=m["file_name"]) if m["file_name"] else None
    fid, t = m["file_id"], m["file_type"]
    if t == "video":
        return InputRichBlockVideo(video=InputMediaVideo(media=fid), caption=cap)
    if t == "audio":
        return InputRichBlockAudio(audio=InputMediaAudio(media=fid), caption=cap)
    if t == "photo":
        return InputRichBlockPhoto(photo=InputMediaPhoto(media=fid), caption=cap)
    if t == "voice":
        return InputRichBlockVoiceNote(voice_note=InputMediaVoiceNote(media=fid), caption=cap)
    return InputRichBlockDocument(document=InputMediaDocument(media=fid), caption=cap)


def group_by_topic(mats) -> list[tuple[str, list]]:
    """Темы идут в порядке появления первого файла."""
    groups: dict[str, list] = {}
    for m in mats:
        groups.setdefault(m["topic"] or NO_TOPIC, []).append(m)
    return list(groups.items())


def split_chunks(groups, limit: int):
    chunks, cur, n = [], [], 0
    for topic, files in groups:
        if cur and n + len(files) > limit:
            chunks.append(cur)
            cur, n = [], 0
        cur.append((topic, files))
        n += len(files)
    if cur:
        chunks.append(cur)
    return chunks


def build_messages(subject_name: str, mats) -> list[tuple[InputRichMessage, list]]:
    groups = group_by_topic(mats)
    chunks = split_chunks(groups, config.RICH_FILES_PER_MESSAGE)
    summary = (f"{plural(len(mats), ('файл', 'файла', 'файлов'))} · "
               f"{plural(len(groups), ('тема', 'темы', 'тем'))}")
    out = []
    for i, chunk in enumerate(chunks, 1):
        part = f" · часть {i} из {len(chunks)}" if len(chunks) > 1 else ""
        blocks = [
            InputRichBlockSectionHeading(text=f"📘 {subject_name}", size=1),
            InputRichBlockParagraph(text=summary + part),
        ]
        for topic, files in chunk:
            blocks.append(InputRichBlockDetails(
                summary=[RichTextBold(text=topic), f"  ·  {len(files)}"],
                blocks=[media_block(f) for f in files],
            ))
        blocks.append(InputRichBlockFooter(text="Нажми на тему, чтобы раскрыть файлы."))
        out.append((InputRichMessage(blocks=blocks), [f for _, fs in chunk for f in fs]))
    return out


async def send_plain(bot: Bot, chat_id: int, subject_name: str, files):
    """Запасной вариант: каждый файл отдельным сообщением с подписью темы."""
    await bot.send_message(chat_id, f"📘 <b>{html.escape(subject_name)}</b>")
    senders = {
        "video": bot.send_video, "audio": bot.send_audio,
        "photo": bot.send_photo, "voice": bot.send_voice,
    }
    for f in files:
        send = senders.get(f["file_type"], bot.send_document)
        await send(chat_id, f["file_id"], caption=f["topic"] or NO_TOPIC,
                   protect_content=config.PROTECT_CONTENT)


async def send_subject(bot: Bot, chat_id: int, subject) -> int:
    """Отправляет предмет. Возвращает количество отправленных файлов."""
    mats = await db.fetch("SELECT * FROM materials WHERE subject_id=? ORDER BY position, id", subject["id"])
    if not mats:
        return 0
    for rich, files in build_messages(subject["name"], mats):
        try:
            await bot.send_rich_message(chat_id, rich_message=rich, protect_content=config.PROTECT_CONTENT)
        except TelegramBadRequest as e:
            log.warning("rich message for subject %s failed: %s — sending plain", subject["id"], e)
            await send_plain(bot, chat_id, subject["name"], files)
    return len(mats)


async def topics_preview(subject_id: int) -> list[tuple[str, int]]:
    mats = await db.fetch("SELECT topic FROM materials WHERE subject_id=? ORDER BY position, id", subject_id)
    return [(t, len(fs)) for t, fs in group_by_topic(mats)]
