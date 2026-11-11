#!/usr/bin/env bash
# Установка S360 Control для Arch / CachyOS. Без root для файлов приложения;
# sudo спрашивается отдельно (пакеты и udev-правило). --no-system пропускает обе sudo-части.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${S360_PREFIX:-$HOME/.local/share/s360-control}"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
UNITS="$HOME/.config/systemd/user"
RULE=/etc/udev/rules.d/70-s360-control.rules
SYSTEM=1
case "${1:-}" in
  "") ;;
  --no-system) SYSTEM=0 ;;
  --help|-h) echo "Установка S360 Control: ./install.sh [--no-system]"; exit 0 ;;
  *) echo "Неизвестный аргумент: $1" >&2; exit 2 ;;
esac

# Check the complete distribution before touching an existing installation.
for file in uninstall.sh s360_lcd.py s360_gui.py theme.py monitor.py thumbs.py pan.py assets/s360-control.png packaging/s360-feed.service packaging/70-s360-control.rules; do
  [[ -f "$HERE/$file" ]] || { echo "Неполный архив: отсутствует $file" >&2; exit 1; }
done

ask() { local a; read -r -p "$1 [y/N] " a || a=n; [[ "$a" =~ ^[YyДд] ]]; }

if (( SYSTEM )); then
  if command -v pacman >/dev/null; then
    missing=()
    for p in pyside6 python-pyusb ffmpeg; do pacman -Qq "$p" >/dev/null 2>&1 || missing+=("$p"); done
    if (( ${#missing[@]} )); then
      echo "Не хватает пакетов: ${missing[*]}"
      if ask "Установить через sudo pacman -S --needed ${missing[*]}?"; then
        sudo pacman -S --needed "${missing[@]}"
      else
        echo "Пропущено. Без них приложение не запустится."
      fi
    else
      echo "Зависимости уже установлены."
    fi
  else
    echo "pacman не найден: поставь вручную PySide6, pyusb и ffmpeg."
  fi
fi

for tool in python3 ffmpeg ffprobe; do
  command -v "$tool" >/dev/null || { echo "Не найден $tool. Установи зависимости и повтори установку." >&2; exit 1; }
done
python3 -c 'import PySide6, usb.core' || { echo "Нужны PySide6 и pyusb для выбранного python3." >&2; exit 1; }

install -Dm644 "$HERE/s360_lcd.py" "$APP/s360_lcd.py"
install -Dm644 "$HERE/s360_gui.py" "$APP/s360_gui.py"
install -Dm644 "$HERE/theme.py" "$APP/theme.py"
install -Dm644 "$HERE/monitor.py" "$APP/monitor.py"
install -Dm644 "$HERE/thumbs.py" "$APP/thumbs.py"
install -Dm644 "$HERE/pan.py" "$APP/pan.py"
install -Dm644 "$HERE/assets/s360-control.png" "$APP/assets/s360-control.png"
install -Dm644 "$HERE/assets/s360-control.png" "$HOME/.local/share/icons/hicolor/512x512/apps/s360-control.png"
install -Dm755 "$HERE/uninstall.sh" "$APP/uninstall.sh"
mkdir -p "$BIN"
printf '#!/usr/bin/env bash\nexec python3 %q "$@"\n' "$APP/s360_gui.py" > "$BIN/s360-control"
printf '#!/usr/bin/env bash\nexec python3 %q "$@"\n' "$APP/s360_lcd.py" > "$BIN/s360-lcd"
printf '#!/usr/bin/env bash\nexec python3 %q "$@"\n' "$APP/monitor.py" > "$BIN/s360-monitor"
chmod 755 "$BIN/s360-control" "$BIN/s360-lcd" "$BIN/s360-monitor"

mkdir -p "$APPS"
# Desktop Entry quoting differs from shell quoting (also escape literal field codes).
desktop_exec=${BIN//\\/\\\\}
desktop_exec=${desktop_exec//\"/\\\"}
desktop_exec=${desktop_exec//\$/\\\$}
desktop_exec=${desktop_exec//\`/\\\`}
desktop_exec=${desktop_exec//%/%%}
cat > "$APPS/s360-control.desktop" << DESK
[Desktop Entry]
Type=Application
Name=S360 Control
Comment=MSI CoreLiquid S360: LCD и охлаждение
Exec="$desktop_exec/s360-control"
Icon=s360-control
Terminal=false
Categories=System;
StartupWMClass=s360-control
Keywords=S360;MSI;LCD;Cooling;
DESK

if command -v gtk-update-icon-cache >/dev/null; then
  gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

install -Dm644 "$HERE/packaging/s360-feed.service" "$UNITS/s360-feed.service"
command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null || true
echo "Сервис s360-feed установлен, но НЕ включён и не запущен (запуск — со страницы «Охлаждение»)."

if (( SYSTEM )); then
  echo; echo "Права на USB без root (udev-правило, только для 0db0:9ba6 и 0db0:6a05):"
  sed 's/^/    /' "$HERE/packaging/70-s360-control.rules"
  if ask "Установить $RULE через sudo?"; then
    sudo install -Dm644 "$HERE/packaging/70-s360-control.rules" "$RULE"
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=usb --subsystem-match=hidraw
    echo "Готово. Если доступ не появился, переподключи USB-устройство или перезайди в сессию."
  fi
fi

echo; echo "Установлено: $APP"
echo "GUI: s360-control (или из меню приложений); CLI: s360-lcd --help"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Добавь $BIN в PATH." ;; esac
