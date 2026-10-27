#!/bin/bash
# Пересборка и установка CCMonitor: компилирует Swift (universal, если доступно),
# собирает .app с вложенными collector.py и codex-usage-local.py, ставит в ~/Applications и перезапускает.
set -e
cd "$(dirname "$0")"

ARM=/tmp/cc-arm64; X86=/tmp/cc-x86; BIN=/tmp/ccmonitor-build
swiftc main.swift -o "$ARM" -target arm64-apple-macos13 -framework AppKit -O
if swiftc main.swift -o "$X86" -target x86_64-apple-macos13 -framework AppKit -O 2>/dev/null; then
  lipo -create "$ARM" "$X86" -o "$BIN"; rm -f "$X86"
  echo "собран universal (arm64 + x86_64)"
else
  echo "(x86_64 недоступен — только Apple Silicon)"; cp "$ARM" "$BIN"
fi
rm -f "$ARM"

APP="$HOME/Applications/CCMonitor.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp Info.plist "$APP/Contents/Info.plist"
cp "$BIN" "$APP/Contents/MacOS/CCMonitor"
cp collector.py "$APP/Contents/Resources/collector.py"   # collector внутри бандла
cp codex-usage-local.py "$APP/Contents/Resources/codex-usage-local.py"
[ -f AppIcon.icns ] && cp AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
rm -f "$BIN"
touch "$APP"

# Подпись своим сертификатом: доступы macOS и уведомления привязаны к нему и переживают пересборку.
# Нет сертификата (чужой Мак) — ad-hoc, как раньше. Настройка: signing-setup.sh.
SIGN_KC="$HOME/Library/Keychains/ccmonitor-signing.keychain-db"
SIGN_PW=$(security find-generic-password -s CCMonitor-signing-keychain -w 2>/dev/null || true)
if [ -f "$SIGN_KC" ] && [ -n "$SIGN_PW" ]; then
  printf 'unlock-keychain -p %s %s\n' "$SIGN_PW" "$SIGN_KC" | security -i >/dev/null
  codesign --force --sign "CCMonitor Self-Signed" --keychain "$SIGN_KC" \
    --identifier com.ccmonitor.app "$APP"
  echo "подписан: CCMonitor Self-Signed"
else
  codesign --force --sign - --identifier com.ccmonitor.app "$APP"
  echo "ВНИМАНИЕ: сертификата нет — ad-hoc подпись (запусти signing-setup.sh)"
fi
unset SIGN_PW

launchctl kickstart -k "gui/$(id -u)/com.ccmonitor.app" 2>/dev/null || \
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.ccmonitor.app.plist" 2>/dev/null || true
launchctl kickstart -k "gui/$(id -u)/com.ccmonitor.app.codex" 2>/dev/null || true

echo "CCMonitor пересобран, установлен в ~/Applications и перезапущен."
