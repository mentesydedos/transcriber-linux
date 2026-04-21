"""
monitor.py — Monitor en tiempo real de transcripciones
Lee la base de datos SQLite cada 2 segundos y muestra el último texto
de cada canal en pantalla. Uso mínimo de CPU/RAM.

Uso:
  python monitor.py
  python monitor.py --intervalo 3
"""

import sqlite3
import time
import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Forzar UTF-8 para poder mostrar símbolos especiales en Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DB_PATH = "transcriptions.db"

# Colores ANSI
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
WHITE  = "\033[97m"
BG_DARK = "\033[48;5;234m"

CURSOR_HOME  = "\033[H"    # mover cursor al inicio sin borrar
CLEAR_EOL    = "\033[K"    # borrar resto de la línea actual
CLEAR_BELOW  = "\033[J"    # borrar todo lo que queda debajo del cursor
HIDE_CURSOR  = "\033[?25l"
SHOW_CURSOR  = "\033[?25h"

def init_screen():
    """Primera vez: borrar pantalla y ocultar cursor."""
    sys.stdout.write("\033[2J" + HIDE_CURSOR)
    sys.stdout.flush()

def reset_cursor():
    """Volver al inicio sin borrar — evita el parpadeo."""
    sys.stdout.write(CURSOR_HOME)
    sys.stdout.flush()

def get_latest(conn: sqlite3.Connection, n_canales: int = 20) -> list[dict]:
    """Obtiene el último segmento transcrito de cada canal."""
    rows = conn.execute("""
        SELECT
            t.channel_id,
            t.channel_name,
            t.timestamp,
            t.text,
            t.confidence,
            cs.status,
            cs.total_segments
        FROM transcriptions t
        JOIN channel_status cs ON cs.channel_id = t.channel_id
        WHERE t.id IN (
            SELECT MAX(id) FROM transcriptions GROUP BY channel_id
        )
        ORDER BY t.channel_id
        LIMIT ?
    """, (n_canales,)).fetchall()
    return [dict(zip([d[0] for d in conn.execute("SELECT * FROM transcriptions LIMIT 0").description
                      if False] or
                     ["channel_id","channel_name","timestamp","text","confidence","status","total_segments"],
                 row)) for row in rows]

CHUNK_SECONDS   = 30   # debe coincidir con worker.py

def get_latest_v2(conn: sqlite3.Connection, n_canales: int = 20,
                  ventana_seg: int = 30) -> list[dict]:
    """
    Por cada canal activo devuelve los últimos `ventana_seg` segundos de
    transcripción concatenados en un solo bloque de texto, más metadatos.
    """
    conn.row_factory = sqlite3.Row
    cutoff_canal = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_texto = (datetime.now() - timedelta(seconds=ventana_seg)).strftime("%Y-%m-%d %H:%M:%S")

    # Canales activos — filtra por heartbeat (el worker lo actualiza cada 30s
    # independientemente de si hay transcripciones; last_seen solo se actualiza al guardar)
    canales = conn.execute("""
        SELECT channel_id, channel_name, status, heartbeat, error_count, last_error,
               last_seen, total_segments
        FROM channel_status
        WHERE heartbeat >= ?
        ORDER BY channel_id
        LIMIT ?
    """, (cutoff_canal, n_canales)).fetchall()

    resultado = []
    for cs in canales:
        cid = cs["channel_id"]

        # Últimas filas dentro de la ventana de tiempo
        rows = conn.execute("""
            SELECT timestamp, text, confidence
            FROM transcriptions
            WHERE channel_id = ? AND timestamp >= ?
            ORDER BY id ASC
        """, (cid, cutoff_texto)).fetchall()

        textos = [r["text"] for r in rows if r["text"] and r["text"] != "[~]"]
        confs  = [r["confidence"] for r in rows if r["confidence"] is not None
                  and r["text"] and r["text"] != "[~]"]

        # Si no hay texto en la ventana, consultar el último chunk (sea texto o silencio)
        if not textos:
            ultimo_row = conn.execute("""
                SELECT timestamp, text, confidence FROM transcriptions
                WHERE channel_id = ?
                ORDER BY id DESC LIMIT 1
            """, (cid,)).fetchone()
            if ultimo_row:
                if ultimo_row["text"] == "[~]":
                    pass   # solo_silencio se activa abajo
                else:
                    textos = [ultimo_row["text"]]
                    if ultimo_row["confidence"] and ultimo_row["confidence"] > 0:
                        confs = [ultimo_row["confidence"]]

        ultimo    = rows[-1] if rows else None
        ts_ultimo = ultimo["timestamp"] if ultimo else cs["last_seen"]
        solo_silencio = not textos

        # Fluidez: chunks procesados en la ventana de visualización
        chunks_min = conn.execute(
            "SELECT COUNT(*) FROM transcriptions WHERE channel_id=? AND timestamp>=?",
            (cid, cutoff_texto)
        ).fetchone()[0]

        resultado.append({
            "channel_id":     cid,
            "channel_name":   cs["channel_name"],
            "timestamp":      ts_ultimo,
            "text":           "[~]" if solo_silencio else " ".join(textos),
            "confidence":     float(sum(confs)/len(confs)) if confs else 0.0,
            "status":         cs["status"],
            "total_segments": cs["total_segments"],
            "heartbeat":      cs["heartbeat"],
            "error_count":    cs["error_count"],
            "last_error":     cs["last_error"],
            "chunks_min":     chunks_min,
        })

    return resultado

def segundos_desde(ts_str: str) -> int:
    """Segundos transcurridos desde el timestamp dado."""
    try:
        ts = datetime.fromisoformat(ts_str)
        return int((datetime.now() - ts).total_seconds())
    except Exception:
        return 9999

def conf_color(conf: float) -> str:
    if conf is None:
        return DIM
    if conf >= 0.7:
        return GREEN
    if conf >= 0.4:
        return YELLOW
    return RED

def wrap(texto: str, ancho: int, indent: str = "") -> list[str]:
    """Parte el texto en líneas de ancho máximo, respetando palabras."""
    palabras = texto.split()
    lineas, linea = [], ""
    for p in palabras:
        if len(linea) + len(p) + 1 <= ancho:
            linea = (linea + " " + p).lstrip()
        else:
            if linea:
                lineas.append(indent + linea)
            linea = p
    if linea:
        lineas.append(indent + linea)
    return lineas or [indent]

def render(canales: list[dict], intervalo: int, iteracion: int, ventana: int = 10, max_lineas: int = 2):
    ahora = datetime.now().strftime("%H:%M:%S")
    FLUENCY_IDEAL = ventana / CHUNK_SECONDS   # chunks esperados en la ventana
    try:
        cols = os.get_terminal_size().columns
    except Exception:
        cols = 120
    cols = max(cols, 80)

    INDENT  = "     "
    TEXTO_W = cols - len(INDENT) - 2
    HEARTBEAT_LIMIT = 180  # debe coincidir con manager.py

    # Acumular todo en un buffer — un solo write al final elimina el parpadeo
    buf = []

    def ln(texto: str = ""):
        buf.append(texto + CLEAR_EOL + "\n")

    ln(f"{BOLD}{WHITE}{'═' * cols}{RESET}")
    ln(f"{BOLD}{WHITE}  MONITOR  {ahora}  |  ventana {ventana}s  |  refresca {intervalo}s  [Ctrl+C salir]{RESET}")
    ln(f"{WHITE}{'═' * cols}{RESET}")

    for c in canales:
        seg   = segundos_desde(c["timestamp"])
        conf  = c.get("confidence") or 0.0
        texto = (c["text"] or "").replace("\n", " ").strip()

        if seg < 60:
            frescura = f"{GREEN}●{RESET}"
            seg_str  = f"{seg}s"
        elif seg < 300:
            frescura = f"{YELLOW}●{RESET}"
            seg_str  = f"{seg//60}m{seg%60:02d}s"
        else:
            frescura = f"{RED}●{RESET}"
            seg_str  = f"{seg//60}m ago"

        canal_str = (c["channel_name"] or "—")[:22]
        conf_str  = f"{conf:.0%}"
        ts_str    = c["timestamp"][11:19] if c["timestamp"] else "—"

        # Watchdog: edad del heartbeat
        hb_age = segundos_desde(c["heartbeat"]) if c.get("heartbeat") else 9999
        if hb_age <= 60:
            hb_str   = f"{GREEN}{hb_age}s{RESET}"
            hb_label = ""
        elif hb_age <= HEARTBEAT_LIMIT:
            hb_str   = f"{YELLOW}{hb_age}s{RESET}"
            hb_label = ""
        else:
            hb_str   = f"{RED}ZOMBIE {hb_age}s{RESET}"
            hb_label = f" {RED}[SIN HB]{RESET}"

        err_count = c.get("error_count") or 0
        err_str   = f" {RED}err={err_count}{RESET}" if err_count > 0 else ""

        # Fluidez: bar proporcional al ideal — 100% = tiempo real, <80% = atraso
        cpm  = c.get("chunks_min", 0)
        pct  = min(cpm / FLUENCY_IDEAL, 1.0) if FLUENCY_IDEAL > 0 else 0
        BAR  = 8
        fill = round(pct * BAR)
        flu_bar = "▓" * fill + "░" * (BAR - fill)
        if pct >= 0.8:
            flu_color = GREEN
        elif pct >= 0.5:
            flu_color = YELLOW
        else:
            flu_color = RED
        flu_str = f"{flu_color}{flu_bar} {cpm:.0f}/{FLUENCY_IDEAL:.0f}/{ventana}s{RESET}"

        ln()
        ln(f"  {frescura} {BOLD}{canal_str:<22}{RESET}  "
           f"{CYAN}{ts_str}{RESET}  {DIM}({seg_str}){RESET}  "
           f"{conf_color(conf)}{conf_str}{RESET}  "
           f"{DIM}HB:{RESET}{hb_str}{hb_label}  "
           f"{DIM}flu:{RESET}{flu_str}{err_str}")

        if texto == "[~]":
            ln(f"{INDENT}\033[35m♪  silencio / sin voz{RESET}")
        elif texto:
            lineas = wrap(texto, TEXTO_W, indent=INDENT)
            # Mostrar solo las últimas max_lineas (el texto más reciente)
            for l in lineas[-max_lineas:]:
                ln(l)
        else:
            ln(f"{INDENT}{DIM}(esperando...){RESET}")

    ln()
    ln(f"{WHITE}{'─' * cols}{RESET}")
    ln(f"{DIM}  Canales activos: {len(canales)}  |  iteración #{iteracion}{RESET}")

    # Un solo write + clear_below → sin parpadeo
    sys.stdout.write(CURSOR_HOME + "".join(buf) + CLEAR_BELOW)
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description="Monitor en tiempo real de transcripciones")
    parser.add_argument("--intervalo", "-i", type=int, default=1,
                        help="Segundos entre actualizaciones (default: 1)")
    parser.add_argument("--ventana", "-v", type=int, default=35,
                        help="Segundos de transcripción a mostrar por canal (default: 35)")
    parser.add_argument("--lineas", "-l", type=int, default=5,
                        help="Máximo de líneas de texto por canal (default: 5)")
    parser.add_argument("--once", "-1", action="store_true",
                        help="Imprimir una vez y salir (sin refresco, scrollable)")
    args = parser.parse_args()

    if not Path(DB_PATH).exists():
        print(f"No se encontró '{DB_PATH}'. ¿Está corriendo el manager?")
        sys.exit(1)

    if args.once:
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            canales = get_latest_v2(conn, n_canales=20, ventana_seg=args.ventana)
            conn.close()
        except sqlite3.OperationalError as e:
            print(f"Error leyendo DB: {e}")
            sys.exit(1)
        if not canales:
            print("Sin transcripciones aún.")
            sys.exit(0)
        # Imprimir sin control ANSI de cursor/clear — texto plano scrollable
        ahora = datetime.now().strftime("%H:%M:%S")
        try:
            cols = os.get_terminal_size().columns
        except Exception:
            cols = 120
        cols = max(cols, 80)
        print(f"{'═' * cols}")
        print(f"  SNAPSHOT {ahora}  |  ventana {args.ventana}s")
        print(f"{'═' * cols}")
        for c in canales:
            seg   = segundos_desde(c["timestamp"])
            conf  = c.get("confidence") or 0.0
            texto = (c["text"] or "").replace("\n", " ").strip()
            ts_str = c["timestamp"][11:19] if c["timestamp"] else "—"
            seg_str = f"{seg}s" if seg < 60 else (f"{seg//60}m{seg%60:02d}s" if seg < 300 else f"{seg//60}m ago")
            fluency_ideal = args.ventana / CHUNK_SECONDS
            print()
            print(f"  [{c['channel_id']:02d}] {c['channel_name']:<22}  {ts_str}  ({seg_str})  conf={conf:.0%}"
                  f"  flu={c.get('chunks_min',0):.0f}/{fluency_ideal:.0f}/{args.ventana}s")
            INDENT = "     "
            TEXTO_W = cols - len(INDENT) - 2
            if texto == "[~]":
                print(f"{INDENT}♪  silencio / sin voz")
            elif texto:
                for l in wrap(texto, TEXTO_W, indent=INDENT):
                    print(l)
            else:
                print(f"{INDENT}(esperando...)")
        print()
        print(f"{'─' * cols}")
        return

    iteracion = 0
    init_screen()
    print("Conectando a la base de datos...")

    try:
        while True:
            iteracion += 1
            try:
                conn = sqlite3.connect(DB_PATH, timeout=5)
                conn.execute("PRAGMA journal_mode=WAL")
                canales = get_latest_v2(conn, n_canales=20, ventana_seg=args.ventana)
                conn.close()

                if canales:
                    render(canales, args.intervalo, iteracion, args.ventana, args.lineas)
                else:
                    sys.stdout.write(CURSOR_HOME + "Esperando primeras transcripciones..." + CLEAR_BELOW)
                    sys.stdout.flush()

            except sqlite3.OperationalError as e:
                print(f"DB ocupada, reintentando... ({e})")

            time.sleep(args.intervalo)

    except KeyboardInterrupt:
        sys.stdout.write(SHOW_CURSOR + "\n")
        print("Monitor detenido.")


if __name__ == "__main__":
    main()
