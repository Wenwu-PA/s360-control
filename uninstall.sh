#!/usr/bin/env bash
# Полное удаление S360 Control. Спрашивает отдельно про настройки, журнал LCD и udev-правило.
set -euo pipefail
APP="${S360_PREFIX:-$HOME/.local/share/s360-control}"
BIN="$HOME/.local/bin"
RULE=/etc/udev/rules.d/70-s360-control.rules
SYSTEM=1; [[ "${1:-}" == "--no-system" ]] && SYSTEM=0
ask() { local a; read -r -p "$1 [y/N] " a || a=n; [[ "$a" =~ ^[YyДд] ]]; }

if command -v systemctl >/dev/null; then
  systemctl --user stop s360-feed.service 2>/dev/null || true
  systemctl --user disable s360-feed.service 2>/dev/null || true
fi
rm -f "$HOME/.config/systemd/user/s360-feed.service"
command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null || true
rm -rf "$APP"
rm -f "$BIN/s360-control" "$BIN/s360-lcd" "$HOME/.local/share/applications/s360-control.desktop"
echo "Приложение, лаунчеры, .desktop и сервис удалены."

CONF="${XDG_CONFIG_HOME:-$HOME/.config}/s360-control"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/s360-lcd"
[[ -d "$CONF" ]] && ask "Удалить настройки ($CONF)?" && rm -rf "$CONF" || true
[[ -d "$STATE" ]] && ask "Удалить журнал LCD и состояние кулера ($STATE)? Журнал защищает от повторной записи при неизвестном состоянии экрана." && rm -rf "$STATE" || true
if (( SYSTEM )) && [[ -f "$RULE" ]]; then
  if ask "Удалить udev-правило $RULE через sudo?"; then
    sudo rm -f "$RULE"; sudo udevadm control --reload-rules
  fi
fi
echo "Готово. Зависимости (pyside6, python-pyusb, ffmpeg) не удалялись."
