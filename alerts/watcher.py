"""
alerts/watcher.py — Hilo de fondo que monitorea transcripciones y dispara alertas.
Corre cada POLL_INTERVAL segundos. Lee transcriptions.db, cruza contra búsquedas
activas, guarda coincidencias y dispara correos según el modo de entrega.
"""
import json
import re
import sqlite3
import threading
import time
import unicodedata
import logging
from datetime import datetime, date
from pathlib  import Path

from alerts.mailer   import send_immediate, send_daily_report, send_final_report
from alerts.telegram import notify_match as tg_notify_match
from alerts.epg      import refresh_if_needed as epg_refresh, ensure_schema as epg_schema

logger = logging.getLogger('watcher')

BASE_DIR      = Path(__file__).parent.parent
ALERTS_DB     = BASE_DIR / 'alerts.db'
TRANS_DB      = BASE_DIR / 'transcriptions.db'
POLL_INTERVAL = 5   # segundos entre cada ciclo


# ── Normalización fonética española ──────────────────────────────────────────
def _strip_accents(text: str) -> str:
    text = unicodedata.normalize('NFD', text.lower())
    return ''.join(c for c in text if unicodedata.category(c) != 'Mn')

def _phonetic_es(text: str) -> str:
    """Normalización fonética básica del español."""
    t = _strip_accents(text)
    t = re.sub(r'\bh', '', t)          # h inicial (muda)
    t = t.replace('v', 'b')            # b/v
    t = t.replace('ll', 'y')           # ll → y
    t = re.sub(r'qu([ei])', r'k\1', t) # que/qui → ke/ki
    t = re.sub(r'c([ei])', r's\1', t)  # ce/ci → se/si
    t = re.sub(r'g([ei])', r'j\1', t)  # ge/gi → je/ji
    t = t.replace('z', 's')            # z → s
    t = t.replace('ck', 'k')           # ck → k
    t = re.sub(r'x', 'ks', t)          # x → ks
    return t

def _match(text: str, keyword: str, phonetic: bool, whole_word: bool = False) -> bool:
    """whole_word exige que la keyword aparezca delimitada por separadores de
    palabra (no dentro de una palabra compuesta, ej. "día" no debe casar con
    "diálogo" ni "mediodía"). Se comprueba con límites \\w sobre el mismo
    texto normalizado en ambos lados (keyword y texto), así que es seguro
    aunque la normalización fonética cambie longitudes de palabra."""
    norm_text = _phonetic_es(text)    if phonetic else _strip_accents(text)
    norm_kw   = _phonetic_es(keyword) if phonetic else _strip_accents(keyword)
    if not norm_kw:
        return False
    if whole_word:
        return re.search(r'(?<!\w)' + re.escape(norm_kw) + r'(?!\w)', norm_text) is not None
    return norm_kw in norm_text


# ── Conexiones ────────────────────────────────────────────────────────────────
def _adb():
    c = sqlite3.connect(str(ALERTS_DB), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def _tdb():
    c = sqlite3.connect(str(TRANS_DB), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


# ── Helpers de estado ─────────────────────────────────────────────────────────
def _get_setting(adb, key: str, default='0') -> str:
    r = adb.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r['value'] if r else default

def _set_setting(adb, key: str, value: str):
    adb.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))

def _smtp_cfg(adb) -> dict | None:
    rows = adb.execute("SELECT key, value FROM settings").fetchall()
    cfg  = {r['key']: r['value'] for r in rows}
    return cfg if cfg.get('smtp_host') else None

def _full_cfg(adb) -> dict:
    rows = adb.execute("SELECT key, value FROM settings").fetchall()
    return {r['key']: r['value'] for r in rows}


# ── Ciclo principal ───────────────────────────────────────────────────────────
def _process(adb, tdb, smtp, cfg=None):
    today = date.today().isoformat()
    now   = datetime.now()

    # 1. Inicializar búsquedas nuevas (histórico desde date_start)
    new_searches = adb.execute(
        "SELECT * FROM searches WHERE initialized=0 AND status='active'"
    ).fetchall()
    for s in new_searches:
        keywords   = json.loads(s['keywords'])
        phonetic   = bool(s['phonetic'])
        whole_word = bool(s['whole_word'])
        BATCH   = 2000

        # Contar total de registros en el rango para mostrar progreso
        total_count = tdb.execute("""
            SELECT COUNT(*) FROM transcriptions
            WHERE timestamp >= ? AND timestamp <= ?
        """, (s['date_start'] + ' 00:00:00', s['date_end'] + ' 23:59:59')).fetchone()[0]
        adb.execute(
            "UPDATE searches SET init_rows_total=?, init_rows_done=0 WHERE id=?",
            (total_count, s['id'])
        )
        adb.commit()

        last_hist_id = 0
        total_hist   = 0
        while True:
            hist = tdb.execute("""
                SELECT id, channel_id, channel_name, timestamp, text
                FROM transcriptions
                WHERE id > ?
                  AND timestamp >= ? AND timestamp <= ?
                ORDER BY id ASC
                LIMIT ?
            """, (last_hist_id,
                  s['date_start'] + ' 00:00:00',
                  s['date_end']   + ' 23:59:59',
                  BATCH)).fetchall()
            if not hist:
                break
            for row in hist:
                text = row['text'] or ''
                if not text or text == '[~]':
                    continue
                for kw in keywords:
                    if _match(text, kw, phonetic, whole_word):
                        adb.execute("""INSERT OR IGNORE INTO matches
                            (search_id, keyword, channel_id, channel_name, timestamp, matched_text)
                            VALUES (?,?,?,?,?,?)""",
                            (s['id'], kw, row['channel_id'], row['channel_name'],
                             row['timestamp'], text[:500]))
            last_hist_id  = hist[-1]['id']
            total_hist   += len(hist)
            adb.execute(
                "UPDATE searches SET init_rows_done=? WHERE id=?",
                (total_hist, s['id'])
            )
            adb.commit()
            if len(hist) < BATCH:
                break
        adb.execute("UPDATE searches SET initialized=1 WHERE id=?", (s['id'],))
        adb.commit()
        logger.info(f"Búsqueda {s['id']} '{s['name']}' inicializada: {total_hist} registros históricos revisados.")

    # 2. Procesar nuevas transcripciones (delta desde último ID)
    last_id = int(_get_setting(adb, 'watcher_last_id', '0'))
    rows = tdb.execute("""
        SELECT id, channel_id, channel_name, timestamp, text
        FROM transcriptions WHERE id > ?
        ORDER BY id ASC LIMIT 500
    """, (last_id,)).fetchall()

    if not rows:
        return

    active = adb.execute("""
        SELECT s.*, u.tg_chat_id as u_tg_chat_id
        FROM searches s JOIN users u ON s.user_id = u.id
        WHERE s.status='active' AND s.initialized=1
        AND s.date_start <= ? AND s.date_end >= ?
    """, (today, today)).fetchall()

    immediate_email   = []   # correos inmediatos (solo modo 'immediate')
    immediate_telegram = []  # alertas Telegram (independiente del modo de correo)

    tg_token       = (cfg or {}).get('tg_token', '')
    tg_chat_global = (cfg or {}).get('tg_chat_id', '')

    if active:
        for row in rows:
            text = row['text'] or ''
            if not text or text == '[~]':
                continue
            for s in active:
                keywords   = json.loads(s['keywords'])
                phonetic   = bool(s['phonetic'])
                whole_word = bool(s['whole_word'])
                for kw in keywords:
                    if _match(text, kw, phonetic, whole_word):
                        adb.execute("""INSERT OR IGNORE INTO matches
                            (search_id, keyword, channel_id, channel_name, timestamp, matched_text)
                            VALUES (?,?,?,?,?,?)""",
                            (s['id'], kw, row['channel_id'], row['channel_name'],
                             row['timestamp'], text[:500]))

                        base = {
                            'search_id':   s['id'],
                            'search_name': s['name'],
                            'keyword':     kw,
                            'channel_id':  row['channel_id'],
                            'channel_name':row['channel_name'],
                            'timestamp':   row['timestamp'],
                            'matched_text':text[:500],
                        }

                        # Email: solo en modo inmediato con correo configurado
                        if s['delivery_mode'] == 'immediate' and s['report_email']:
                            immediate_email.append({**base, 'report_email': s['report_email']})

                        # Telegram: siempre que esté activado, sin importar modo de correo
                        notify_tg = s['notify_telegram'] if 'notify_telegram' in s.keys() else 0
                        if notify_tg:
                            if not tg_token:
                                logger.warning(f"[TG] Búsqueda {s['id']}: notify_telegram=1 pero sin tg_token en ajustes.")
                            else:
                                chat_id = (s['u_tg_chat_id'] if 'u_tg_chat_id' in s.keys() else '') or tg_chat_global
                                if not chat_id:
                                    logger.warning(f"[TG] Búsqueda {s['id']}: sin chat_id (ni en perfil de usuario ni en ajustes globales).")
                                else:
                                    immediate_telegram.append({**base, 'chat_id': chat_id})
        adb.commit()

    # 3. Correos inmediatos
    if smtp and immediate_email:
        for m in immediate_email:
            try:
                send_immediate(m, smtp)
            except Exception as e:
                logger.error(f"Email inmediato error: {e}")

    # 4. Alertas Telegram (independientes del modo de entrega de correo)
    for m in immediate_telegram:
        try:
            ok = tg_notify_match(
                tg_token, m['chat_id'],
                m['search_name'], m['keyword'],
                m['channel_name'], m['timestamp'],
                m['matched_text'],
            )
            if ok:
                logger.info(f"[TG] Alerta enviada: búsqueda '{m['search_name']}' · kw '{m['keyword']}'")
            else:
                logger.error(f"[TG] Fallo al enviar alerta: búsqueda '{m['search_name']}' · chat_id '{m['chat_id']}'")
        except Exception as e:
            logger.error(f"[TG] Excepción: {e}")


    _set_setting(adb, 'watcher_last_id', str(rows[-1]['id']))
    adb.commit()


def _daily_reports(adb, smtp):
    if not smtp:
        return
    if datetime.now().hour < 7:
        return
    today = date.today().isoformat()
    searches = adb.execute("""
        SELECT * FROM searches
        WHERE delivery_mode='daily' AND report_email IS NOT NULL AND report_email!=''
        AND status='active'
        AND (last_daily_report IS NULL OR last_daily_report < ?)
    """, (today,)).fetchall()
    for s in searches:
        matches = adb.execute("""
            SELECT * FROM matches WHERE search_id=?
            AND date(found_at,'localtime') = date('now','localtime')
            ORDER BY found_at
        """, (s['id'],)).fetchall()
        try:
            send_daily_report(s, [dict(m) for m in matches], smtp)
            adb.execute("UPDATE searches SET last_daily_report=? WHERE id=?", (today, s['id']))
            adb.commit()
        except Exception as e:
            logger.error(f"Reporte diario error search {s['id']}: {e}")


def _close_expired(adb):
    """Marca como 'completed' cualquier búsqueda cuyo date_end ya pasó, sin importar el modo."""
    today = date.today().isoformat()
    rows = adb.execute("""
        SELECT id, name FROM searches
        WHERE status='active' AND date_end < ?
    """, (today,)).fetchall()
    for s in rows:
        adb.execute("UPDATE searches SET status='completed' WHERE id=?", (s['id'],))
        logger.info(f"Búsqueda {s['id']} '{s['name']}' marcada como completada (date_end expirado).")
    if rows:
        adb.commit()


def _final_reports(adb, smtp):
    if not smtp:
        return
    today = date.today().isoformat()
    searches = adb.execute("""
        SELECT * FROM searches
        WHERE delivery_mode='final' AND report_email IS NOT NULL AND report_email!=''
        AND status IN ('active','completed') AND date_end < ?
        AND (last_daily_report IS NULL OR last_daily_report < date_end)
    """, (today,)).fetchall()
    for s in searches:
        matches = adb.execute(
            "SELECT * FROM matches WHERE search_id=? ORDER BY found_at", (s['id'],)
        ).fetchall()
        try:
            send_final_report(s, [dict(m) for m in matches], smtp)
            adb.execute("UPDATE searches SET status='completed', last_daily_report=? WHERE id=?",
                        (today, s['id']))
            adb.commit()
        except Exception as e:
            logger.error(f"Reporte final error search {s['id']}: {e}")


def _loop():
    logger.info("Watcher iniciado.")
    while True:
        try:
            adb  = _adb()
            tdb  = _tdb()
            cfg  = _full_cfg(adb)
            smtp = cfg if cfg.get('smtp_host') else None
            epg_schema(adb)
            epg_refresh(adb)
            _close_expired(adb)
            _process(adb, tdb, smtp, cfg)
            _daily_reports(adb, smtp)
            _final_reports(adb, smtp)
            adb.close()
            tdb.close()
        except Exception as e:
            logger.exception(f"Error en watcher: {e}")
        time.sleep(POLL_INTERVAL)


def start_watcher():
    t = threading.Thread(target=_loop, daemon=True, name='alertas-watcher')
    t.start()
    return t
