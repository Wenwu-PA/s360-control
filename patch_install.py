#!/usr/bin/env python3
"""Добавляет в install.sh копирование monitor.py и команду s360-monitor. Запуск из папки проекта; повторный запуск ничего не меняет."""
import re, sys
from pathlib import Path
p = Path("install.sh"); s = p.read_text()
if "monitor.py" in s:
    print("уже применено"); sys.exit(0)
copy = 'install -Dm644 "$HERE/monitor.py" "$APP/monitor.py"\n'
m = re.search(r'^install -Dm644 "\$HERE/theme\.py".*\n', s, re.M) or re.search(r'^install -Dm644 "\$HERE/s360_gui\.py".*\n', s, re.M)
if not m: sys.exit("не нашёл строку копирования s360_gui.py в install.sh")
s = s[:m.end()] + copy + s[m.end():]
m = re.search(r'^printf .*s360_lcd\.py.*\n', s, re.M)
if not m: sys.exit("не нашёл создание команды s360-lcd")
s = s[:m.end()] + 'printf \'#!/usr/bin/env bash\\nexec python3 "%s/monitor.py" "$@"\\n\' "$APP" > "$BIN/s360-monitor"\n' + s[m.end():]
s, n = re.subn(r'^chmod 755 (.*)$', lambda mm: 'chmod 755 ' + mm.group(1) + ' "$BIN/s360-monitor"', s, count=1, flags=re.M)
if not n: sys.exit("не нашёл chmod в install.sh")
p.write_text(s); print("ok: monitor.py и s360-monitor добавлены в install.sh")
