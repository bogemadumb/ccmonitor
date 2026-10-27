#!/bin/bash
# Самоподписанная подпись CCMonitor в отдельной связке ключей.
# Зачем: macOS привязывает доступы (TCC) и уведомления к сертификату, а не к хэшу бинарника —
# разрешения переживают пересборку. Повторный запуск ничего не меняет.
set -euo pipefail

IDENTITY="CCMonitor Self-Signed"
KC="$HOME/Library/Keychains/ccmonitor-signing.keychain-db"
PW_SERVICE="CCMonitor-signing-keychain"

kc_password() {
    security find-generic-password -s "$PW_SERVICE" -w 2>/dev/null
}

if [ -f "$KC" ] && security find-identity -p codesigning "$KC" 2>/dev/null | grep -q "$IDENTITY"; then
    echo "уже настроено: $IDENTITY в $KC"
    exit 0
fi

TMP=$(mktemp -d)
chmod 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT

PW=$(kc_password || true)
if [ -z "$PW" ]; then
    PW=$(openssl rand -hex 24)
    printf 'add-generic-password -U -s %s -a ccmonitor -w %s\n' "$PW_SERVICE" "$PW" | security -i >/dev/null
fi
printf '%s' "$PW" > "$TMP/pw"

if [ ! -f "$KC" ]; then
    printf 'create-keychain -p %s %s\n' "$PW" "$KC" | security -i >/dev/null
fi
printf 'unlock-keychain -p %s %s\n' "$PW" "$KC" | security -i >/dev/null
security set-keychain-settings "$KC"

cat > "$TMP/cert.cnf" <<'EOF'
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = CCMonitor Self-Signed
[ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
EOF
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -config "$TMP/cert.cnf" \
    -keyout "$TMP/key.pem" -out "$TMP/cert.pem" 2>/dev/null
openssl pkcs12 -export -inkey "$TMP/key.pem" -in "$TMP/cert.pem" -name "$IDENTITY" \
    -out "$TMP/id.p12" -passout "file:$TMP/pw"

printf 'import %s -k %s -f pkcs12 -P %s -T /usr/bin/codesign\n' "$TMP/id.p12" "$KC" "$PW" | security -i >/dev/null
printf 'set-key-partition-list -S apple-tool:,apple:,codesign: -s -k %s %s\n' "$PW" "$KC" | security -i >/dev/null

CURRENT=$(security list-keychains -d user | sed 's/^ *"//; s/"$//')
if ! printf '%s\n' "$CURRENT" | grep -qx "$KC"; then
    # shellcheck disable=SC2086
    security list-keychains -d user -s $CURRENT "$KC"
fi

cp "$TMP/cert.pem" "$HOME/.claude/usage-monitor/signing-cert.pem"
echo "создано: $IDENTITY в $KC"
security find-identity -p codesigning "$KC"
