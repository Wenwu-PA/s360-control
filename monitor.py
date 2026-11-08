#!/usr/bin/env python3
"""monitor.py - чтение оборотов кулера S360 и температур GPU для главного экрана (только чтение).

Обороты: опрос статуса `d0 31` (64 байта, как у MSI Center) и разбор ответа. Раскладка сверена с захватами 2 и 3 и со скриншотом MSI Center
(радиатор 444 об/мин, помпа 2255, вентилятор водоблока 0): u16 little-endian на смещениях 2 (радиатор), 8 (вентилятор водоблока), 10 (помпа).
Температуру жидкости НЕ читаем: байт 14 = 39 в захватах похож на неё, но не подтверждён.
GPU: AMD — hwmon amdgpu (edge / junction / mem); NVIDIA — nvidia-smi (ядро; hotspot и VRAM драйвер Linux у GeForce обычно не отдаёт).
Ничего не пишет на устройство, кроме запроса статуса `d0 31`. Использование: python3 monitor.py rpm | gpu | all | selftest"""
import json, os, select, struct, subprocess, sys, time
from pathlib import Path

import s360_lcd as core

QUERY = bytes([0xD0, 0x31]) + bytes(62)            # как в захватах MSI Center: d0 31 и нули, 64 байта


def parse_cooler_status(b: bytes) -> dict:
    """Ответ `d0 31` -> обороты (об/мин) и текущая мощность (%). Остальные каналы (2, 3) не подключены и не возвращаются."""
    if len(b) != 64 or b[:2] != b"\xd0\x31":
        raise core.S360Error("неожиданный ответ кулера")
    u = lambda o: struct.unpack_from("<H", b, o)[0]
    return {"rpm": {"radiator": u(2), "waterblock": u(8), "pump": u(10)},
            "duty": {"radiator": u(22), "waterblock": u(28), "pump": u(30)}}


def read_cooler_status(fd=None, hid_path=None, timeout=1.5) -> dict:
    """Запрос статуса и ответ. fd можно передать для тестов; иначе открывается hidraw кулера (O_RDWR, неблокирующий)."""
    own = fd is None
    if own:
        path = hid_path or core.find_hidraw()
        if path is None:
            raise core.S360Error("кулер не найден")
        try:
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            raise core.S360Error(f"нет доступа к кулеру ({e})")
    try:
        while select.select([fd], [], [], 0)[0]:            # выбросить накопившиеся отчёты
            try:
                if not os.read(fd, 64):
                    break
            except BlockingIOError:
                break
        try:
            if os.write(fd, QUERY) != 64:
                raise core.S360Error("короткая запись запроса статуса")
        except OSError as e:
            raise core.S360Error(f"ошибка запроса статуса ({e})")
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise core.S360Error("кулер не ответил")
            if not select.select([fd], [], [], left)[0]:
                continue
            try:
                data = os.read(fd, 64)
            except BlockingIOError:
                continue
            except OSError as e:
                raise core.S360Error(f"ошибка чтения ({e})")
            if len(data) == 64 and data[:2] == b"\xd0\x31":
                return parse_cooler_status(data)
    finally:
        if own:
            os.close(fd)


def _temp(v):
    try:
        t = int(round(float(v)))
        return t if 0 < t <= 130 else None
    except (TypeError, ValueError):
        return None


def _smi() -> str:
    return subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,temperature.memory", "--format=csv,noheader,nounits"],
                          capture_output=True, text=True, timeout=3, check=True).stdout


def read_gpu_temps(hwmon_root=Path("/sys/class/hwmon"), smi=_smi) -> dict:
    """{'vendor', 'core', 'hotspot', 'vram'}: недоступное — None (в интерфейсе такое не показывать)."""
    out = {"vendor": None, "core": None, "hotspot": None, "vram": None}
    try:
        hw = sorted(hwmon_root.glob("hwmon*"))
    except OSError:
        hw = []
    for h in hw:                                              # AMD
        try:
            if (h / "name").read_text().strip() != "amdgpu":
                continue
        except OSError:
            continue
        out["vendor"] = "amd"
        for i in (1, 2, 3):
            try:
                label = (h / f"temp{i}_label").read_text().strip()
                val = _temp(int((h / f"temp{i}_input").read_text()) / 1000)
            except (OSError, ValueError):
                continue
            key = {"edge": "core", "junction": "hotspot", "mem": "vram"}.get(label)
            if key:
                out[key] = val
        return out
    try:                                                      # NVIDIA
        line = smi().strip().splitlines()[0]
        core_t, _, mem_t = line.partition(",")
        if _temp(core_t) is not None:
            out.update(vendor="nvidia", core=_temp(core_t), vram=_temp(mem_t.strip().strip("[]")))
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return out


def selftest():
    import socket, threading, tempfile
    R2 = bytes.fromhex("d031cc01000000000000e008000027067d007d00000016003300330000003c000000000000000000000000000000000000000000000000000000000000000000")
    R3 = bytes.fromhex("d0313e02000000000000cf08000027077d007d0000001d002a002a00000039000000000000000000000000000000000000000000000000000000000000000000")
    assert len(R2) == len(R3) == 64
    s = parse_cooler_status(R2)                                 # захват 2: радиатор 460, помпа 2272; мощность 22/0/60
    assert s["rpm"] == {"radiator": 460, "waterblock": 0, "pump": 2272} and s["duty"] == {"radiator": 22, "waterblock": 0, "pump": 60}, s
    s = parse_cooler_status(R3)                                 # захват 3: 574 / 0 / 2255, мощность 29/0/57
    assert s["rpm"] == {"radiator": 574, "waterblock": 0, "pump": 2255} and s["duty"]["pump"] == 57, s
    for bad in (b"", R2[:63], b"\xd0\x85" + R2[2:]):
        try: parse_cooler_status(bad); assert False
        except core.S360Error: pass
    # обмен через пару сокетов: устройство сначала присылает старый отчёт, потом отвечает на запрос
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    b.send(R2)                                                  # «залежавшийся» отчёт должен быть выброшен
    def dev():
        got = b.recv(64); assert got == QUERY, got
        b.send(R3)
    t = threading.Thread(target=dev); t.start()
    assert read_cooler_status(fd=a.fileno(), timeout=2)["rpm"]["pump"] == 2255
    t.join()
    try: read_cooler_status(fd=a.fileno(), timeout=0.2); assert False        # устройство молчит
    except core.S360Error: pass
    a.close(); b.close()
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        h = d / "hwmon3"; h.mkdir(); (h / "name").write_text("amdgpu\n")
        for i, (lab, v) in enumerate((("edge", 45000), ("junction", 57000), ("mem", 52000)), 1):
            (h / f"temp{i}_label").write_text(lab + "\n"); (h / f"temp{i}_input").write_text(f"{v}\n")
        assert read_gpu_temps(d, smi=lambda: "") == {"vendor": "amd", "core": 45, "hotspot": 57, "vram": 52}
    empty = Path(tempfile.mkdtemp())
    assert read_gpu_temps(empty, smi=lambda: "47, [N/A]\n") == {"vendor": "nvidia", "core": 47, "hotspot": None, "vram": None}
    assert read_gpu_temps(empty, smi=lambda: "47, 62\n")["vram"] == 62
    def boom(): raise FileNotFoundError("нет nvidia-smi")
    assert read_gpu_temps(empty, smi=boom) == {"vendor": None, "core": None, "hotspot": None, "vram": None}
    print("monitor selftest ok: статус кулера (захваты 2, 3), обмен с повторным опросом, GPU AMD/NVIDIA")


def main(argv=None):
    cmd = (argv or sys.argv[1:] or ["all"])[0]
    if cmd == "selftest":
        return selftest()
    try:
        if cmd in ("rpm", "all"):
            s = read_cooler_status()
            print("обороты, об/мин:", s["rpm"], "| мощность, %:", s["duty"])
        if cmd in ("gpu", "all"):
            print("GPU:", json.dumps(read_gpu_temps(), ensure_ascii=False))
    except core.S360Error as e:
        print("ОШИБКА:", e, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
