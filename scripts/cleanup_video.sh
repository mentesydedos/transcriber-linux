#!/bin/bash
# cleanup_video.sh — Borra bloques de video más viejos que RETENTION_DAYS.
# Pensado como red de seguridad mientras la grabación vive en disco local
# (796 GB libres); una vez montado el NAS de 50TB, subir RETENTION_DAYS o
# desactivar este timer.
set -euo pipefail

VIDEO_DIR="/home/transcriber/transcriber-linux/output_video"
RETENTION_DAYS="${TRANSCRIBER_VIDEO_RETENTION_DAYS:-2}"

find "$VIDEO_DIR" -type f -name "*.ts" -mtime "+${RETENTION_DAYS}" -print -delete
