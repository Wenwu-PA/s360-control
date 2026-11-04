#!/usr/bin/env python3
"""s360_lcd.py - ядро и CLI для MSI MEG CoreLiquid S360 под Linux.

Что делает:
  LCD  (0db0:9ba6): загрузка GIF/MP4 (проверено на железе); яркость, вкл/выкл, поворот экрана, режим «Часы» -
                    по захватам MSI Center с ЭТОГО устройства (docs/PROTOCOL_VERIFIED.md), на железе из Linux ещё не пробовано.
  Кулер(0db0:6a05): режимы Silent/Balance/Game/Customize и кастомные кривые - формат отчётов d0 40/41 сверен с
                    захватами MSI Center этого устройства; на железе из Linux ещё не пробовано. Режим держится
                    вместе с передачей температуры CPU (d0 85).
НЕ реализовано: режимы экрана Hardware Monitor/Weather/Banner, чтение RPM.

Правила безопасности: по умолчанию ничего не пишется; без авто-повторов; без reset; при ошибке записи LCD
состояние = UNCERTAIN.
"""
import argparse, hashlib, json, math, os, re, signal, subprocess, sys, tempfile, time
from fractions import Fraction
from pathlib import Path

VERSION = "0.7"
VID, PID = 0x0DB0, 0x9BA6
IFACE = 0
OUT_EP = 0x02
SLOT_NAME = b"M1-S7-V-R0.mp4"           # слот из захвата (перезапишет содержимое S7/R0!)
REFERENCE_SHA256 = "4b2ae0d578d988031ce8f3f2528d5f4ae6d1e8100531d6e13127cfead3ac8a34"
REFERENCE_SIZE = 231012
MAX_BYTES = 32 * 1024 * 1024            # программный предел, не лимит прошивки
CALL_TIMEOUT_MS = 2000
TOTAL_TIMEOUT_S = 60
_XDG_STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
_XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
STATE = _XDG_STATE / "s360-lcd" / "last_operation.json"
COOLING_STATE = _XDG_STATE / "s360-lcd" / "cooling.json"
CONFIG_DIR = _XDG_CONFIG / "s360-control"
SETTINGS = CONFIG_DIR / "settings.json"
COOLING_ENV = CONFIG_DIR / "cooling.env"       # читается systemd-юнитом s360-feed.service

COOLER_VID, COOLER_PID = 0x0DB0, 0x6A05
COOLING_MODES = {"silent": 0, "balance": 1, "game": 2}          # то, что показывает GUI
COOLING_MODE_IDS = {"silent": 0, "balance": 1, "game": 2, "custom": 3}   # подтверждено захватами 3-6
CHANNELS = ("radiator1", "radiator2", "radiator3", "waterblock", "pump")  # блоки d0 40/41 по смещениям 2/10/18/26/34
PUMP_MIN_DUTY = 60               # консервативный запас: так в стандартной кривой; это НЕ предел MSI Center
CURVES_FILE = CONFIG_DIR / "curves.json"
INI_NAME = b"HMI_FW_Setting.ini"
# Стандартные кривые, как их шлёт MSI Center этого устройства (захват 2): 4 точки на вентилятор, duty % и температура °C.
# Блоки 1-2 сопоставлены радиатору по виду (одинаковы, MSI показывает вкладки 1/2/3); 0, 3, 4 сверены со скриншотами.
DEFAULT_CURVES = {
    "radiator1": {"duty": [21, 28, 42, 100], "temp": [48, 63, 79, 81]},
    "radiator2": {"duty": [28, 40, 70, 100], "temp": [35, 40, 70, 81]},
    "radiator3": {"duty": [28, 40, 70, 100], "temp": [35, 40, 70, 81]},
    "waterblock": {"duty": [13, 23, 35, 53], "temp": [51, 64, 75, 81]},
    "pump": {"duty": [60, 60, 89, 100], "temp": [59, 68, 75, 81]},
}
COOLING_LABELS = {"silent": "Silent", "balance": "Balance", "game": "Game Mode"}
FEED_FREQ_DUMMY_MHZ = 3000              # как в стороннем драйвере: прошивка частоту CPU, по их словам, не использует
FEED_INTERVAL_S = 2.0

DEFAULT_SETTINGS = {"fps": "15", "max_seconds": None, "scale": "fill", "focus_x": 0.5, "focus_y": 0.5,
                    "with_select": True, "cooling_mode": "balance", "background": "black", "rotate": 0,
                    "device_direction": 0, "edge_trim": 0, "brightness": 100}


# Проверено НА ЭТОМ экране (отправка и показ без зависаний): 20/3, 10 и 15 fps; анимация до ~2,5 с (37 кадров, 146 КБ).
TESTED_RATES = (Fraction(20, 3), Fraction(10), Fraction(15))
TESTED_MAX_SECONDS = 2.5


class S360Error(Exception):
    def __init__(self, msg, uncertain=False):
        super().__init__(msg)
        self.uncertain = uncertain


# ----------------------------------------------------------------------------- протокол LCD
def cmd_report(cmd: int, length: int, params: bytes = b"") -> bytes:
    """ID1, 64 байта: 01 6b 5a CMD 00 LEN_LE32 PARAMS(zero pad)."""
    body = bytes([0x01, 0x6B, 0x5A, cmd, 0x00]) + length.to_bytes(4, "little") + params
    if len(body) > 64:
        raise ValueError("команда длиннее 64 байт")
    return body.ljust(64, b"\x00")


def data_reports(data: bytes):
    """ID2, 1024 байта: 0x02 + 1023 байта файла, последний добит нулями."""
    for i in range(0, len(data), 1023):
        yield (b"\x02" + data[i:i + 1023]).ljust(1024, b"\x00")


DIRECTIONS = (0, 90, 180, 270)


def slot_for_direction(deg: int) -> bytes:
    """По статическому анализу MSI: файлы для 0/180 называются ...-R0, для 90/270 - ...-R1. НЕ проверено на железе."""
    if deg not in DIRECTIONS:
        raise S360Error("поворот экрана: 0, 90, 180 или 270")
    return SLOT_NAME if deg in (0, 180) else SLOT_NAME.replace(b"-R0", b"-R1")


def build_save_plan():
    """Как MSI Center после смены яркости/поворота: файл HMI_FW_Setting.ini из 3 байт "MSI" (cmd 20/22, ID2, cmd 26).
    Подтверждено захватами 9-12 (байты совпадают)."""
    return build_upload_plan(b"MSI", INI_NAME)


def build_brightness_plan(value: int):
    """cmd 18 (uint32LE 0..100) + сохранение. Подтверждено: 0 (экран выкл), 50 и 100 (захваты 9, 10, 12)."""
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
        raise S360Error("яркость: целое число 0..100")
    return [("control", cmd_report(0x18, 4, value.to_bytes(4, "little")))] + build_save_plan()


def build_direction_plan(deg: int):
    """cmd 1a (uint32LE градусы) + сохранение + повторный выбор слота S7, как MSI Center (захват 11).
    Подтверждено: 180 и 0. 90/270 - по анализу DLL, захвата нет."""
    if deg not in DIRECTIONS:
        raise S360Error("поворот экрана: 0, 90, 180 или 270")
    return ([("control", cmd_report(0x1A, 4, deg.to_bytes(4, "little")))] + build_save_plan() + build_select_plan())


def build_clock_plan(style: int = 1, now=None, tz_offset=None):
    """Режим «System Clock»: cmd 36 (флаг 1, 0, МЕСТНОЕ время как unix-секунды) + cmd 50 (режим 5, стилей 5,
    выбранный стиль). Подтверждено захватом 1 только для стиля 1 (остальные - по сдвигу флага)."""
    if style not in (1, 2, 3, 4, 5):
        raise S360Error("стиль часов: 1..5")
    base = int(time.time() if now is None else now)
    local = base + (time.localtime().tm_gmtoff if tz_offset is None else tz_offset)
    flags = bytearray(16)
    flags[style - 1] = 1
    return [("control", cmd_report(0x36, 12, (1).to_bytes(4, "little") + bytes(4) + (local & 0xFFFFFFFF).to_bytes(4, "little"))),
            ("control", cmd_report(0x50, 19, bytes([5, 0, 5]) + bytes(flags)))]


def build_upload_plan(data: bytes, slot: bytes = SLOT_NAME):
    """Список (маршрут, байты). control = SET_REPORT на EP0, interrupt = OUT 0x02."""
    n = len(data)
    plan = [
        ("control", cmd_report(0x20, 4 + len(slot), n.to_bytes(4, "little") + slot)),
        ("control", cmd_report(0x22, n)),
    ]
    plan += [("interrupt", r) for r in data_reports(data)]
    plan.append(("control", cmd_report(0x26, 0)))
    return plan


def build_select_plan():
    """cmd50 + 8 x cmd40 ПЕРЕД загрузкой, как в захвате MSI Center. На практике без них экран не менялся."""
    flags = bytes([0, 0, 0, 0, 0, 0, 0, 1] + [0] * 8)           # слот 7
    plan = [("control", cmd_report(0x50, 19, bytes([1, 0, 8]) + flags))]
    names = [f"M1-S7-O-R{r}.{e}" for r in (0, 1) for e in ("jpg", "bmp", "gif", "png")]
    for nm in names:
        p = nm.encode().ljust(32, b"\x00") + (350).to_bytes(4, "little") * 2 + bytes([0, 0])
        plan.append(("control", cmd_report(0x40, 42, p)))
    return plan


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def mp4_top_boxes(b: bytes):
    pos, out = 0, []
    while pos + 8 <= len(b):
        size = int.from_bytes(b[pos:pos + 4], "big")
        typ = b[pos + 4:pos + 8].decode("latin1")
        if size < 8 or pos + size > len(b):
            raise ValueError(f"битый MP4: box {typ} @ {pos}")
        out.append((typ, pos, size)); pos += size
    if pos != len(b):
        raise ValueError("MP4: хвост после последнего box")
    return out


def validate_mp4(b: bytes):
    if not b or len(b) > MAX_BYTES:
        raise ValueError("файл пуст или больше 32 MiB (программный предел)")
    types = [t for t, _, _ in mp4_top_boxes(b)]
    if "ftyp" not in types or "mdat" not in types or "moov" not in types:
        raise ValueError(f"нет ftyp/mdat/moov: {types}")


# ----------------------------------------------------------------------------- конвертация
ENC = {"fps": None, "max_seconds": None, "profile": "baseline", "level": "3.0", "pix_fmt": "yuvj420p",
       "scale": "fill", "focus_x": 0.5, "focus_y": 0.5, "background": "black", "rotate": 0, "edge_trim": 0}
W, H = 240, 320    # размер кадра эталонного MP4 (портрет), как в захвате


ROT = {0: None, 90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}   # поворот ИЗОБРАЖЕНИЯ по часовой


def _bg_color():
    c = str(ENC["background"] or "black").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", c):
        return "0x" + c[1:]
    if re.fullmatch(r"[a-zA-Z]+", c):
        return c.lower()
    raise S360Error("фон: имя цвета (black, white...) или #RRGGBB")


def build_vf(fps=None, tail=None):
    """Фильтр ffmpeg: [fps] -> поворот -> прозрачность на фон (В ИСХОДНОМ разрешении) -> масштаб под экран -> формат.
    Подложка ДО масштабирования важна: прозрачные пиксели GIF несут белый цвет, и при масштабировании он
    подмешивался в край фигуры (белая обводка).  --edge-trim N стачивает прозрачный край на N пикселей (0..3).
    Поворот программный (меняет картинку), команда поворота экрана здесь не отправляется.
    fit - вписать (полосы цвета фона); fill - заполнить с обрезкой (--focus-x/y); stretch - растянуть."""
    mode, bg, rot = ENC["scale"], _bg_color(), int(ENC["rotate"])
    trim = int(ENC.get("edge_trim") or 0)
    if rot not in ROT:
        raise S360Error("поворот: 0, 90, 180 или 270")
    if not 0 <= trim <= 3:
        raise S360Error("edge-trim: от 0 до 3")
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
        parts.append(f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:{bg}")
    elif mode == "fill":
        fx, fy = float(ENC["focus_x"]), float(ENC["focus_y"])
        parts.append(f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H}:(iw-{W})*{fx}:(ih-{H})*{fy}")
    elif mode == "stretch":
        parts.append(f"scale={W}:{H}")
    else:
        raise S360Error(f"неизвестный режим масштабирования: {mode}")
    parts.append("setsar=1")
    return ",".join(parts) + "," + (tail or f"format={ENC['pix_fmt']}")


def _run_ffmpeg(cmd, timeout):
    try:
        subprocess.run(cmd, check=True, timeout=timeout, capture_output=True, text=True)
    except FileNotFoundError:
        raise S360Error("ffmpeg не найден (pacman -S ffmpeg)")
    except subprocess.CalledProcessError as e:
        raise S360Error(f"ffmpeg завершился с ошибкой: {(e.stderr or '').strip()[-300:]}")
    except subprocess.TimeoutExpired:
        raise S360Error("ffmpeg: превышено время ожидания")


def convert_gif(src: Path, dst: Path, fps=None):
    """GIF -> H.264 240x320 (параметры как у эталона: baseline, level 3.0, yuvj420p), без faststart."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-i", str(src), "-an",
           "-vf", build_vf(fps), "-c:v", "libx264", "-pix_fmt", ENC["pix_fmt"]]
    if ENC["profile"]:
        cmd += ["-profile:v", ENC["profile"]]
    if ENC["level"]:
        cmd += ["-level", ENC["level"]]
    if ENC["max_seconds"]:
        cmd += ["-t", str(ENC["max_seconds"])]
    _run_ffmpeg(cmd + [str(dst)], 120)


def preview_png(src: Path, dst: Path):
    """Первый кадр с текущими настройками масштабирования (без USB)."""
    Path(dst).unlink(missing_ok=True)
    _run_ffmpeg(["ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-i", str(src),
                 "-vf", build_vf(None, tail="format=rgb24"), "-frames:v", "1", str(dst)], 60)


def probe_mp4(data: bytes):
    """(длительность_с, кадров, r_frame_rate-строка) через ffprobe; None, если не удалось."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "x.mp4"
        f.write_bytes(data)
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "stream=nb_frames,r_frame_rate,duration", "-of", "json", str(f)],
                                 capture_output=True, text=True, timeout=30, check=True).stdout
            st = json.loads(out)["streams"][0]
            dur = float(st["duration"]) if st.get("duration") not in (None, "N/A") else None
            frames = int(st["nb_frames"]) if str(st.get("nb_frames", "")).isdigit() else None
            return dur, frames, st.get("r_frame_rate")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
            return None, None, None


def beyond_tested(duration, rate) -> bool:
    """True, если длительность или fps выходят за то, что проверено на экране."""
    try:
        if rate and Fraction(rate) not in TESTED_RATES:
            return True
    except (ValueError, ZeroDivisionError):
        return True
    return duration is not None and duration > TESTED_MAX_SECONDS


def load_media(path: Path, fps=None) -> bytes:
    if path.suffix.lower() == ".gif":
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out.mp4"
            convert_gif(path, out, fps)
            data = out.read_bytes()
    elif path.suffix.lower() == ".mp4":
        data = path.read_bytes()
    else:
        raise S360Error("Поддерживаются только .gif и .mp4 (статичные PNG/JPG не подтверждены протоколом)")
    try:
        validate_mp4(data)
    except ValueError as e:
        raise S360Error(str(e))
    return data


# ----------------------------------------------------------------------------- состояние и настройки
def read_state():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _atomic_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def write_state(status, **extra):
    _atomic_json(STATE, {"status": status, "time": time.time(), **extra})


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update({k: v for k, v in json.loads(SETTINGS.read_text()).items() if k in DEFAULT_SETTINGS})
    except (FileNotFoundError, ValueError):
        pass
    return s


def save_settings(s):
    _atomic_json(SETTINGS, {k: s[k] for k in DEFAULT_SETTINGS if k in s})


def usb_present(vid, pid, root=Path("/sys/bus/usb/devices")):
    """Есть ли устройство в sysfs (без открытия, без прав)."""
    try:
        for d in root.iterdir():
            try:
                if (int((d / "idVendor").read_text(), 16) == vid and int((d / "idProduct").read_text(), 16) == pid):
                    return True
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return False


# ----------------------------------------------------------------------------- отправка на LCD
def do_send(plan, detach_ok: bool, media_sha: str, progress=None, cancel=None, kind="upload"):
    """Реальная запись. Без повторов. Ошибки -> S360Error (uncertain=True, если запись уже началась)."""
    progress = progress or (lambda i, n: print(f"  {i}/{n}", flush=True) if (i % 25 == 1 or i == n) else None)
    try:
        import usb.core, usb.util                  # pyusb, только здесь
    except ImportError:
        raise S360Error("нет pyusb (pacman -S python-pyusb)")
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        raise S360Error("LCD 0db0:9ba6 не найден")
    try:
        active = dev.is_kernel_driver_active(IFACE)
    except usb.core.USBError as e:
        raise S360Error(f"нет доступа к USB-устройству ({e}). Нужно udev-правило (install.sh) или sudo.")
    if active and not detach_ok:
        raise S360Error("Интерфейс 0 занят ядром. Нужно разрешить временное отсоединение HID-драйвера.")
    write_state("IN_PROGRESS", sha256=media_sha, kind=kind)
    t0 = time.monotonic()
    detached = claimed = False
    phase = "setup"
    try:
        if active:
            dev.detach_kernel_driver(IFACE); detached = True
        usb.util.claim_interface(dev, IFACE); claimed = True
        phase = "writing"
        for i, (route, payload) in enumerate(plan):
            if cancel and cancel():
                raise InterruptedError("отменено пользователем")
            if time.monotonic() - t0 > TOTAL_TIMEOUT_S:
                raise TimeoutError("общий таймаут 60 с")
            if route == "control":
                n = dev.ctrl_transfer(0x21, 0x09, 0x0201, IFACE, payload, CALL_TIMEOUT_MS)
            else:
                n = dev.write(OUT_EP, payload, CALL_TIMEOUT_MS)
            if n != len(payload):
                raise IOError(f"короткая запись: пакет {i}, {n}/{len(payload)}")
            progress(i + 1, len(plan))
            if route == "control" and payload[3] == 0x20:
                time.sleep(0.001)                   # вендор ждёт 1 мс между 20 и 22
    except BaseException as e:                       # включая Ctrl+C: НИЧЕГО не досылаем
        if phase == "setup":
            write_state("NOT_STARTED", sha256=media_sha, kind=kind, error=repr(e))
            raise S360Error(f"не удалось подготовить USB (запись не начиналась): {e}")
        write_state("UNCERTAIN", sha256=media_sha, kind=kind, error=repr(e))
        raise S360Error(f"ошибка во время записи: {e}. Состояние LCD НЕИЗВЕСТНО, автоповторов нет.", uncertain=True)
    finally:
        if claimed:
            try: usb.util.release_interface(dev, IFACE)
            except Exception: pass
        if detached:
            try: dev.attach_kernel_driver(IFACE)
            except Exception as e: print(f"Не удалось вернуть HID-драйвер: {e!r}", file=sys.stderr)
    write_state("HOST_COMPLETE_UNCONFIRMED", sha256=media_sha, kind=kind)


# ----------------------------------------------------------------------------- кулер (ЭКСПЕРИМЕНТАЛЬНО)
def validate_curves(curves):
    """Проверка кривых: по 4 точки на канал, температуры растут, обороты не падают, помпа не ниже PUMP_MIN_DUTY."""
    if not isinstance(curves, dict) or set(curves) != set(CHANNELS):
        raise S360Error("кривые: нужны каналы " + ", ".join(CHANNELS))
    for ch in CHANNELS:
        d, t = curves[ch].get("duty"), curves[ch].get("temp")
        if not (isinstance(d, list) and isinstance(t, list) and len(d) == len(t) == 4
                and all(isinstance(x, int) and not isinstance(x, bool) for x in d + t)):
            raise S360Error(f"{ch}: нужны списки duty и temp по 4 целых числа")
        if any(not 0 <= x <= 100 for x in d + t):
            raise S360Error(f"{ch}: значения от 0 до 100")
        if any(t[k] >= t[k + 1] for k in range(3)):
            raise S360Error(f"{ch}: температуры должны строго расти")
        if any(d[k] > d[k + 1] for k in range(3)):
            raise S360Error(f"{ch}: обороты не должны падать с ростом температуры")
    if min(curves["pump"]["duty"]) < PUMP_MIN_DUTY:
        raise S360Error(f"pump: не ниже {PUMP_MIN_DUTY}% (остановка помпы опасна для CPU)")
    return curves


def load_curves(path=None):
    path = Path(path or CURVES_FILE)
    try:
        return validate_curves(json.loads(path.read_text()))
    except FileNotFoundError:
        raise S360Error(f"нет файла кривых {path} (шаблон: s360-lcd cooling curves > {path})")
    except ValueError as e:
        raise S360Error(f"{path}: не JSON ({e})")


def build_cooler_reports(mode: int, curves=None):
    """d0 40 (duty %) и d0 41 (температура °C): 5 блоков по 8 байт [режим, 4 значения, 0, 0, 0] со смещения 2.
    Режимы 0/1/2/3 = Silent/Balance/Game/Customize. Байт-в-байт совпадает с отчётами MSI Center (захваты 2-6)."""
    curves = validate_curves(curves if curves is not None else DEFAULT_CURVES)
    if mode not in COOLING_MODE_IDS.values():
        raise S360Error("режим кулера: 0..3")
    out = []
    for cmd, key in ((0x40, "duty"), (0x41, "temp")):
        b = bytearray(64)
        b[0], b[1] = 0xD0, cmd
        for n, ch in enumerate(CHANNELS):
            o = 2 + 8 * n
            b[o] = mode
            b[o + 1:o + 5] = bytes(curves[ch][key])
        out.append(bytes(b))
    return out


def cooler_mode_reports(mode: int):
    return build_cooler_reports(mode)


def feed_report(temp_c: int, freq_mhz: int = FEED_FREQ_DUMMY_MHZ) -> bytes:
    """d0 85: uint16LE на смещениях 2 (частота) и 4 (температура CPU, °C)."""
    b = bytearray(64)
    b[0], b[1] = 0xD0, 0x85
    b[2:4] = int(freq_mhz).to_bytes(2, "little")
    b[4:6] = int(temp_c).to_bytes(2, "little")
    return bytes(b)


def find_hidraw(vid=COOLER_VID, pid=COOLER_PID, iface=1, sys_root=Path("/sys"), dev_root=Path("/dev")):
    """Ищет hidraw-узел нужного интерфейса по sysfs (без номеров шин/адресов)."""
    base = sys_root / "class/hidraw"
    try:
        nodes = sorted(base.glob("hidraw*"))
    except OSError:
        nodes = []
    for node in nodes:
        try:
            hid_dev = (node / "device").resolve()
            ids = [l.split("=", 1)[1] for l in (hid_dev / "uevent").read_text().splitlines() if l.startswith("HID_ID=")]
            _, v, p = ids[0].split(":")
            if int(v, 16) != vid or int(p, 16) != pid:
                continue
            if int((hid_dev.parent / "bInterfaceNumber").read_text().strip(), 16) != iface:
                continue
            return dev_root / node.name
        except (OSError, ValueError, IndexError):
            continue
    return None


def read_cpu_temp(root=Path("/sys/class/hwmon")):
    """Температура CPU из hwmon: coretemp (Intel) temp1_input, либо k10temp (AMD)."""
    for want in ("coretemp", "k10temp"):
        for h in sorted(root.glob("hwmon*")):
            try:
                if (h / "name").read_text().strip() == want:
                    t = int((h / "temp1_input").read_text()) / 1000.0
                    if -20 <= t <= 130:
                        return int(round(t))
            except (OSError, ValueError):
                continue
    raise S360Error("температура CPU не прочитана (нет coretemp/k10temp в hwmon)")


def _write_reports(path, reports):
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError as e:
        raise S360Error(f"не открыть {path}: {e}. Нужно udev-правило (install.sh).")
    try:
        for r in reports:
            n = os.write(fd, r)
            if n != len(r):
                raise S360Error(f"короткая запись в {path}: {n}/{len(r)}")
    except OSError as e:
        raise S360Error(f"ошибка записи в {path}: {e}")
    finally:
        os.close(fd)


def cooling_set(mode_name: str, apply=False, hid_path=None, curves=None, sleep=time.sleep):
    """Отправка режима. Как MSI Center, пара d0 40/41 шлётся дважды с паузой ~0.4 с."""
    mode = COOLING_MODE_IDS[mode_name]
    if mode == 3 and curves is None:
        curves = load_curves()
    reports = build_cooler_reports(mode, curves)
    if not apply:
        return reports
    path = hid_path or find_hidraw()
    if path is None:
        raise S360Error("hidraw-узел кулера 0db0:6a05 (интерфейс 1) не найден")
    _write_reports(path, reports)
    sleep(0.4)
    _write_reports(path, reports)
    _atomic_json(COOLING_STATE, {"mode": mode_name, "time": time.time(),
                                 "note": "последний ОТПРАВЛЕННЫЙ режим; обратного чтения нет"})
    return reports


def cooling_feed(mode_name=None, interval=FEED_INTERVAL_S, stop=lambda: False, log=print, sleep=time.sleep,
                 hid_path=None, temp_reader=read_cpu_temp):
    """Цикл передачи температуры CPU. Не шлёт ничего при неизвестной температуре. SIGTERM -> выход."""
    path = hid_path or find_hidraw()
    if path is None:
        raise S360Error("hidraw-узел кулера 0db0:6a05 (интерфейс 1) не найден")
    if mode_name:
        cooling_set(mode_name, apply=True, hid_path=path, sleep=sleep)
        log(f"режим {mode_name} отправлен")
    log(f"передача температуры CPU каждые {interval:g} с -> {path}")
    while not stop():
        try:
            t = temp_reader()
            _write_reports(path, [feed_report(t)])
        except S360Error as e:
            log(f"пропуск: {e}")
        sleep(interval)
    log("остановлено (передача температуры прекращена; поведение прошивки без неё не проверено)")


# ----------------------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    enc = argparse.ArgumentParser(add_help=False)
    enc.add_argument("--fps", help="фиксированный fps для GIF (проверено на экране: 20/3, 10, 15)")
    enc.add_argument("--max-seconds", type=float, help="обрезать до N секунд (эталон: 1.95)")
    enc.add_argument("--profile", choices=["baseline", "main", "high"], default="baseline",
                     help="профиль H.264 (по умолчанию baseline, как у эталона)")
    enc.add_argument("--scale", choices=["fit", "fill", "stretch"], default="fill",
                     help="fill (по умолчанию, обрезка), fit (чёрные полосы), stretch (растянуть)")
    enc.add_argument("--focus-x", type=float, default=0.5, help="для fill: 0=левый край, 0.5=центр, 1=правый")
    enc.add_argument("--focus-y", type=float, default=0.5, help="для fill: 0=верх, 0.5=центр, 1=низ")
    enc.add_argument("--rotate", type=int, choices=[0, 90, 180, 270], default=0,
                     help="повернуть ИЗОБРАЖЕНИЕ по часовой (программно; на экран команда поворота не шлётся)")
    enc.add_argument("--edge-trim", type=int, choices=[0, 1, 2, 3], default=0,
                     help="сточить край прозрачного объекта на N px (против светлой каймы, запечённой в GIF)")
    enc.add_argument("--background", default="black", help="фон для прозрачных GIF и полос fit: black, white, #RRGGBB")
    enc.add_argument("--level", default="3.0", help="уровень H.264 (по умолчанию 3.0, как у эталона)")
    enc.add_argument("--pix-fmt", dest="pix_fmt", choices=["yuvj420p", "yuv420p"], default="yuvj420p")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert", parents=[enc], help="GIF -> MP4 240x320"); c.add_argument("src"); c.add_argument("dst")
    pv = sub.add_parser("preview", parents=[enc], help="PNG первого кадра (без USB)"); pv.add_argument("src"); pv.add_argument("dst")
    i = sub.add_parser("info", parents=[enc], help="ffprobe итогового MP4"); i.add_argument("file")
    p = sub.add_parser("plan", parents=[enc], help="dry-run: размер, SHA256, число пакетов"); p.add_argument("file")
    s = sub.add_parser("send", parents=[enc], help="РЕАЛЬНАЯ отправка на LCD"); s.add_argument("file")
    s.add_argument("--detach-kernel-driver", action="store_true")
    s.add_argument("--allow-unverified-media", action="store_true", help="разрешить не эталонный файл")
    s.add_argument("--no-select", action="store_true", help="не слать cmd50/cmd40 перед загрузкой (без них экран не менялся)")
    s.add_argument("--with-select", action="store_true", help=argparse.SUPPRESS)   # совместимость, теперь по умолчанию
    s.add_argument("--acknowledge-previous", action="store_true", help="я проверил состояние после прошлой незавершённой операции")
    def lcd_cmd(name, help_):
        q = sub.add_parser(name, help=help_)
        q.add_argument("--detach-kernel-driver", action="store_true")
        q.add_argument("--acknowledge-previous", action="store_true", help="я проверил экран после прошлой незавершённой операции")
        return q
    d = lcd_cmd("direction", "повернуть сам экран (0/180 подтверждены захватом, 90/270 - по анализу DLL)")
    d.add_argument("degrees", type=int, choices=list(DIRECTIONS))
    b = lcd_cmd("brightness", "яркость экрана 0..100 (сохраняется в экране, как в MSI Center)")
    b.add_argument("value", type=int)
    o = lcd_cmd("display", "включить/выключить экран (выкл = яркость 0, вкл = последняя сохранённая яркость)")
    o.add_argument("state", choices=["on", "off"])
    c = lcd_cmd("clock", "режим «Часы» (стиль 1 подтверждён захватом)")
    c.add_argument("--style", type=int, choices=[1, 2, 3, 4, 5], default=1)
    sub.add_parser("selftest", help="проверка сборки пакетов без USB")
    cl = sub.add_parser("cooling", help="ЭКСПЕРИМЕНТАЛЬНО: режимы кулера и передача температуры CPU")
    cs = cl.add_subparsers(dest="ccmd", required=True)
    cs.add_parser("status", help="найден ли кулер, температура CPU, последний отправленный режим")
    cset = cs.add_parser("set", help="отправить режим (по умолчанию dry-run)")
    cset.add_argument("mode", choices=list(COOLING_MODE_IDS)); cset.add_argument("--apply", action="store_true")
    cset.add_argument("--curves", help="JSON с кривыми для режима custom (по умолчанию ~/.config/s360-control/curves.json)")
    cs.add_parser("curves", help="вывести шаблон кривых (стандартные значения MSI) в JSON")
    cf = cs.add_parser("feed", help="цикл передачи температуры CPU (для systemd-сервиса)")
    cf.add_argument("--mode", choices=list(COOLING_MODE_IDS)); cf.add_argument("--interval", type=float, default=FEED_INTERVAL_S)
    a = ap.parse_args(argv)
    try:
        return _dispatch(a)
    except S360Error as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        raise SystemExit(2 if e.uncertain else 1)


def _dispatch(a):
    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "cooling":
        return _cooling_cli(a)
    if a.cmd in ("direction", "brightness", "display", "clock"):
        return _lcd_setting_cli(a)
    for k in ("fps", "max_seconds", "profile", "level", "pix_fmt", "scale", "focus_x", "focus_y", "rotate", "background",
              "edge_trim"):
        ENC[k] = getattr(a, k)
    if not (0.0 <= ENC["focus_x"] <= 1.0 and 0.0 <= ENC["focus_y"] <= 1.0):
        raise S360Error("--focus-x/--focus-y должны быть от 0 до 1")
    if a.cmd == "preview":
        preview_png(Path(a.src), Path(a.dst)); print("ok", a.dst); return
    if a.cmd == "convert":
        convert_gif(Path(a.src), Path(a.dst), a.fps); validate_mp4(Path(a.dst).read_bytes()); print("ok", a.dst); return
    if a.cmd == "info":
        data = load_media(Path(a.file), a.fps)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.mp4"; f.write_bytes(data)
            print("боксы:", [(t, sz) for t, _, sz in mp4_top_boxes(data)], "размер", len(data))
            subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                "stream=codec_name,profile,level,width,height,r_frame_rate,avg_frame_rate,nb_frames,duration,pix_fmt",
                "-of", "default=nw=1", str(f)], check=True, timeout=30)
        return
    data = load_media(Path(a.file), a.fps); sh = sha256(data)
    slot = slot_for_direction(int(load_settings()["device_direction"]))
    plan = build_upload_plan(data, slot)
    print(f"файл: {len(data)} байт, SHA256 {sh}\nпакетов: {len(plan)} (данных {len(plan) - 3}), слот {slot.decode()}")
    print("совпадает с эталоном" if sh == REFERENCE_SHA256 else "НЕ эталон (совместимость с прошивкой не доказана)")
    dur, frames, rate = probe_mp4(data)
    if dur is not None:
        print(f"длительность {dur:.2f} с, кадров {frames}, fps {rate}"
              + ("  — ВЫШЕ проверенного (до 15 fps и ~2,5 с): возможно зависание" if beyond_tested(dur, rate) else "  — в проверенных пределах"))
    if a.cmd == "plan":
        print("dry-run: ничего не отправлено"); return
    if sh != REFERENCE_SHA256 and not a.allow_unverified_media:
        raise S360Error("Не эталонный файл. Первая проверка - только эталон, либо --allow-unverified-media.")
    st = read_state()
    if st and st["status"] in ("IN_PROGRESS", "UNCERTAIN") and not a.acknowledge_previous:
        raise S360Error(f"Прошлая операция: {st['status']}. Сначала проверь экран, потом --acknowledge-previous.")
    full = ([] if a.no_select else build_select_plan()) + plan
    print(f"\nЭТО ПЕРЕЗАПИШЕТ слот на LCD ({slot.decode()}). Отката нет.")
    if input(f"Введи первые 8 символов SHA256 ({sh[:8]}) для подтверждения: ").strip() != sh[:8]:
        raise S360Error("Не подтверждено. Ничего не отправлено.")
    do_send(full, a.detach_kernel_driver, sh)
    print("Хост отправил все пакеты. Принял ли LCD файл - смотри на экран (ACK в протоколе нет).")


def _lcd_setting_cli(a):
    """Яркость / вкл-выкл / поворот / часы: показать, что уйдёт, спросить, отправить одной операцией без повторов."""
    cfg = load_settings()
    if a.cmd == "direction":
        plan, kind, tag = build_direction_plan(a.degrees), "direction", f"direction:{a.degrees}"
        text = (f"Поворот экрана {a.degrees}°: cmd 1a + сохранение в HMI_FW_Setting.ini + повторный выбор слота S7 "
                f"(как MSI Center).\n0° и 180° подтверждены твоим захватом; 90°/270° и слот R1 — по анализу DLL.")
    elif a.cmd == "brightness":
        plan, kind, tag = build_brightness_plan(a.value), "brightness", f"brightness:{a.value}"
        text = f"Яркость {a.value}%: cmd 18 + сохранение в HMI_FW_Setting.ini (запись во флеш экрана, не гоняй циклом)."
    elif a.cmd == "display":
        val = 0 if a.state == "off" else int(cfg.get("brightness") or 100)
        plan, kind, tag = build_brightness_plan(val), "brightness", f"brightness:{val}"
        text = f"Экран {'выкл (яркость 0)' if val == 0 else f'вкл (яркость {val}%)'}: так делает тумблер в MSI Center (захват 12)."
    else:
        plan, kind, tag = build_clock_plan(a.style), "clock", f"clock:{a.style}"
        text = (f"Режим «Часы», стиль {a.style}: cmd 36 (местное время) + cmd 50. Стиль 1 подтверждён захватом; "
                f"остальные — по сдвигу флага. Режим часов у экрана держится, пока не выберешь другой.")
    st = read_state()
    if st and st["status"] in ("IN_PROGRESS", "UNCERTAIN") and not a.acknowledge_previous:
        raise S360Error(f"Прошлая операция: {st['status']}. Сначала проверь экран, потом --acknowledge-previous.")
    print(text + f"\nПакетов: {len(plan)}. Результат смотри на экране (подтверждения от устройства нет).")
    if input("Введи y для подтверждения: ").strip().lower() != "y":
        raise S360Error("Не подтверждено. Ничего не отправлено.")
    do_send(plan, a.detach_kernel_driver, tag, kind=kind)
    if a.cmd == "direction":
        cfg["device_direction"] = a.degrees
    elif kind == "brightness" and plan[0][1][9] > 0:
        cfg["brightness"] = plan[0][1][9]
    save_settings(cfg)
    print("Хост отправил. Посмотри на экран.")


def _cooling_cli(a):
    if a.ccmd == "curves":
        print(json.dumps(DEFAULT_CURVES, indent=1)); return
    if a.ccmd == "status":
        hp = find_hidraw()
        print("кулер 0db0:6a05 в USB:", "да" if usb_present(COOLER_VID, COOLER_PID) else "нет")
        print("hidraw-узел (интерфейс 1):", hp or "не найден")
        try: print("температура CPU:", read_cpu_temp(), "°C")
        except S360Error as e: print("температура CPU:", e)
        try: print("последний отправленный режим:", json.loads(COOLING_STATE.read_text())["mode"], "(обратного чтения нет)")
        except (OSError, ValueError, KeyError): print("последний отправленный режим: нет данных")
        return
    if a.ccmd == "set":
        curves = load_curves(a.curves) if (a.mode == "custom" and a.curves) else None
        reports = cooling_set(a.mode, apply=False, curves=curves)
        for r in reports:
            print(r[:42].hex(" "))
        if not a.apply:
            print("dry-run: ничего не отправлено (добавь --apply)"); return
        print("\nФормат сверен с захватами MSI Center этого устройства, но с Linux ещё не пробовался. Режим работает только вместе с "
              "передачей температуры CPU (cooling feed / сервис s360-feed).")
        if input(f"Введи название режима ({a.mode}) для подтверждения: ").strip() != a.mode:
            raise S360Error("Не подтверждено. Ничего не отправлено.")
        cooling_set(a.mode, apply=True, curves=curves); print("отправлено"); return
    stop_flag = {"v": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop_flag.__setitem__("v", True))
    def interruptible_sleep(sec):
        end = time.monotonic() + sec
        while time.monotonic() < end and not stop_flag["v"]:
            time.sleep(0.1)
    cooling_feed(a.mode, a.interval, stop=lambda: stop_flag["v"], log=lambda m: print(m, flush=True), sleep=interruptible_sleep)


# ----------------------------------------------------------------------------- selftest
def selftest():
    data = bytes((i * 7) % 251 for i in range(REFERENCE_SIZE))
    plan = build_upload_plan(data)
    assert len(plan) == 229, len(plan)
    assert all(len(p) == 64 for r, p in plan if r == "control")
    assert all(len(p) == 1024 and p[0] == 2 for r, p in plan if r == "interrupt")
    c20 = plan[0][1]
    assert c20[:5] == b"\x01\x6b\x5a\x20\x00" and c20[5:9] == (18).to_bytes(4, "little")
    assert c20[9:13] == REFERENCE_SIZE.to_bytes(4, "little") and c20[13:27] == SLOT_NAME
    assert plan[1][1][3] == 0x22 and plan[1][1][5:9] == REFERENCE_SIZE.to_bytes(4, "little")
    assert plan[-1][1][3] == 0x26 and plan[-1][1][5:9] == bytes(4)
    assert b"".join(p[1:] for r, p in plan if r == "interrupt")[:REFERENCE_SIZE] == data
    assert len(build_select_plan()) == 9 and all(len(p) == 64 for _, p in build_select_plan())
    dp = build_direction_plan(90)
    assert len(dp) == 1 + 4 + 9, len(dp)
    d90 = dp[0][1]
    assert d90[:9] == b"\x01\x6b\x5a\x1a\x00\x04\x00\x00\x00" and d90[9:13] == (90).to_bytes(4, "little") and len(d90) == 64
    assert slot_for_direction(0) == SLOT_NAME == slot_for_direction(180) and slot_for_direction(270) == b"M1-S7-V-R1.mp4"
    assert not beyond_tested(2.47, "15/1") and not beyond_tested(1.95, "20/3") and beyond_tested(3.0, "15/1") and beyond_tested(1.0, "30/1")
    r1 = build_upload_plan(data, slot_for_direction(90))
    assert len(r1) == 229 and r1[0][1][13:27] == b"M1-S7-V-R1.mp4"
    assert math.ceil(REFERENCE_SIZE / 1023) == 226
    # --- сверка с байтами из захватов MSI Center (этого устройства)
    sv = build_save_plan()
    assert len(sv) == 4 and sv[0][1][3:5] == b"\x20\x00" and sv[0][1][5:9] == (22).to_bytes(4, "little")
    assert sv[0][1][9:13] == (3).to_bytes(4, "little") and sv[0][1][13:31] == INI_NAME
    assert sv[1][1][3] == 0x22 and sv[1][1][5:9] == (3).to_bytes(4, "little")
    assert sv[2][0] == "interrupt" and sv[2][1][:4] == b"\x02MSI" and sv[3][1][3] == 0x26
    b50 = build_brightness_plan(50)
    assert b50[0][1][:13].hex() == "016b5a1800" "04000000" "32000000"        # захват 9: cmd18, len 4, значение 0x32
    assert build_brightness_plan(0)[0][1][9] == 0 and build_brightness_plan(100)[0][1][9] == 100
    assert dp[0][1][9] == 90 and dp[5][1][3] == 0x50                           # поворот, затем сохранение, затем выбор слота
    assert build_direction_plan(180)[0][1][9:13] == bytes([0xB4, 0, 0, 0])      # захват 11: 180 = 0xb4
    ck = build_clock_plan(1, now=1793743945, tz_offset=10800)                  # захват 1
    assert ck[0][1][9:21].hex() == "01000000" "00000000" "7986ea6a" and ck[1][1][9:12].hex() == "050005" and ck[1][1][12] == 1
    for bad in (-1, 101, "5", True):
        try: build_brightness_plan(bad); assert False
        except S360Error: pass
    # кулер: байты d0 40/41 из захватов 2 (custom=3), 3 (silent=0), 4 (game=2)
    CAP = {3: ("d04003151c2a64000000031c284664000000031c284664000000030d172335000000033c3c5964" + "00" * 25,
               "d04103303f4f51000000032328465100000003232846510000000333404b51000000033b444b51" + "00" * 25),
           0: ("d04000151c2a64000000001c284664000000001c284664000000000d172335000000003c3c5964" + "00" * 25,
               "d04100303f4f51000000002328465100000000232846510000000033404b51000000003b444b51" + "00" * 25),
           }
    for mode in (0, 3):
        r = cooler_mode_reports(mode)
        assert r[0].hex() == CAP[mode][0] and r[1].hex() == CAP[mode][1], (mode, r[0].hex())
    r40, r41 = cooler_mode_reports(2)
    assert r40.hex().startswith("d04002151c2a6400000002") and r41.hex().startswith("d04102303f4f5100000002")
    assert all(r40[o] == 2 for o in (2, 10, 18, 26, 34)) and len(r40) == len(r41) == 64
    cu = json.loads(json.dumps(DEFAULT_CURVES)); cu["radiator1"]["duty"][0] = 2; cu["radiator1"]["temp"][0] = 47
    assert build_cooler_reports(3, cu)[0][3] == 2 and build_cooler_reports(3, cu)[1][3] == 47   # захват 7
    cu["pump"]["duty"][0] = 40
    try: build_cooler_reports(3, cu); assert False
    except S360Error: pass
    cu2 = json.loads(json.dumps(DEFAULT_CURVES)); cu2["pump"]["temp"] = [59, 59, 75, 81]
    try: validate_curves(cu2); assert False
    except S360Error: pass
    f = feed_report(57)
    assert len(f) == 64 and f[:2] == b"\xd0\x85" and f[2:4] == (3000).to_bytes(2, "little") and f[4] == 57 and f[5] == 0
    # поиск hidraw и hwmon на поддельном sysfs
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        iface = t / "sys/devices/usb1/1-6/1-6:1.1"; hid = iface / "0003:0DB0:6A05.0005"
        (hid / "hidraw/hidraw3").mkdir(parents=True); (hid / "uevent").write_text("DRIVER=hid-generic\nHID_ID=0003:00000DB0:00006A05\n")
        (iface / "bInterfaceNumber").write_text("01\n")
        iface0 = t / "sys/devices/usb1/1-6/1-6:1.0"; hid0 = iface0 / "0003:0DB0:6A05.0004"; hid0.mkdir(parents=True)
        (hid0 / "uevent").write_text("HID_ID=0003:00000DB0:00006A05\n"); (iface0 / "bInterfaceNumber").write_text("00\n")
        (t / "sys/class/hidraw").mkdir(parents=True)
        (t / "sys/class/hidraw/hidraw3").symlink_to(hid / "hidraw/hidraw3")
        (hid / "hidraw/hidraw3/device").symlink_to(hid)
        (t / "sys/class/hidraw/hidraw1").mkdir(); (t / "sys/class/hidraw/hidraw1/device").symlink_to(hid0)
        assert find_hidraw(sys_root=t / "sys", dev_root=Path("/dev")) == Path("/dev/hidraw3")
        assert find_hidraw(pid=0x9BA6, sys_root=t / "sys") is None
        hw = t / "hwmon"; (hw / "hwmon0").mkdir(parents=True); (hw / "hwmon1").mkdir()
        (hw / "hwmon0/name").write_text("nct6687\n"); (hw / "hwmon1/name").write_text("coretemp\n")
        (hw / "hwmon1/temp1_input").write_text("56000\n")
        assert read_cpu_temp(hw) == 56
        (hw / "hwmon1/temp1_input").write_text("999999\n")
        try: read_cpu_temp(hw); assert False
        except S360Error: pass
        # feed: пишет d0 85 только при известной температуре, режим один раз (запись подменена на запись в список)
        sent, orig, orig_state = [], globals()["_write_reports"], globals()["COOLING_STATE"]
        globals()["_write_reports"] = lambda path, reports: sent.extend(reports)
        globals()["COOLING_STATE"] = t / "cooling-test.json"      # тест не должен трогать реальное состояние
        try:
            calls = {"n": 0}
            def temps():
                calls["n"] += 1
                if calls["n"] == 2: raise S360Error("нет температуры")
                return 50 + calls["n"]
            ticks = iter([False, False, False, True])
            cooling_feed("silent", 0, stop=lambda: next(ticks), log=lambda m: None, sleep=lambda s: None,
                         hid_path=Path("/dev/null"), temp_reader=temps)
        finally:
            globals()["_write_reports"] = orig
            globals()["COOLING_STATE"] = orig_state
        assert [r[1] for r in sent] == [0x40, 0x41, 0x40, 0x41, 0x85, 0x85], [r[1] for r in sent]   # пара дважды, один тик пропущен
        assert sent[4][4] == 51 and sent[5][4] == 53
    print("selftest ok: LCD, яркость, поворот, часы, кулер d0 40/41/85 сверены с захватами MSI Center; hidraw, hwmon, feed")


if __name__ == "__main__":
    main()
