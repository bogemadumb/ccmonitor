#!/bin/bash
# Установщик CCMonitor. Запуск: в Терминале `bash install.sh`, либо правый клик -> Открыть.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "== Установка CCMonitor =="

# 1. python3
if ! command -v python3 >/dev/null 2>&1 && [ ! -x /usr/bin/python3 ]; then
  echo "Нужен python3. Установи инструменты разработчика командой:"
  echo "    xcode-select --install"
  echo "затем запусти install.sh снова."
  exit 1
fi

# 2. проверка входа в Claude Code (не блокирует, только предупреждает)
if ! security find-generic-password -s "Claude Code-credentials" >/dev/null 2>&1; then
  echo ""
  echo "! Не найден вход в Claude Code."
  echo "  Приложение показывает ТВОИ лимиты Claude Code — установи и залогинься в Claude Code,"
  echo "  иначе данные не появятся. Установку продолжаю."
  echo ""
fi

# 3. копируем .app в ~/Applications и снимаем карантин (неподписанное приложение)
mkdir -p "$HOME/Applications"
rm -rf "$HOME/Applications/CCMonitor.app"
cp -R "$DIR/CCMonitor.app" "$HOME/Applications/CCMonitor.app"
xattr -dr com.apple.quarantine "$HOME/Applications/CCMonitor.app" 2>/dev/null || true

# 4. автозапуск (LaunchAgent под текущего пользователя)
PLIST="$HOME/Library/LaunchAgents/com.ccmonitor.app.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.ccmonitor.app</string>
  <key>ProgramArguments</key><array>
    <string>$HOME/Applications/CCMonitor.app/Contents/MacOS/CCMonitor</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
EOF

U=$(id -u)
launchctl bootout "gui/$U/com.ccmonitor.app" 2>/dev/null || true
launchctl bootstrap "gui/$U" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST" 2>/dev/null || true

echo ""
echo "Готово. CCMonitor запущен и будет стартовать при входе в систему."
echo "Иконка со статусом — в строке меню справа вверху."
echo "При первом обращении к паролю Claude Code нажми «Всегда разрешать»."
