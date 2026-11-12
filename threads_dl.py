"""Скачивание из Threads без логина.

Ни yt-dlp, ни gallery-dl Threads не поддерживают, а данные поста (фото, видео,
карусели) есть только на странице, которую открыл браузер. Поэтому открываем
пост в headless-Chromium (Playwright) и читаем JSON, встроенный в страницу.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def shortcode(url: str) -> str | None:
    m = re.search(r"/post/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def _page_json(url: str, code: str) -> list[str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        # встроенный Chromium (playwright install chromium), иначе установленный Edge/Chrome
        browser, errors = None, []
        for channel in (None, "msedge", "chrome"):
            try:
                browser = p.chromium.launch(headless=True, channel=channel)
                break
            except Exception as e:  # noqa: BLE001
                errors.append(str(e).splitlines()[0])
        if browser is None:
            raise RuntimeError("Нет браузера для Threads. Выполни: python -m playwright install chromium")
        try:
            page = browser.new_page(locale="en-US")
            # нам нужен только JSON со страницы — картинки, видео, шрифты и стили не грузим
            page.route("**/*", lambda route: route.abort()
                       if route.request.resource_type in ("image", "media", "font", "stylesheet")
                       else route.continue_())
            page.goto(url, wait_until="commit", timeout=90_000)
            # Ждём, пока в странице появятся данные именно этого поста. Опрашиваем из
            # Python: wait_for_function использует eval, а CSP Threads его запрещает.
            selector = 'script[type="application/json"]'
            deadline = time.monotonic() + 90
            while True:
                texts = page.eval_on_selector_all(selector, "els => els.map(e => e.textContent)")
                found = [t for t in texts if code in t and "image_versions2" in t]
                if found or time.monotonic() > deadline:
                    return found
                page.wait_for_timeout(1000)
        finally:
            browser.close()


def _find_post(obj, code: str):
    """Ищет в JSON словарь поста с нужным code и медиа-полями."""
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            if o.get("code") == code and ("image_versions2" in o or "carousel_media" in o):
                return o
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return None


def _item_media(item: dict) -> tuple[str, str] | None:
    """(url, ext) лучшего варианта: видео, если есть, иначе самая большая картинка."""
    videos = item.get("video_versions") or []
    if videos and videos[0].get("url"):
        return videos[0]["url"], ".mp4"
    cands = (item.get("image_versions2") or {}).get("candidates") or []
    if cands:
        best = max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
        return best["url"], ".jpg"
    return None


def download(url: str, out: Path) -> None:
    code = shortcode(url)
    if not code:
        raise RuntimeError("не похоже на ссылку на пост Threads (нужно .../post/КОД)")
    url = url.split("?", 1)[0]  # параметры отслеживания (?xmt=...) не нужны

    post = None
    for text in _page_json(url, code):
        try:
            post = _find_post(json.loads(text), code)
        except json.JSONDecodeError:
            continue
        if post:
            break
    if not post:
        raise RuntimeError("Threads не отдал данные поста (пост удалён или закрыт?)")

    items = post.get("carousel_media") or [post]
    out.mkdir(parents=True, exist_ok=True)
    for i, item in enumerate(items, 1):
        media = _item_media(item)
        if not media:
            continue
        media_url, ext = media
        req = urllib.request.Request(media_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=300) as r, open(out / f"{i:03d}_{code}{ext}", "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
