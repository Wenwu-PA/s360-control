#!/usr/bin/env python3
"""check_capture.py - быстрая проверка USBPcap-записи: есть ли в ней водянка MSI S360.
Использование:  python check_capture.py файл [файл ...]
Читает только заголовки пакетов (классический .pcap из USBPcapCMD), ничего не меняет.
Роль устройства определяет ПО ТРАФИКУ (в записях USBPcap VID:PID из описаний бывают сдвинуты относительно адресов):
  экран S360: отчёты 01 6b 5a ...;  кулер S360: 64-байтные отчёты d0 xx;  Skydimo: пакеты 'Ada'."""
import struct, sys, collections

def check(path):
    roles, pk = {}, collections.Counter()
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic == b"\x0a\x0d\x0d\x0a":
            return print(f"{path}: формат pcapng — сохрани как .pcap (запись через USBPcapCMD даёт .pcap)")
        if magic != b"\xd4\xc3\xb2\xa1":
            return print(f"{path}: не похоже на .pcap")
        f.read(20)
        while True:
            h = f.read(16)
            if len(h) < 16: break
            incl = struct.unpack_from("<I", h, 8)[0]
            d = f.read(incl)
            if len(d) < 27: continue
            hl, irp, st, fn, info, bus, dev, ep, tr, dl = struct.unpack_from("<HQIHBHHBBI", d, 0)
            pk[(bus, dev)] += 1
            pl = d[hl:]
            if dl == 0 or (info & 1):
                continue
            if tr == 2 and len(pl) >= 12 and pl[:2] == b"\x21\x09" and pl[8:11] == b"\x01\x6b\x5a":
                roles[(bus, dev)] = "экран S360 (LCD)"
            elif tr == 1 and not (ep & 0x80) and dl == 64 and pl[:1] == b"\xd0":
                roles[(bus, dev)] = "кулер S360 (помпа/вентиляторы)"
            elif tr == 3 and pl[:3] == b"Ada":
                roles[(bus, dev)] = "Skydimo/светодиоды (не S360)"
    print(f"\n{path}")
    for k in sorted(pk):
        print(f"  шина {k[0]}, адрес {k[1]}: {roles.get(k, ''):34s} пакетов {pk[k]}")
    have = set(roles.values())
    lcd, cool = "экран S360 (LCD)" in have, "кулер S360 (помпа/вентиляторы)" in have
    if lcd and cool:
        print("  ВЕРДИКТ: в записи есть и экран, и кулер S360.")
    elif lcd or cool:
        print("  ВЕРДИКТ: есть только", "экран" if lcd else "кулер", "S360 (может быть нормально для этого действия).")
    else:
        print("  ВЕРДИКТ: S360 в записи НЕТ (за время записи ни одной команды экрана/кулера). Проверь хаб и устройства в USBPcapCMD.")

for p in sys.argv[1:]:
    check(p)
if len(sys.argv) < 2:
    print(__doc__)
