#!/bin/bash
set -e
ROOT=/home/transcriber/transcriber-linux

echo "== copiando archivos =="
cp "$ROOT/systemd/radio-recorder.service"      /etc/systemd/system/radio-recorder.service
cp "$ROOT/systemd/backup-nas2-radio.service"   /etc/systemd/system/backup-nas2-radio.service
cp "$ROOT/systemd/backup-nas2-radio.timer"     /etc/systemd/system/backup-nas2-radio.timer
cp "$ROOT/systemd/cleanup-nas-radio.service"   /etc/systemd/system/cleanup-nas-radio.service
cp "$ROOT/systemd/cleanup-nas-radio.timer"     /etc/systemd/system/cleanup-nas-radio.timer

echo "== aplicando =="
systemctl daemon-reload
systemctl enable --now radio-recorder.service
systemctl enable --now backup-nas2-radio.timer
systemctl enable --now cleanup-nas-radio.timer

echo "== esperando 15s para que arranquen las grabaciones =="
sleep 15

echo "== estado =="
systemctl is-active radio-recorder.service backup-nas2-radio.timer cleanup-nas-radio.timer

echo "--- estaciones grabando ahora mismo ---"
ls /home/transcriber/transcriber-linux/output_radio/ 2>/dev/null | wc -l
find /home/transcriber/transcriber-linux/output_radio -name "*.aac" 2>/dev/null | wc -l

echo "--- últimas líneas del log ---"
tail -n 15 /home/transcriber/transcriber-linux/logs/radio_recorder.log 2>/dev/null

echo "listo. El primer respaldo al NAS y el primer borrado local pasan en el siguiente minuto (backup-nas2-radio.timer)."
