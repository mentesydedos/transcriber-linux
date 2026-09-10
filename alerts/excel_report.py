"""
alerts/excel_report.py — Construcción del Workbook de Excel para reportes de
búsquedas. Antes vivía inline en la ruta /export de alerts/app.py; se separó
para poder reusar el mismo armado tanto en la descarga manual desde el
navegador como en el reporte semanal automático por correo (ver
_weekly_excel_reports en alerts/watcher.py) sin duplicar la lógica.

build_workbook() es un movimiento LITERAL del cuerpo que antes vivía en
export() (alerts/app.py) -- mismo formato exacto que ya usan los usuarios,
solo parametrizado sobre (search_row, matches_rows) en vez de iterar
search_ids y volver a consultar la DB internamente (esa parte, que sí varía
según quién llama -- filtros del navegador vs. rango fijo semanal -- se
queda en cada caller).
"""
import io
import re
from datetime import datetime, timedelta

import openpyxl
from openpyxl.styles         import Font, PatternFill, Alignment, Border, Side
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text      import InlineFont

from alerts.epg import get_programme_at

WINDOW_SEC = 15


def _bold_kw(text, keyword, phonetic=False, whole_word=False):
    """Devuelve CellRichText con la keyword en negrita.
    Modo exacto: case-insensitive. Modo fonético: resalta cada palabra
    del texto que sea fonéticamente equivalente al keyword. En modo
    whole_word exige límites de palabra (no resalta "día" dentro de
    "diálogo")."""
    if not text or not keyword:
        return text or ''
    from alerts.app import _phonetic  # import diferido -- evita import circular a nivel de módulo
    bold = InlineFont(b=True, color='1D4ED8')
    parts, last = [], 0
    if phonetic:
        ph_kw   = _phonetic(keyword)
        pattern = re.compile(r'(?<!\w)' + re.escape(ph_kw) + r'(?!\w)') if whole_word else None
        for m in re.finditer(r'\S+', text):
            word    = m.group()
            word_ph = _phonetic(word)
            matched = bool(pattern.search(word_ph)) if whole_word else (ph_kw in word_ph)
            if matched:
                if m.start() > last:
                    parts.append(text[last:m.start()])
                parts.append(TextBlock(bold, word))
                last = m.end()
    else:
        kw_pattern = re.escape(keyword)
        if whole_word:
            kw_pattern = r'(?<!\w)' + kw_pattern + r'(?!\w)'
        for m in re.compile(kw_pattern, re.IGNORECASE).finditer(text):
            if m.start() > last:
                parts.append(text[last:m.start()])
            parts.append(TextBlock(bold, m.group()))
            last = m.end()
    if last < len(text):
        parts.append(text[last:])
    return CellRichText(*parts) if parts else text


def build_workbook(sheets_data: list[tuple], d, tdb) -> io.BytesIO:
    """sheets_data: [(search_row, matches_rows), ...] -- una hoja por
    búsqueda. `d` es la conexión a alerts.db (para EPG), `tdb` a
    transcriptions.db (contexto ±15s alrededor de cada coincidencia)."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # Estilos
    h_font   = Font(bold=True, color='FFFFFF', size=11, name='Calibri')
    h_fill   = PatternFill('solid', fgColor='1D4ED8')
    h_align  = Alignment(horizontal='center', vertical='center', wrap_text=True)
    thin     = Side(style='thin', color='CBD5E1')
    border   = Border(left=thin, right=thin, top=thin, bottom=thin)
    alt_fill = PatternFill('solid', fgColor='F1F5F9')

    for s, matches in sheets_data:
        # ── Precarga de contexto: 1 query por canal en vez de 1 por fila ──
        channel_ranges = {}
        for m in matches:
            cid, ts = m['channel_id'], m['timestamp']
            if cid and ts:
                lo, hi = channel_ranges.get(cid, (ts, ts))
                channel_ranges[cid] = (min(lo, ts), max(hi, ts))

        ctx_data = {}   # channel_id -> [(timestamp_str, text), ...]
        for cid, (ts_min, ts_max) in channel_ranges.items():
            trans_rows = tdb.execute("""
                SELECT timestamp, text FROM transcriptions
                WHERE channel_id = ?
                  AND timestamp >= datetime(?, ?)
                  AND timestamp <= datetime(?, ?)
                  AND text IS NOT NULL AND text != '[~]'
                ORDER BY timestamp ASC
            """, (cid,
                  ts_min, f'-{WINDOW_SEC} seconds',
                  ts_max, f'+{WINDOW_SEC} seconds')).fetchall()
            ctx_data[cid] = [(r['timestamp'], r['text']) for r in trans_rows]

        def _get_context(channel_id, timestamp):
            if not channel_id or not timestamp:
                return ''
            entries = ctx_data.get(channel_id, [])
            if not entries:
                return ''
            dt = datetime.strptime(timestamp[:19], '%Y-%m-%d %H:%M:%S')
            lo = (dt - timedelta(seconds=WINDOW_SEC)).strftime('%Y-%m-%d %H:%M:%S')
            hi = (dt + timedelta(seconds=WINDOW_SEC)).strftime('%Y-%m-%d %H:%M:%S')
            return ' '.join(t for ts2, t in entries if lo <= ts2 <= hi)

        sheet_name = re.sub(r'[\\/*?:\[\]]', '', s['name'])[:31] or f"Busqueda_{s['id']}"
        ws = wb.create_sheet(title=sheet_name)

        # ── Encabezado ──
        ws.merge_cells('A1:G1')
        c = ws['A1']
        c.value     = f"Monitoreo ITESO — {s['name']}"
        c.font      = Font(bold=True, size=13, color='1D4ED8', name='Calibri')
        c.alignment = Alignment(horizontal='center', vertical='center')
        ws.row_dimensions[1].height = 22

        ws['A2'] = f"Período: {s['date_start']} → {s['date_end']}"
        ws['C2'] = f"Total coincidencias: {len(matches)}"
        ws['G2'] = f"Exportado: {datetime.now().date().isoformat()}"
        for cell in [ws['A2'], ws['C2'], ws['G2']]:
            cell.font = Font(italic=True, size=9, color='64748B', name='Calibri')
        ws.row_dimensions[2].height = 16
        ws.append([])

        # ── Cabeceras ──
        headers = ['Fecha / Hora Señal', 'Canal', 'Programa (EPG)',
                   'Palabra Detectada', 'Segmento detectado',
                   'Contexto ampliado (±15 seg)', 'Fecha Detección']
        ws.append(headers)
        hrow = ws.max_row
        for col in range(1, len(headers) + 1):
            cell = ws.cell(row=hrow, column=col)
            cell.font      = h_font
            cell.fill      = h_fill
            cell.alignment = h_align
            cell.border    = border
        ws.row_dimensions[hrow].height = 20

        # ── Datos ──
        for i, m in enumerate(matches):
            row_num  = hrow + 1 + i
            contexto = _get_context(m['channel_id'], m['timestamp'])
            programa = get_programme_at(d, m['channel_name'] or '', m['timestamp'] or '')

            ws.cell(row=row_num, column=1, value=str(m['timestamp'] or '')[:19])
            ws.cell(row=row_num, column=2, value=m['channel_name'] or '')
            ws.cell(row=row_num, column=3, value=programa)
            ws.cell(row=row_num, column=4, value=m['keyword'] or '')
            ws.cell(row=row_num, column=5, value=_bold_kw(m['matched_text'] or '', m['keyword'] or '', bool(s['phonetic']), bool(s['whole_word'])))
            ws.cell(row=row_num, column=6, value=_bold_kw(contexto, m['keyword'] or '', bool(s['phonetic']), bool(s['whole_word'])))
            ws.cell(row=row_num, column=7, value=str(m['found_at'] or '')[:19])

            fill = alt_fill if i % 2 == 0 else None
            for col in range(1, len(headers) + 1):
                cell = ws.cell(row=row_num, column=col)
                cell.font      = Font(size=10, name='Calibri')
                cell.border    = border
                cell.alignment = Alignment(vertical='top',
                                           wrap_text=(col in (5, 6)))
                if fill:
                    cell.fill = fill

            # Altura dinámica según longitud del contexto
            ctx_len = len(contexto)
            ws.row_dimensions[row_num].height = (
                80 if ctx_len > 500 else
                50 if ctx_len > 200 else
                30 if ctx_len > 80  else 18
            )

        # ── Anchos ──
        ws.column_dimensions['A'].width = 22
        ws.column_dimensions['B'].width = 18
        ws.column_dimensions['C'].width = 30
        ws.column_dimensions['D'].width = 20
        ws.column_dimensions['E'].width = 50
        ws.column_dimensions['F'].width = 80
        ws.column_dimensions['G'].width = 22

        ws.freeze_panes = ws.cell(row=hrow + 1, column=1)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output
