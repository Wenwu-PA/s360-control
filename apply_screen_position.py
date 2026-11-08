#!/usr/bin/env python3
"""Добавляет в s360_lcd.py «положение экрана» (0/90/180/270): рамка предпросмотра как у зрителя, картинка в файле
повёрнута так, чтобы на физически повёрнутом экране выглядеть прямо. Протокол и USB не затрагивает.
Запуск из папки проекта: python3 apply_screen_position.py   (делает s360_lcd.py.bak, при ошибке откатывает)."""
import re, shutil, subprocess, sys
from pathlib import Path

P = Path("s360_lcd.py")
s = P.read_text()
if '"screen_pos"' in s and "viewer=False" in s:
    print("уже применено"); sys.exit(0)
shutil.copy(P, "s360_lcd.py.bak")

NEW_VF = '''def build_vf(fps=None, tail=None, viewer=False):
    """Фильтр ffmpeg: [fps] -> поворот картинки -> прозрачность на фон (в исходном разрешении) -> масштаб под экран ->
    компенсация положения экрана -> формат.
    «Поворот картинки» (ENC["rotate"]) крутит само изображение по часовой стрелке.
    «Положение экрана» (ENC["orient"]) — как физически повёрнут экран по часовой: 0/90/180/270. Кадр зрителя при 90/270
    горизонтальный (320x240); в файл (всегда 240x320) картинка кладётся повёрнутой на (360 - положение), чтобы на повёрнутом
    экране выглядеть прямо. viewer=True — кадр так, как его увидит зритель (для предпросмотра).
    Команда поворота экрана (cmd 1a) здесь не отправляется. fit/fill/stretch — как раньше."""
    mode, bg, rot = ENC["scale"], _bg_color(), int(ENC["rotate"])
    orient = int(ENC.get("orient") or 0)
    trim = int(ENC.get("edge_trim") or 0)
    if rot not in ROT:
        raise S360Error("поворот: 0, 90, 180 или 270")
    if orient not in ROT:
        raise S360Error("положение экрана: 0, 90, 180 или 270")
    if not 0 <= trim <= 3:
        raise S360Error("edge-trim: от 0 до 3")
    vw, vh = (H, W) if orient in (90, 270) else (W, H)
    parts = []
    if fps or ENC["fps"]:
        parts.append(f"fps={fps or ENC['fps']}")
    if ROT[rot]:
        parts.append(ROT[rot])
    parts.append("format=rgba")
    if trim:
        parts.append("split[ea][eb];[ea]alphaextract" + ",erosion" * trim + "[em];[eb][em]alphamerge")
    parts.append(f"split[fg][t];[t]drawbox=x=0:y=0:w=iw:h=ih:color={bg}@1:t=fill[bgl];[bgl][fg]overlay=format=auto")
    if mode == "fit":
        parts.append(f"scale={vw}:{vh}:force_original_aspect_ratio=decrease,pad={vw}:{vh}:(ow-iw)/2:(oh-ih)/2:{bg}")
    elif mode == "fill":
        fx, fy = float(ENC["focus_x"]), float(ENC["focus_y"])
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
    return ",".join(parts) + "," + (tail or f"format={ENC['pix_fmt']}")


'''
NEW_PREVIEW = '''def preview_png(src: Path, dst: Path, viewer=False):
    """Первый кадр с текущими настройками (без USB). viewer=True — кадр глазами зрителя (при положении 90/270 горизонтальный)."""
    Path(dst).unlink(missing_ok=True)
    _run_ffmpeg(["ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-i", str(src),
                 "-vf", build_vf(None, tail="format=rgb24", viewer=viewer), "-frames:v", "1", str(dst)], 60)


'''
def swap(name, new):
    global s
    m = re.search(rf"^def {name}\(.*?(?=^def |^# -{{10,}})", s, re.S | re.M)
    if not m:
        raise SystemExit(f"не нашёл функцию {name}: файл изменён сильнее ожидаемого, ничего не меняю")
    s = s[:m.start()] + new + s[m.end():]
swap("build_vf", NEW_VF)
swap("preview_png", NEW_PREVIEW)
m = re.search(r"DEFAULT_SETTINGS\s*=\s*\{", s)
if not m:
    raise SystemExit("не нашёл DEFAULT_SETTINGS")
s = s[:m.end()] + '"screen_pos": 0, ' + s[m.end():]
m = re.search(r'ENC\s*=\s*\{', s)
if m and '"orient": 0' not in s:
    s = s[:m.end()] + '"orient": 0, ' + s[m.end():]
P.write_text(s)
try:
    subprocess.run([sys.executable, "-m", "py_compile", "s360_lcd.py"], check=True)
    subprocess.run([sys.executable, "s360_lcd.py", "selftest"], check=True, capture_output=True)
except Exception as e:
    shutil.copy("s360_lcd.py.bak", P); raise SystemExit(f"проверка не прошла, откатил: {e}")
print("ok: положение экрана добавлено (резервная копия s360_lcd.py.bak)")
