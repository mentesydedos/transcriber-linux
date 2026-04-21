"""
alerts/whatsapp.py — Notificaciones WhatsApp via CallMeBot (gratis).

Activación (una sola vez por usuario):
1. Guarda el número +34 644 60 93 42 como "CallMeBot" en tus contactos WhatsApp
2. Manda el mensaje: "I allow callmebot to send me messages"
3. Recibirás tu API key por WhatsApp
4. Configura número y API key en Ajustes del dashboard
"""
import urllib.request, urllib.parse, logging
logger = logging.getLogger('whatsapp')
WA_URL = "https://api.callmebot.com/whatsapp.php"

def send_whatsapp(phone: str, apikey: str, message: str) -> tuple[bool, str]:
    if not phone or not apikey:
        return False, 'Sin configuración WhatsApp'
    try:
        params = urllib.parse.urlencode({'phone': phone, 'text': message, 'apikey': apikey})
        req = urllib.request.Request(f"{WA_URL}?{params}", headers={'User-Agent':'AlertaTV/1.0'})
        with urllib.request.urlopen(req, timeout=12) as r:
            return True, r.read().decode('utf-8','replace')
    except Exception as e:
        logger.error(f"WhatsApp error: {e}")
        return False, str(e)

def notify_match(phone, apikey, search_name, keyword, channel, timestamp, text):
    msg = f"🔔 *AlertaTV*\nBúsqueda: *{search_name}*\nPalabra: *{keyword}*\nCanal: {channel}\nHora: {str(timestamp)[:19]}\n_{text[:200]}_"
    return send_whatsapp(phone, apikey, msg)
