"""Telegram-бот: кидаешь ссылку на пост или сам файл — получаешь обработанные файлы."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, InputMediaDocument, Message
from dotenv import load_dotenv

from downloader import DownloadError, download
from processing import process

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def env(name: str) -> str:
    return os.getenv(name, "").strip()


TOKEN = env("BOT_TOKEN")
if not TOKEN:
    raise SystemExit(f"Не задан BOT_TOKEN. Впиши токен от @BotFather в файл {BASE_DIR / '.env'}")
ALLOWED = {int(x) for x in env("ALLOWED_USERS").replace(" ", "").split(",") if x}

# Необязательно: cookies.txt (например, если YouTube на сервере просит подтвердить, что ты не бот)
_cookies = env("COOKIES_FILE")
COOKIES = str(BASE_DIR / _cookies) if _cookies and (BASE_DIR / _cookies).is_file() else None

LOCAL_API = env("LOCAL_BOT_API_URL") or None  # напр. http://localhost:8081
# Обычный Bot API: отправка до 50 МБ, скачивание у пользователя до 20 МБ.
# Локальный сервер: до 2000 МБ в обе стороны.
MAX_BYTES = (1990 if LOCAL_API else 49) * 1024 * 1024
MAX_INCOMING = (1990 if LOCAL_API else 20) * 1024 * 1024

URL_RE = re.compile(r"https?://\S+")
SUPPORTED = ("tiktok.com", "instagram.com", "threads.net", "threads.com",
             "youtube.com", "youtu.be")

dp = Dispatcher()
work_slots = asyncio.Semaphore(2)  # не больше двух ffmpeg одновременно


def allowed(msg: Message) -> bool:
    return not ALLOWED or bool(msg.from_user and msg.from_user.id in ALLOWED)


async def _safe_edit(status: Message, text: str) -> None:
    try:
        await status.edit_text(text)
    except Exception:  # noqa: BLE001 — «message is not modified» и т.п.
        pass


async def process_and_send(msg: Message, status: Message, files: list[Path], tmp: Path) -> None:
    loop = asyncio.get_running_loop()
    n = len(files)
    results = []
    for i, f in enumerate(files, 1):
        prefix = f"🛠 Обрабатываю {i}/{n}" if n > 1 else "🛠 Обрабатываю"
        await _safe_edit(status, f"{prefix}…")
        last = [0.0]

        def on_progress(frac: float, prefix=prefix, last=last) -> None:
            now = time.monotonic()
            if now - last[0] >= 4:  # не чаще раза в 4 с — лимиты Telegram
                last[0] = now
                asyncio.run_coroutine_threadsafe(
                    _safe_edit(status, f"{prefix}: {int(frac * 100)}%"), loop)

        results.append(await asyncio.to_thread(process, f, tmp / "out", MAX_BYTES, on_progress))

    await status.edit_text("📤 Отправляю…")
    if len(results) == 1:
        await msg.answer_document(FSInputFile(results[0]))
    else:
        for i in range(0, len(results), 10):  # альбом из документов, по 10 штук
            group = [InputMediaDocument(media=FSInputFile(p)) for p in results[i:i + 10]]
            await msg.answer_media_group(group)
    await status.delete()


async def fail(status: Message, text: str) -> None:
    await status.edit_text(text[:4000], parse_mode=None)


@dp.message(CommandStart())
async def start(msg: Message) -> None:
    if not allowed(msg):
        return
    await msg.answer(
        "Кинь ссылку на пост/видео из TikTok, Instagram, Threads или YouTube "
        "или пришли фото/видео файлом — верну с незаметной обработкой.\n"
        f"Твой Telegram ID: <code>{msg.from_user.id}</code>"
    )


async def handle_url(msg: Message, url: str) -> None:
    status = await msg.answer("⏳ Скачиваю…")
    tmp = Path(tempfile.mkdtemp(prefix="reposter_"))
    try:
        async with work_slots:
            files = await asyncio.to_thread(download, url, tmp / "src", COOKIES)
            await process_and_send(msg, status, files, tmp)
    except DownloadError as e:
        await fail(status, f"❌ {e}")
    except Exception as e:  # noqa: BLE001
        logging.exception("Ошибка на %s", url)
        await fail(status, f"❌ Ошибка: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@dp.message(F.text)
async def on_text(msg: Message) -> None:
    if not allowed(msg):
        return
    urls = [u for u in URL_RE.findall(msg.text) if any(s in u.lower() for s in SUPPORTED)]
    if not urls:
        await msg.answer("Не вижу ссылки на TikTok / Instagram / Threads / YouTube. "
                         "Можно прислать фото или видео файлом.")
        return
    for url in urls:
        await handle_url(msg, url)


MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm", ".mkv", ".m4v"}


def file_info(msg: Message):
    """(объект файла, расширение) или None, если это не фото/видео."""
    if msg.photo:
        return msg.photo[-1], ".jpg"
    if msg.video:
        return msg.video, ".mp4"
    if msg.animation:
        return msg.animation, ".mp4"
    if msg.document:
        mime = msg.document.mime_type or ""
        if not (mime.startswith("image/") or mime.startswith("video/")):
            return None
        ext = Path(msg.document.file_name or "").suffix.lower()
        if ext not in MEDIA_EXTS:
            ext = ".mp4" if mime.startswith("video/") else ".jpg"
        return msg.document, ext
    return None


async def handle_files(msgs: list[Message], bot: Bot) -> None:
    """Обрабатывает одно или несколько присланных сообщений с медиа и отвечает
    одним файлом или альбомом в том же порядке."""
    first = msgs[0]
    items, skipped, too_big = [], 0, 0
    for m in msgs:
        info = file_info(m)
        if not info:
            skipped += 1
        elif info[0].file_size and info[0].file_size > MAX_INCOMING:
            too_big += 1
        else:
            items.append(info)

    notes = []
    if skipped:
        notes.append(f"пропущено не-медиа: {skipped}")
    if too_big:
        notes.append(f"больше {MAX_INCOMING // 1024 // 1024} МБ (пришли ссылкой): {too_big}")
    if not items:
        await first.answer("Принимаю только фото и видео" + (f" ({'; '.join(notes)})" if notes else "") + ".")
        return
    if notes:
        await first.answer("⚠️ " + "; ".join(notes))

    status = await first.answer("⏳ Получаю файлы…" if len(items) > 1 else "⏳ Получаю файл…")
    tmp = Path(tempfile.mkdtemp(prefix="reposter_"))
    try:
        (tmp / "src").mkdir(parents=True)
        files = []
        for i, (obj, ext) in enumerate(items, 1):
            src = tmp / "src" / f"{i:03d}{ext}"  # номер в имени сохраняет порядок
            await bot.download(obj.file_id, destination=src)
            files.append(src)
        async with work_slots:
            await process_and_send(first, status, files, tmp)
    except Exception as e:  # noqa: BLE001
        logging.exception("Ошибка обработки файлов")
        await fail(status, f"❌ Ошибка: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Альбом приходит боту отдельными сообщениями с общим media_group_id.
# Копим их и обрабатываем, когда новые части перестали приходить.
ALBUM_WAIT = 2.0
_albums: dict[tuple[int, str], list[Message]] = {}
_album_timers: dict[tuple[int, str], asyncio.Task] = {}


async def _flush_album(key: tuple[int, str], bot: Bot) -> None:
    try:
        await asyncio.sleep(ALBUM_WAIT)
    except asyncio.CancelledError:
        return  # пришла ещё одна часть альбома — таймер перезапущен
    _album_timers.pop(key, None)
    msgs = sorted(_albums.pop(key, []), key=lambda m: m.message_id)
    if msgs:
        await handle_files(msgs, bot)


@dp.message(F.media_group_id, F.photo | F.video | F.document | F.animation)
async def on_album_part(msg: Message, bot: Bot) -> None:
    if not allowed(msg):
        return
    key = (msg.chat.id, msg.media_group_id)
    _albums.setdefault(key, []).append(msg)
    if timer := _album_timers.get(key):
        timer.cancel()
    _album_timers[key] = asyncio.create_task(_flush_album(key, bot))


@dp.message(F.photo | F.video | F.document | F.animation)
async def on_file(msg: Message, bot: Bot) -> None:
    if not allowed(msg):
        return
    await handle_files([msg], bot)


async def main() -> None:
    session = None
    if LOCAL_API:
        session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API, is_local=True))
    bot = Bot(TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    from processing import FFMPEG
    if FFMPEG:
        logging.info("ffmpeg: %s", FFMPEG)
    else:
        logging.warning("ffmpeg не найден — видео обрабатываться не будут! pip install imageio-ffmpeg")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
