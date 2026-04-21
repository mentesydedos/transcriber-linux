"""
search.py — Búsqueda full-text en las transcripciones almacenadas
Usa SQLite FTS5 para búsqueda rápida por texto, canal y rango de fechas.

Uso:
  python search.py "palabra clave"
  python search.py "frase exacta" --canal "Canal 5"
  python search.py "término" --desde "2024-01-15 08:00" --hasta "2024-01-15 10:00"
  python search.py "término" --limite 50
"""

import sqlite3
import argparse
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = "transcriptions.db"

def search(
    query:    str,
    canal:    str  = None,
    desde:    str  = None,
    hasta:    str  = None,
    limite:   int  = 20,
    contexto: bool = False
) -> list[dict]:
    """
    Busca en las transcripciones usando FTS5.
    Retorna lista de resultados ordenados por relevancia y fecha.
    """
    if not Path(DB_PATH).exists():
        print(f"Error: No se encontró la base de datos '{DB_PATH}'")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Construcción dinámica del WHERE
    conditions = ["fts.text MATCH ?"]
    params     = [query]

    if canal:
        conditions.append("t.channel_name LIKE ?")
        params.append(f"%{canal}%")
    if desde:
        conditions.append("t.timestamp >= ?")
        params.append(desde)
    if hasta:
        conditions.append("t.timestamp <= ?")
        params.append(hasta)

    where_clause = " AND ".join(conditions)
    params.append(limite)

    sql = f"""
        SELECT
            t.id,
            t.channel_id,
            t.channel_name,
            t.timestamp,
            t.text,
            t.confidence,
            t.duration_sec,
            rank
        FROM transcriptions_fts fts
        JOIN transcriptions t ON fts.rowid = t.id
        WHERE {where_clause}
        ORDER BY rank, t.unix_ts DESC
        LIMIT ?
    """

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        print(f"Error en búsqueda: {e}")
        print("Tip: Si el error menciona FTS5, la tabla puede estar vacía todavía.")
        return []
    finally:
        conn.close()

    return [dict(row) for row in rows]


def show_status():
    """Muestra estado actual de todos los canales."""
    if not Path(DB_PATH).exists():
        print("Base de datos no encontrada. ¿El sistema está corriendo?")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            cs.channel_id,
            cs.channel_name,
            cs.status,
            cs.total_segments,
            cs.error_count,
            cs.last_seen,
            COUNT(t.id) as segments_hoy
        FROM channel_status cs
        LEFT JOIN transcriptions t
            ON t.channel_id = cs.channel_id
            AND t.timestamp >= date('now')
        GROUP BY cs.channel_id
        ORDER BY cs.channel_id
    """).fetchall()
    conn.close()

    print("\n" + "═" * 78)
    print(f"  {'ID':>3}  {'Canal':<28}  {'Estado':<12}  {'Segmentos':<10}  {'Último'}")
    print("═" * 78)
    for r in rows:
        last = r["last_seen"][:19] if r["last_seen"] else "—"
        print(f"  {r['channel_id']:>3}  {(r['channel_name'] or '—')[:28]:<28}  "
              f"{(r['status'] or '—'):<12}  {r['total_segments']:<10}  {last}")
    print("═" * 78 + "\n")


def format_result(r: dict, highlight: str = None) -> str:
    """Formatea un resultado para mostrar en terminal."""
    text = r["text"]
    # Resaltar la palabra buscada (simple, sin regex)
    if highlight:
        for word in highlight.split():
            text = text.replace(word, f"\033[1;33m{word}\033[0m")  # Amarillo bold
            text = text.replace(word.lower(), f"\033[1;33m{word.lower()}\033[0m")
            text = text.replace(word.capitalize(), f"\033[1;33m{word.capitalize()}\033[0m")

    conf_str = f"{r['confidence']:.0%}" if r.get("confidence") is not None else "—"
    return (
        f"  \033[36m[{r['timestamp']}]\033[0m "
        f"\033[1m{r['channel_name']}\033[0m "
        f"\033[2m(conf: {conf_str})\033[0m\n"
        f"  {text}\n"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Búsqueda en transcripciones de canales de TV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python search.py "presidente"
  python search.py "economía inflación" --canal "Milenio"
  python search.py "terremoto" --desde "2024-03-01 00:00" --limite 100
  python search.py --status
        """
    )
    parser.add_argument("query",          nargs="?",  help="Texto a buscar")
    parser.add_argument("--canal",  "-c", type=str,   help="Filtrar por nombre de canal")
    parser.add_argument("--desde",  "-d", type=str,   help="Fecha/hora inicio (YYYY-MM-DD HH:MM)")
    parser.add_argument("--hasta",        type=str,   help="Fecha/hora fin   (YYYY-MM-DD HH:MM)")
    parser.add_argument("--limite", "-l", type=int,   default=20, help="Máximo de resultados (default: 20)")
    parser.add_argument("--status", "-s", action="store_true", help="Mostrar estado de los canales")

    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if not args.query:
        parser.print_help()
        sys.exit(1)

    print(f"\n\033[1mBuscando:\033[0m '{args.query}'" +
          (f"  canal: {args.canal}" if args.canal else "") +
          (f"  desde: {args.desde}" if args.desde else "") +
          (f"  hasta: {args.hasta}" if args.hasta else "") + "\n")

    results = search(
        query  = args.query,
        canal  = args.canal,
        desde  = args.desde,
        hasta  = args.hasta,
        limite = args.limite
    )

    if not results:
        print("  Sin resultados.\n")
        return

    print(f"  {len(results)} resultado(s) encontrado(s):\n")
    print("─" * 70)
    for r in results:
        print(format_result(r, highlight=args.query))
    print("─" * 70)
    print(f"\n  Mostrando {len(results)} de máximo {args.limite}. "
          f"Usa --limite N para ver más.\n")


if __name__ == "__main__":
    main()
