#!/bin/bash
# check_disk_alert.sh — Notifica por escritorio si el filesystem del proyecto
# supera el umbral de espacio usado. Pensado para correr vía systemd --user timer
# (necesita la sesión gráfica del usuario para notify-send).
set -euo pipefail

FS="/home/transcriber/transcriber-linux"
THRESHOLD_GB=800
STATE_FILE="/home/transcriber/.cache/transcriber-disk-alert-last"
RENOTIFY_SEC=3600   # no repetir la notificación más de 1 vez por hora mientras siga sobre el umbral

used_gb=$(df --output=used -BG "$FS" | tail -1 | tr -dc '0-9')

if [ "$used_gb" -le "$THRESHOLD_GB" ]; then
    rm -f "$STATE_FILE"
    exit 0
fi

now=$(date +%s)
last=0
[ -f "$STATE_FILE" ] && last=$(cat "$STATE_FILE")

if [ $(( now - last )) -ge "$RENOTIFY_SEC" ]; then
    export DISPLAY=:1
    export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
    notify-send -u critical "Transcriptor TV — disco al límite" \
        "Uso de disco: ${used_gb} GB (umbral ${THRESHOLD_GB} GB). Revisa output/ y transcriptions.db."
    echo "$now" > "$STATE_FILE"
fi
