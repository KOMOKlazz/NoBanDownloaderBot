"""Скачивание медиа по ссылке без логина: yt-dlp, gallery-dl и og:-теги страницы."""
from __future__ import annotations

import html
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import yt_dlp

VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MEDIA_EXT = VIDEO_EXT | IMAGE_EXT

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

# mp4 до 1080p — чтобы влезало в лимиты Telegram и не было проблем с кодеками
FORMAT = ("bv*[height<=1080][ext=mp4]+ba[ext=m4a]/"
          "b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b[height<=1080]/b")


class DownloadError(Exception):
    pass


def _media_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_EXT]
    return sorted(files, key=lambda p: p.name)


def _fetch(url: str, dst: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r, open(dst, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)


def _best_image(entry: dict) -> str | None:
    thumbs = sorted(entry.get("thumbnails") or [],
                    key=lambda t: (t.get("preference") or 0, t.get("width") or 0))
    return thumbs[-1]["url"] if thumbs else entry.get("thumbnail")


class _SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _via_ytdlp(url: str, out: Path, cookies: str | None) -> None:
    """Видео качает yt-dlp; фото (у которых нет видеоформатов) — по ссылке на картинку.

    Так без логина скачиваются и фото-посты, и карусели Instagram (фото + видео).
    """
    opts = {
        "outtmpl": str(out / "%(playlist_index|0)03d_%(id)s.%(ext)s"),
        "format": FORMAT,
        "merge_output_format": "mp4",
        "ignore_no_formats_error": True,  # фото-элементы не роняют весь пост
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "playlistend": 20,
        "logger": _SilentLogger(),  # ошибки и так попадут в ответ бота
    }
    if cookies:
        opts["cookiefile"] = cookies

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        entries = [e for e in (info.get("entries") or [info]) if e]
        for n, entry in enumerate(entries, 1):
            entry.setdefault("playlist_index", n)
            # у аудио-форматов vcodec == "none"; None — «неизвестно», считаем видео
            has_video = any(f.get("vcodec") != "none" for f in entry.get("formats") or [])
            if has_video:
                ydl.process_ie_result(entry, download=True)
            elif (img := _best_image(entry)) and not entry.get("formats"):
                _fetch(img, out / f"{n:03d}_{entry.get('id', 'img')}.jpg")


def _via_gallerydl(url: str, out: Path, cookies: str | None) -> None:
    """Запасной вариант, например для фото-каруселей TikTok."""
    cmd = [sys.executable, "-m", "gallery_dl", "-D", str(out), "--range", "1-20"]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="ignore", timeout=300, check=False)
    if r.returncode != 0:
        errs = [l for l in r.stderr.splitlines() if "error" in l.lower()]
        raise RuntimeError(errs[-1] if errs else f"код выхода {r.returncode}")


def _via_og_tags(url: str, out: Path) -> None:
    """Последний шанс (полезно для Threads): берём og:video / og:image со страницы."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        page = r.read().decode("utf-8", "ignore")

    def meta(prop: str) -> list[str]:
        pat = rf'<meta[^>]+property="{prop}"[^>]+content="([^"]+)"'
        return [html.unescape(m) for m in re.findall(pat, page)]

    targets = [(u, ".mp4") for u in meta("og:video")] or [(u, ".jpg") for u in meta("og:image")]
    for i, (media_url, ext) in enumerate(targets[:10]):
        _fetch(media_url, out / f"og_{i:03d}{ext}")


def download(url: str, out: Path, cookies: str | None = None) -> list[Path]:
    """Скачивает всё медиа из поста в папку out и возвращает список файлов."""
    out.mkdir(parents=True, exist_ok=True)
    errors = []

    # TikTok стабильнее отдаёт видео через gallery-dl, остальное — через yt-dlp.
    if "tiktok.com" in url.lower():
        order = (_via_gallerydl, _via_ytdlp, _via_og_tags)
    else:
        order = (_via_ytdlp, _via_gallerydl, _via_og_tags)

    for method in order:
        try:
            if method is _via_og_tags:
                method(url, out)
            else:
                method(url, out, cookies)
        except Exception as e:  # noqa: BLE001 — пробуем следующий способ
            errors.append(f"{method.__name__}: {e}")
        files = _media_files(out)
        if files:
            return files

    msg = "Не удалось скачать медиа.\n" + "\n".join(errors)[-1500:]
    msg += "\n\nМожно просто прислать мне фото или видео файлом."
    raise DownloadError(msg)
