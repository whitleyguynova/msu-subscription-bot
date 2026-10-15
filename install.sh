#!/usr/bin/env bash
# Установка бота на чистый сервер Ubuntu 22.04/24.04. Запускать от root из папки с ботом:
#   bash install.sh
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DST=/opt/subbot

echo "→ Ставлю Python и зависимости системы"
apt-get -o DPkg::Lock::Timeout=300 update -qq
apt-get -o DPkg::Lock::Timeout=300 install -y -qq python3 python3-venv sqlite3 >/dev/null

echo "→ Копирую бота в $DST"
mkdir -p "$DST" "$DST/backups"
cp -r "$SRC"/*.py "$SRC"/handlers "$SRC"/requirements.txt "$DST"/
[ -f "$DST/.env" ] || cp "$SRC/.env" "$DST/.env"   # при обновлении .env на сервере не трогаем
chmod 600 "$DST/.env"

echo "→ Ставлю библиотеки"
python3 -m venv "$DST/venv"
"$DST/venv/bin/pip" install -q --upgrade pip
"$DST/venv/bin/pip" install -q -r "$DST/requirements.txt"

echo "→ Настраиваю автозапуск"
cat > /etc/systemd/system/subbot.service <<UNIT
[Unit]
Description=Telegram subscription bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DST
ExecStart=$DST/venv/bin/python main.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now subbot
systemctl restart subbot

echo "→ Ежедневная копия базы в $DST/backups (храню 14 дней)"
cat > /etc/cron.daily/subbot-backup <<CRON
#!/bin/sh
[ -f $DST/bot.db ] && sqlite3 $DST/bot.db ".backup '$DST/backups/bot-\$(date +%F).db'"
find $DST/backups -name 'bot-*.db' -mtime +14 -delete
CRON
chmod +x /etc/cron.daily/subbot-backup

sleep 5
echo
systemctl --no-pager --lines=15 status subbot || true
echo
echo "Готово. Логи: journalctl -u subbot -f   Перезапуск: systemctl restart subbot"
