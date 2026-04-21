"""
alerts/telegram.py — Notificaciones por Telegram vía Bot API.
"""
import html
import json
import urllib.request
import urllib.parse
import urllib.error
import logging

logger = logging.getLogger('telegram')

def _e(s) -> str:
    """Escapa caracteres HTML para que Telegram no los rechace."""
    return html.escape(str(s or ''))


def send_telegram(token: str, chat_id: str, message: str) -> tuple[bool, str]:
    """Envía un mensaje a Telegram. Devuelve (éxito, mensaje_error)."""
    if not token or not chat_id:
        return False, 'Token o Chat ID no configurados.'
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        'chat_id':    chat_id,
        'text':       message,
        'parse_mode': 'HTML',
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=10) as r:
            return True, ''
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='ignore')
        try:
            detail = json.loads(body).get('description', body)
        except Exception:
            detail = body[:200]
        logger.error(f"Telegram HTTP {e.code}: {detail}")
        return False, f'Error {e.code}: {detail}'
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False, str(e)


def notify_match(token: str, chat_id: str,
                 search_name: str, keyword: str,
                 channel: str, timestamp: str, text: str) -> bool:
    msg = (
        f"<b>Monitoreo ITESO</b>\n"
        f"Búsqueda: <b>{_e(search_name)}</b>\n"
        f"Palabra detectada: <b>{_e(keyword)}</b>\n"
        f"Canal: {_e(channel)}\n"
        f"Hora señal: {_e(str(timestamp)[:19])}\n"
        f"Texto: <i>{_e(text[:300])}</i>"
    )
    ok, _ = send_telegram(token, chat_id, msg)
    return ok


def send_report(token: str, chat_id: str, search: dict, matches: list) -> tuple[bool, str]:
    """Envía reporte a Telegram.
    Mensaje 1: resumen estadístico (siempre).
    Mensajes siguientes: bloques de coincidencias de ~3800 chars c/u."""
    total = len(matches)

    # ── Resumen estadístico ──────────────────────────────────────────
    from collections import Counter
    kw_cnt = Counter(m.get('keyword', '') for m in matches)
    ch_cnt = Counter(m.get('channel_name', '') for m in matches)

    summary_lines = [
        f"<b>Reporte — {_e(search['name'])}</b>",
        f"Período: {_e(search['date_start'])} → {_e(search['date_end'])}",
        f"Total coincidencias: <b>{total}</b>",
        "",
        "<b>Por palabra clave:</b>",
    ]
    for kw, cnt in kw_cnt.most_common():
        summary_lines.append(f"  • <b>{_e(kw)}</b>: {cnt}")
    summary_lines += ["", "<b>Top canales:</b>"]
    for ch, cnt in ch_cnt.most_common(5):
        summary_lines.append(f"  • {_e(ch)}: {cnt}")

    ok, err = send_telegram(token, chat_id, '\n'.join(summary_lines))
    if not ok:
        return False, err

    # ── Bloques de coincidencias (~3800 chars por mensaje) ───────────
    BLOCK = 3800
    block_lines, block_len = [], 0
    for m in matches:
        ts  = str(m.get('timestamp') or '')[:16]
        ch  = _e(m.get('channel_name') or '')
        kw  = _e(m.get('keyword') or '')
        txt = _e((m.get('matched_text') or '')[:100])
        line = f"• {ts} · {ch} · <b>{kw}</b>: <i>{txt}</i>"
        if block_len + len(line) + 1 > BLOCK:
            ok, err = send_telegram(token, chat_id, '\n'.join(block_lines))
            if not ok:
                return False, err
            block_lines, block_len = [], 0
        block_lines.append(line)
        block_len += len(line) + 1
    if block_lines:
        ok, err = send_telegram(token, chat_id, '\n'.join(block_lines))

    return ok, err


def test_connection(token: str, chat_id: str) -> tuple[bool, str]:
    """Envía un mensaje de prueba para verificar la configuración."""
    return send_telegram(token, chat_id,
                         '<b>Monitoreo ITESO</b>\nConexión con Telegram configurada correctamente.')
