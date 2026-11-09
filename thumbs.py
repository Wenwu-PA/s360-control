#!/usr/bin/env python3
"""thumbs.py - миниатюры для библиотеки картинок (папка с GIF/MP4). Не трогает глобальные настройки ядра (core.ENC), безопасно для фоновых потоков.
Первый кадр -> PNG 120x160 (вписан на чёрном), кэш по пути, времени изменения и размеру файла."""
import hashlib, os, subprocess
from pathlib import Path

W, H = 120, 160
EXTS = {".gif", ".mp4"}


def list_media(folder, limit=100):
    """GIF/MP4 в папке (без вложенных и скрытых), по имени без учёта регистра; не больше limit."""
    try:
        items = [p for p in Path(folder).iterdir() if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in EXTS]
    except OSError:
        return []
    return sorted(items, key=lambda p: p.name.lower())[:limit]


def thumb_path(src, cache_dir):
    st = Path(src).stat()
    key = hashlib.sha1(f"{Path(src).resolve()}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:20]
    return Path(cache_dir) / f"{key}.png"


def ensure_thumb(src, cache_dir, timeout=20):
    """Путь к миниатюре (создаёт при необходимости) или None, если файл не читается ffmpeg."""
    try:
        out = thumb_path(src, cache_dir)
    except OSError:
        return None
    if out.exists() and out.stat().st_size > 0:
        return out
    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part.png")
        vf = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,format=rgb24"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vf", vf, "-frames:v", "1", str(tmp)],
                       check=True, timeout=timeout, capture_output=True)
        if not tmp.exists() or tmp.stat().st_size == 0:
            return None
        os.replace(tmp, out)
        return out
    except (OSError, subprocess.SubprocessError):
        return None


def media_size(src, timeout=10):
    """(ширина, высота) первого видеопотока через ffprobe или None. Нужна для сдвига картинки на предпросмотре (pan.py)."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(src)],
                             capture_output=True, text=True, timeout=timeout, check=True).stdout.strip().splitlines()[0]
        w, h = (int(x) for x in out.split(",")[:2])
        return (w, h) if w > 0 and h > 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def selftest():
    import subprocess as sp, tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d); lib = d / "lib"; lib.mkdir(); cache = d / "cache"
        sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-t", "1", "-i", "testsrc=s=200x100:r=5", str(lib / "b.gif")], check=True)
        sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-t", "1", "-i", "testsrc=s=64x64:r=5", "-pix_fmt", "yuv420p", str(lib / "A.MP4")], check=True)
        (lib / "x.txt").write_text("не медиа"); (lib / ".hidden.gif").write_bytes(b"x"); (lib / "broken.gif").write_bytes(b"not a gif")
        (lib / "sub").mkdir(); (lib / "sub" / "z.gif").write_bytes(b"x")
        names = [p.name for p in list_media(lib)]
        assert names == ["A.MP4", "b.gif", "broken.gif"], names
        assert list_media(d / "нет") == [] and len(list_media(lib, limit=1)) == 1
        for n in ("b.gif", "A.MP4"):
            t = ensure_thumb(lib / n, cache)
            assert t and t.exists()
            w, h = sp.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(t)], capture_output=True, text=True).stdout.strip().split(",")
            assert (int(w), int(h)) == (W, H), (n, w, h)
        t1 = ensure_thumb(lib / "b.gif", cache); m1 = t1.stat().st_mtime_ns
        assert ensure_thumb(lib / "b.gif", cache) == t1 and t1.stat().st_mtime_ns == m1          # кэш используется
        assert ensure_thumb(lib / "broken.gif", cache) is None and not list(cache.glob("*.part.png"))
        assert ensure_thumb(lib / "missing.gif", cache) is None
        assert media_size(lib / "b.gif") == (200, 100) and media_size(lib / "A.MP4") == (64, 64)
        assert media_size(lib / "broken.gif") is None and media_size(lib / "missing.gif") is None
        os.utime(lib / "b.gif", (1, 1)); assert ensure_thumb(lib / "b.gif", cache) != t1          # файл изменился -> новая миниатюра
    print("thumbs selftest ok: список, миниатюры 120x160, кэш, размеры, битые и отсутствующие файлы")


if __name__ == "__main__":
    selftest()
