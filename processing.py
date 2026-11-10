"""Незаметная для глаза обработка фото и видео.

Каждый файл получает случайный набор параметров, поэтому два прогона одного
и того же видео дают разные файлы:
  * обрезка краёв на 1–2.5% со случайным смещением и масштаб обратно;
  * яркость/контраст/насыщенность ±1–3%, лёгкий сдвиг цветовых каналов;
  * едва заметная виньетка;
  * слабый зерно-шум;
  * видео: ускорение на 0–3% (аудио без изменения тона), громкость ±4%;
  * полная перекодировка и удаление всех метаданных (EXIF, creation_time и т.п.).
"""
from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageOps

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


# ---------------------------------------------------------------- фото ----
def process_image(src: Path, dst_dir: Path) -> Path:
    img = Image.open(src)
    img = ImageOps.exif_transpose(img).convert("RGB")
    w, h = img.size

    # 1. обрезка 1–2.5% с несимметричным смещением + масштаб к исходному размеру
    k = random.uniform(0.010, 0.025)
    cw, ch = int(w * (1 - k)), int(h * (1 - k))
    left = random.randint(0, w - cw)
    top = random.randint(0, h - ch)
    img = img.crop((left, top, left + cw, top + ch)).resize((w, h), Image.LANCZOS)

    a = np.asarray(img).astype(np.float32)

    # 2. яркость и микросдвиг каналов
    a *= random.uniform(0.975, 1.03)
    a *= np.array([random.uniform(0.99, 1.01) for _ in range(3)], dtype=np.float32)

    # 3. виньетка: затемнение углов на 5–9%
    yy, xx = np.ogrid[:h, :w]
    cx, cy = w / 2 + random.uniform(-0.02, 0.02) * w, h / 2 + random.uniform(-0.02, 0.02) * h
    r = np.sqrt(((xx - cx) / (w / 2)) ** 2 + ((yy - cy) / (h / 2)) ** 2) / np.sqrt(2)
    strength = random.uniform(0.05, 0.09)
    a *= (1 - strength * np.clip(r, 0, 1) ** 2)[..., None]

    # 4. лёгкий шум
    a += np.random.normal(0, random.uniform(0.8, 1.8), a.shape).astype(np.float32)

    out = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    dst = dst_dir / f"{src.stem}_r.jpg"
    # сохраняем без EXIF, с чуть разным качеством
    out.save(dst, "JPEG", quality=random.randint(92, 96), optimize=True, subsampling=0)
    return dst


# ---------------------------------------------------------------- видео ---
def _has_audio(src: Path) -> bool:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True, check=False,
    )
    return bool(r.stdout.strip())


def _duration(src: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(src)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _encode(src: Path, dst: Path, p: dict, audio: bool, vkbps: int, max_h: int | None,
            out_dur: float, progress: Callable[[float], None] | None) -> None:
    k = p["crop"]
    scale_back = "scale=trunc(iw/{k}/2)*2:trunc(ih/{k}/2)*2:flags=bicubic".format(k=1 - k)
    vf = [
        f"crop=iw*{1 - k:.4f}:ih*{1 - k:.4f}:(iw-ow)*{p['ox']:.3f}:(ih-oh)*{p['oy']:.3f}",
        scale_back,
    ]
    if max_h:
        vf.append(f"scale=-2:'min({max_h},ih)'")
    vf += [
        "setsar=1",
        "eq=brightness={b:.3f}:contrast={c:.3f}:saturation={s:.3f}:gamma={g:.3f}".format(**p["eq"]),
        f"vignette=angle={p['vig']:.3f}",
    ]
    # Зерно сильно раздувает битрейт — добавляем его, только если есть запас.
    if p["noise"] and vkbps >= 3000:
        vf.append(f"noise=alls={p['noise']}:allf=t")
    vf += [f"setpts=PTS/{p['speed']:.4f}", "format=yuv420p"]

    cmd = [FFMPEG, "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
           "-i", str(src), "-map", "0:v:0", "-vf", ",".join(vf),
           # один быстрый проход: качество по CRF, но не выше бюджета по размеру
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(p["crf"]),
           "-maxrate", f"{vkbps}k", "-bufsize", f"{vkbps * 2}k",
           "-profile:v", "high", "-g", str(p["gop"])]
    if audio:
        af = (f"atempo={p['speed']:.4f},volume={p['vol']:.3f},"
              f"aresample={p['sr']}")
        cmd += ["-map", "0:a:0", "-af", af, "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k"]
    cmd += ["-map_metadata", "-1", "-map_chapters", "-1",
            "-metadata", "encoder=", "-fflags", "+bitexact",
            "-movflags", "+faststart", str(dst)]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="ignore")
    for line in proc.stdout:
        if progress and out_dur > 0 and line.startswith("out_time_us="):
            try:
                progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / out_dur))
            except ValueError:
                pass
    stderr = proc.stderr.read()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg: {stderr.strip()[-800:]}")


AUDIO_KBPS = 128


def process_video(src: Path, dst_dir: Path, max_bytes: int,
                  progress: Callable[[float], None] | None = None) -> Path:
    p = {
        "crop": random.uniform(0.010, 0.025),
        "ox": random.random(),
        "oy": random.random(),
        "eq": {
            "b": random.choice([-1, 1]) * random.uniform(0.008, 0.025),
            "c": random.uniform(1.005, 1.03),
            "s": random.uniform(0.98, 1.05),
            "g": random.uniform(0.98, 1.02),
        },
        # ffmpeg vignette: угол линзы, по умолчанию PI/5≈0.63; меньше = слабее
        "vig": random.uniform(0.28, 0.38),
        "noise": random.randint(1, 3),
        "crf": random.randint(20, 22),
        "speed": random.uniform(1.0, 1.03),
        "vol": random.uniform(0.96, 1.04),
        "sr": random.choice([44100, 48000]),
        "gop": random.choice([48, 50, 60, 72, 90]),
    }
    audio = _has_audio(src)
    dst = dst_dir / f"{src.stem[:60]}_r.mp4"
    out_dur = _duration(src) / p["speed"]

    # Битрейт считаем заранее, чтобы уложиться в лимит Telegram за один проход.
    if out_dur > 0:
        total_kbps = max_bytes * 8 / 1000 * 0.92 / out_dur
        vkbps = int(min(8000, total_kbps - (AUDIO_KBPS if audio else 0)))
    else:
        vkbps = 2500
    if vkbps < 250:
        raise RuntimeError(
            f"Видео слишком длинное ({out_dur / 60:.0f} мин), чтобы уложиться в "
            f"{max_bytes / 1024 / 1024:.0f} МБ. Нужен локальный Bot API сервер (см. README)."
        )
    # при маленьком бюджете уменьшаем разрешение — так и картинка лучше, и быстрее
    max_h = 720 if vkbps < 1500 else (1080 if vkbps < 4000 else None)

    _encode(src, dst, p, audio, vkbps, max_h, out_dur, progress)
    if dst.stat().st_size > max_bytes:  # редкий случай — повторяем с запасом
        vkbps = int(vkbps * max_bytes / dst.stat().st_size * 0.85)
        _encode(src, dst, p, audio, max(vkbps, 150), 720, out_dur, progress)
    if dst.stat().st_size > max_bytes:
        raise RuntimeError("Не удалось ужать видео до лимита Telegram (см. README про локальный Bot API).")
    return dst


def process(src: Path, dst_dir: Path, max_bytes: int,
            progress: Callable[[float], None] | None = None) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
        return process_image(src, dst_dir)
    return process_video(src, dst_dir, max_bytes, progress)
