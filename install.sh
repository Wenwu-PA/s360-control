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
SYSTEM=1; [[ "${1:-}" == "--no-system" ]] && SYSTEM=0

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

install -Dm644 "$HERE/s360_lcd.py" "$APP/s360_lcd.py"
install -Dm644 "$HERE/s360_gui.py" "$APP/s360_gui.py"
install -Dm644 "$HERE/theme.py" "$APP/theme.py"
install -Dm644 "$HERE/monitor.py" "$APP/monitor.py"
install -Dm644 "$HERE/thumbs.py" "$APP/thumbs.py"
install -Dm644 "$HERE/pan.py" "$APP/pan.py"
mkdir -p "$BIN"
printf '#!/usr/bin/env bash\nexec python3 "%s/s360_gui.py" "$@"\n' "$APP" > "$BIN/s360-control"
printf '#!/usr/bin/env bash\nexec python3 "%s/s360_lcd.py" "$@"\n' "$APP" > "$BIN/s360-lcd"
printf '#!/usr/bin/env bash\nexec python3 "%s/monitor.py" "$@"\n' "$APP" > "$BIN/s360-monitor"
chmod 755 "$BIN/s360-control" "$BIN/s360-lcd" "$BIN/s360-monitor"

mkdir -p "$APPS"
cat > "$APPS/s360-control.desktop" << DESK
[Desktop Entry]
Type=Application
Name=S360 Control
Comment=MSI CoreLiquid S360: LCD и охлаждение
Exec=$BIN/s360-control
Icon=video-display
Terminal=false
Categories=Utility;System;
StartupWMClass=s360-control
DESK

install -Dm644 "$HERE/packaging/s360-feed.service" "$UNITS/s360-feed.service"
command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null || true
echo "Сервис s360-feed установлен, но НЕ включён и не запущен (запуск — со страницы Cooling)."

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
