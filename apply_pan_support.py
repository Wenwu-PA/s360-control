#!/usr/bin/env python3
"""Для сдвига картинки на предпросмотре: в s360_lcd.py build_vf/preview_png получают параметр enc (настройки одним вызовом, без глобального ENC —
безопасно для фонового потока), а режим «Вписать» учитывает focus_x/focus_y (положение картинки в кадре; 0.5 = по центру, как раньше).
Протокол и USB не затрагивает. Запуск из папки проекта (после apply_screen_position.py): python3 apply_pan_support.py
Делает s360_lcd.py.bak2; если проверка не прошла — откатывает."""
import re, shutil, subprocess, sys
from pathlib import Path

P = Path("s360_lcd.py")
s = P.read_text()
if "enc=None" in s and "focus_x" in s and "def build_vf(fps=None, tail=None, viewer=False, enc=None)" in s:
    print("уже применено"); sys.exit(0)
if "viewer=False" not in s:
    sys.exit("сначала примени apply_screen_position.py")
shutil.copy(P, "s360_lcd.py.bak2")

NEW = {}
NEW["_bg_color"] = '''def _bg_color(enc=None):
    c = str((ENC if enc is None else enc).get("background") or "black").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", c):
        return "0x" + c[1:]
    if re.fullmatch(r"[a-zA-Z]+", c):
        return c.lower()
    raise S360Error("фон: имя цвета (black, white...) или #RRGGBB")


'''
NEW["build_vf"] = '''def build_vf(fps=None, tail=None, viewer=False, enc=None):
    """Фильтр ffmpeg: [fps] -> поворот картинки -> прозрачность на фон (в исходном разрешении) -> масштаб под экран ->
    компенсация положения экрана -> формат. enc — словарь настроек поверх ENC (ENC не меняется; безопасно для потоков).
    «Положение экрана» (orient): кадр зрителя при 90/270 горизонтальный (320x240), файл всегда 240x320; viewer=True — кадр глазами зрителя.
    fit: картинка вписывается целиком, её положение в кадре задают focus_x/focus_y (0.5 = по центру);
    fill: заполняет кадр, окно обрезки задают focus_x/focus_y; stretch: растягивается."""
    E = dict(ENC)
    E.update(enc or {})
    mode, bg, rot = E["scale"], _bg_color(E), int(E["rotate"])
    orient = int(E.get("orient") or 0)
    trim = int(E.get("edge_trim") or 0)
    if rot not in ROT:
        raise S360Error("поворот: 0, 90, 180 или 270")
    if orient not in ROT:
        raise S360Error("положение экрана: 0, 90, 180 или 270")
    if not 0 <= trim <= 3:
        raise S360Error("edge-trim: от 0 до 3")
    vw, vh = (H, W) if orient in (90, 270) else (W, H)
    fx = min(1.0, max(0.0, float(E.get("focus_x", 0.5))))
    fy = min(1.0, max(0.0, float(E.get("focus_y", 0.5))))
    parts = []
    if fps or E.get("fps"):
        parts.append(f"fps={fps or E['fps']}")
    if ROT[rot]:
        parts.append(ROT[rot])
    parts.append("format=rgba")
    if trim:
        parts.append("split[ea][eb];[ea]alphaextract" + ",erosion" * trim + "[em];[eb][em]alphamerge")
    parts.append(f"split[fg][t];[t]drawbox=x=0:y=0:w=iw:h=ih:color={bg}@1:t=fill[bgl];[bgl][fg]overlay=format=auto")
    if mode == "fit":
        parts.append(f"scale={vw}:{vh}:force_original_aspect_ratio=decrease,pad={vw}:{vh}:(ow-iw)*{fx}:(oh-ih)*{fy}:{bg}")
    elif mode == "fill":
        parts.append(f"scale={vw}:{vh}:force_original_aspect_ratio=increase,"
                     f"crop={vw}:{vh}:(iw-{vw})*{fx}:(ih-{vh})*{fy}")
    elif mode == "stretch":
        parts.append(f"scale={vw}:{vh}")
    else:
        raise S360Error(f"неизвестный режим масштабирования: {mode}")
    parts.append("setsar=1")
    comp = (360 - orient) % 360
    if not viewer and ROT[comp]:
        parts.append(ROT[comp])
    return ",".join(parts) + "," + (tail or f"format={E['pix_fmt']}")


'''
NEW["preview_png"] = '''def preview_png(src: Path, dst: Path, viewer=False, enc=None):
    """Первый кадр с настройками (без USB). viewer=True — кадр глазами зрителя. enc — настройки поверх ENC (для фоновых потоков)."""
    Path(dst).unlink(missing_ok=True)
    _run_ffmpeg(["ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-i", str(src),
                 "-vf", build_vf(None, tail="format=rgb24", viewer=viewer, enc=enc), "-frames:v", "1", str(dst)], 60)


'''
for name, new in NEW.items():
    m = re.search(rf"^def {name}\(.*?(?=^def |^# -{{10,}})", s, re.S | re.M)
    if not m:
        sys.exit(f"не нашёл функцию {name}: файл изменён сильнее ожидаемого, ничего не меняю")
    s = s[:m.start()] + new + s[m.end():]
P.write_text(s)
try:
    subprocess.run([sys.executable, "-m", "py_compile", "s360_lcd.py"], check=True)
    subprocess.run([sys.executable, "s360_lcd.py", "selftest"], check=True, capture_output=True)
except Exception as e:
    shutil.copy("s360_lcd.py.bak2", P); sys.exit(f"проверка не прошла, откатил: {e}")
print("ok: сдвиг в «Вписать» и параметр enc добавлены (резервная копия s360_lcd.py.bak2)")
