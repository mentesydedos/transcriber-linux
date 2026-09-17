#!/usr/bin/env python3
"""
media_review_job.py — Detecta medios/páginas de Facebook/Instagram nuevos
sin país identificado (alerts/media_countries.py) y los encola en
pending_media_review para revisión manual -- NO intenta adivinar el país
solo, ni por WHOIS ni por geolocalización de IP: son señales poco
confiables para esto (el registrante de un dominio o dónde está hosteado
no siempre coincide con el país real del medio, ver alerts/
media_countries.py:PAGE_NAME_COUNTRY -- "Exitosa Noticias" resultó ser de
Perú aunque apareciera en una búsqueda de política mexicana, justo el
tipo de error que se evita revisando a mano en vez de adivinar).

Solo automatiza la parte segura: detectar qué es nuevo y avisar por
Telegram cuando se acumula suficiente para justificar una revisión --
igual que se ha venido haciendo a mano en esta conversación, un lote a la
vez, con conocimiento real de cada medio antes de agregarlo a la tabla
curada.

Corre una vez por noche vía media-review.timer, en la ventana de baja
actividad (00:30-05:00) -- no por urgencia, sino porque no tiene sentido
competir por recursos con el watcher/transcripción en vivo por una tarea
que no es sensible al tiempo.

Uso: python3 media_review_job.py
"""
import sqlite3
from pathlib import Path

from alerts.channel_types import NEWS_CHANNEL_ID, GDELT_CHANNEL_ID, GDELT_MX_CHANNEL_ID
from alerts.media_countries import country_for, SOCIAL_DOMAINS, PAGE_NAME_COUNTRY, _extract_page_name
from alerts.telegram import send_telegram

BASE_DIR  = Path(__file__).parent
ALERTS_DB = BASE_DIR / "alerts.db"
EXTERNAL_CHANNEL_IDS = (NEWS_CHANNEL_ID, GDELT_CHANNEL_ID, GDELT_MX_CHANNEL_ID)

# No avisar por 1-2 medios sueltos -- esperar a que se acumule un lote que
# valga la pena revisar de una sentada (mismo criterio que ya se usó a
# mano en esta conversación).
NOTIFY_THRESHOLD = 3


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS pending_media_review (
        kind        TEXT NOT NULL,   -- 'domain' o 'social_page'
        value       TEXT NOT NULL,
        mentions    INTEGER DEFAULT 1,
        first_seen  TEXT DEFAULT (datetime('now','localtime')),
        notified    INTEGER DEFAULT 0,
        PRIMARY KEY (kind, value)
    )""")
    conn.commit()


def find_unclassified(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """Dominios y nombres de página de Facebook/Instagram que hoy no
    resuelven a ningún país -- ver alerts/media_countries.py:country_for."""
    domains, social = {}, {}
    placeholders = ','.join('?' * len(EXTERNAL_CHANNEL_IDS))
    rows = conn.execute(f"""SELECT channel_domain, matched_text FROM matches
        WHERE channel_id IN ({placeholders}) AND channel_domain IS NOT NULL
          AND channel_country IS NULL""", EXTERNAL_CHANNEL_IDS).fetchall()
    for domain, text in rows:
        d = (domain or '').strip().lower()
        if d.startswith('www.'):
            d = d[4:]
        if not d:
            continue
        if d in SOCIAL_DOMAINS:
            name = _extract_page_name(text)
            if name and name not in PAGE_NAME_COUNTRY:
                social[name] = social.get(name, 0) + 1
            continue
        if country_for(domain):
            continue  # se resolvió por otro camino (subdominio, TLD, etc.)
        domains[d] = domains.get(d, 0) + 1
    return domains, social


def sync_queue(conn: sqlite3.Connection, domains: dict, social: dict) -> list[tuple[str, str, int]]:
    """Inserta lo nuevo, actualiza el conteo de lo que ya estaba encolado,
    y limpia lo que mientras tanto ya se resolvió (curado en el código
    entre una corrida y otra). Devuelve lo recién agregado esta corrida."""
    new_rows = []
    for kind, items in (('domain', domains), ('social_page', social)):
        for value, mentions in items.items():
            cur = conn.execute(
                "INSERT OR IGNORE INTO pending_media_review (kind, value, mentions) VALUES (?,?,?)",
                (kind, value, mentions))
            if cur.rowcount:
                new_rows.append((kind, value, mentions))
            else:
                conn.execute(
                    "UPDATE pending_media_review SET mentions=? WHERE kind=? AND value=?",
                    (mentions, kind, value))

    # Limpieza: lo que ya se resolvió desde que se encoló (alguien agregó
    # el dominio/página a la tabla curada en el código y se redesplegó).
    for row in conn.execute("SELECT kind, value FROM pending_media_review").fetchall():
        resolved = (country_for(row['value']) if row['kind'] == 'domain'
                    else row['value'] in PAGE_NAME_COUNTRY)
        if resolved:
            conn.execute("DELETE FROM pending_media_review WHERE kind=? AND value=?",
                         (row['kind'], row['value']))
    conn.commit()
    return new_rows


def notify_if_enough(conn: sqlite3.Connection) -> None:
    pending = conn.execute(
        "SELECT kind, value, mentions FROM pending_media_review WHERE notified=0 ORDER BY mentions DESC"
    ).fetchall()
    if len(pending) < NOTIFY_THRESHOLD:
        return  # se sigue acumulando en silencio hasta que valga la pena avisar

    cfg = {r['key']: r['value'] for r in conn.execute("SELECT key, value FROM settings")}
    token, chat_id = cfg.get('tg_token', ''), cfg.get('tg_chat_id', '')
    if not token or not chat_id:
        return  # sin Telegram configurado, se queda solo encolado para revisar en /admin o a mano

    lines = [f"📋 <b>{len(pending)} medios nuevos sin país identificado</b>", ""]
    for r in pending[:15]:
        kind_label = 'página de Facebook/Instagram' if r['kind'] == 'social_page' else 'dominio'
        lines.append(f"• {r['value']} ({kind_label}, {r['mentions']} mención{'es' if r['mentions'] != 1 else ''})")
    if len(pending) > 15:
        lines.append(f"… y {len(pending) - 15} más.")
    lines.append("")
    lines.append("Pídele a Claude que los revise para agregarlos a alerts/media_countries.py.")

    ok, _ = send_telegram(token, chat_id, '\n'.join(lines))
    if ok:
        conn.execute("UPDATE pending_media_review SET notified=1 WHERE notified=0")
        conn.commit()


def main():
    conn = sqlite3.connect(str(ALERTS_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    domains, social = find_unclassified(conn)
    new_rows = sync_queue(conn, domains, social)
    print(f"{len(new_rows)} medios nuevos encolados para revisión "
          f"({len(domains)} dominios, {len(social)} páginas de Facebook/Instagram detectados hoy).")

    notify_if_enough(conn)
    conn.close()


if __name__ == '__main__':
    main()
