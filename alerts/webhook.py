"""
alerts/webhook.py — Envío de notificaciones via webhook HTTP POST.
Úsalo con Zapier, Make.com o n8n para conectar a Instagram u otras redes.
"""
import json
import urllib.request
import logging

logger = logging.getLogger('webhook')


def send_webhook(url: str, payload: dict) -> bool:
    """Hace POST con JSON al URL configurado."""
    if not url:
        return False
    data = json.dumps(payload).encode()
    try:
        req = urllib.request.Request(
            url, data=data, method='POST',
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status < 300
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return False


def notify_match(url: str, search_name: str, keyword: str,
                 channel: str, timestamp: str, text: str) -> bool:
    return send_webhook(url, {
        'event':        'match',
        'search_name':  search_name,
        'keyword':      keyword,
        'channel_name': channel,
        'timestamp':    str(timestamp)[:19],
        'matched_text': text[:300],
    })
