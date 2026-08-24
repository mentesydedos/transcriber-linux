#!/bin/bash
set -e
ROOT=/home/transcriber/transcriber-linux

echo "== copiando archivos =="
cp "$ROOT/systemd/alerts.service"        /etc/systemd/system/alerts.service
cp "$ROOT/systemd/alerts-stream.service" /etc/systemd/system/alerts-stream.service
cp "$ROOT/nginx/alertatv"                /etc/nginx/sites-enabled/alertatv

echo "== probando config de nginx =="
nginx -t

echo "== aplicando =="
systemctl daemon-reload
systemctl enable --now alerts-stream.service
systemctl restart alerts.service
systemctl reload nginx

echo "== estado =="
systemctl is-active alerts.service alerts-stream.service nginx
echo "--- alerts.service (8001) ---"
grep -o -- '--workers [0-9]* --threads [0-9]* --bind [^ ]* --timeout [0-9]*' /etc/systemd/system/alerts.service
echo "--- alerts-stream.service (8002) ---"
grep -o -- '--workers [0-9]* --threads [0-9]* --bind [^ ]* --timeout [0-9]*' /etc/systemd/system/alerts-stream.service

echo "== prueba rápida =="
curl -s -o /dev/null -w "dashboard (8001 via nginx): %{http_code}\n" http://127.0.0.1:5001/login
curl -s -o /dev/null -w "streaming directo 8002:     %{http_code}\n" http://127.0.0.1:8002/login

echo "listo."
