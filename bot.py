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


@dp.message(F.photo | F.video | F.document | F.animation)
async def on_file(msg: Message, bot: Bot) -> None:
    if not allowed(msg):
        return
    if msg.photo:
        obj, ext = msg.photo[-1], ".jpg"
    elif msg.video:
        obj, ext = msg.video, ".mp4"
    elif msg.animation:
        obj, ext = msg.animation, ".mp4"
    else:
        obj = msg.document
        mime = obj.mime_type or ""
        if not (mime.startswith("image/") or mime.startswith("video/")):
            await msg.answer("Принимаю только фото и видео.")
            return
        ext = Path(obj.file_name or "").suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm", ".mkv", ".m4v"}:
            ext = ".mp4" if mime.startswith("video/") else ".jpg"

    if obj.file_size and obj.file_size > MAX_INCOMING:
        await msg.answer(f"Файл больше {MAX_INCOMING // 1024 // 1024} МБ — обычный Bot API не даёт "
                         "ботам скачивать такие. Пришли лучше ссылку.")
        return

    status = await msg.answer("⏳ Получаю файл…")
    tmp = Path(tempfile.mkdtemp(prefix="reposter_"))
    try:
        src = tmp / "src" / f"input{ext}"
        src.parent.mkdir(parents=True)
        await bot.download(obj.file_id, destination=src)
        async with work_slots:
            await process_and_send(msg, status, [src], tmp)
    except Exception as e:  # noqa: BLE001
        logging.exception("Ошибка обработки файла")
        await fail(status, f"❌ Ошибка: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
