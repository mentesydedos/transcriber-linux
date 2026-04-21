"""
alerts/mailer.py — Envío de correos para alertas y reportes.
"""
import smtplib, ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from datetime             import datetime


# ── Estilos del correo ────────────────────────────────────────────────────────
_STYLE = """
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#0d1117;color:#e6edf3;margin:0;padding:24px}
  .wrap{max-width:680px;margin:0 auto}
  .hdr{background:#161b22;border:1px solid #30363d;border-radius:10px;
       padding:20px 24px;margin-bottom:20px}
  .hdr h1{margin:0 0 4px;font-size:18px;color:#58a6ff}
  .hdr p{margin:0;color:#8b949e;font-size:13px}
  .match{background:#161b22;border:1px solid #30363d;border-left:3px solid #3fb950;
         border-radius:8px;padding:14px 18px;margin-bottom:12px}
  .mh{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
  .kw{background:#1f6feb33;color:#58a6ff;border:1px solid #1f6feb55;
      border-radius:4px;padding:2px 9px;font-size:12px;font-weight:700;font-family:monospace}
  .ch{color:#3fb950;font-weight:600;font-size:14px}
  .ts{color:#8b949e;font-size:12px}
  .txt{font-size:13px;color:#c9d1d9;line-height:1.6}
  .hl{color:#f0883e;font-weight:700}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{background:#21262d;color:#8b949e;padding:8px 12px;text-align:left;
     font-size:11px;text-transform:uppercase;letter-spacing:.05em}
  td{padding:8px 12px;border-bottom:1px solid #21262d;vertical-align:top}
  .ftr{text-align:center;color:#8b949e;font-size:11px;margin-top:20px}
</style>"""


def _build_smtp(cfg: dict):
    host = cfg.get('smtp_host', '')
    port = int(cfg.get('smtp_port', 587))
    user = cfg.get('smtp_user', '')
    pwd  = cfg.get('smtp_pass', '')
    tls  = cfg.get('smtp_tls', '1') == '1'
    if tls:
        ctx = ssl.create_default_context()
        srv = smtplib.SMTP(host, port, timeout=15)
        srv.ehlo(); srv.starttls(context=ctx); srv.ehlo()
    else:
        srv = smtplib.SMTP_SSL(host, port, timeout=15)
    if user:
        srv.login(user, pwd)
    return srv


def _send(cfg: dict, to: str, subject: str, html: str):
    sender = cfg.get('smtp_from') or cfg.get('smtp_user', '')
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = sender
    msg['To']      = to
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    srv = _build_smtp(cfg)
    srv.sendmail(sender, to, msg.as_string())
    srv.quit()


def _match_block(m: dict) -> str:
    kw   = m.get('keyword', '')
    text = m.get('matched_text', '')
    text_hl = text.replace(kw, f'<span class="hl">{kw}</span>', 1)
    ts   = str(m.get('timestamp', ''))[:19]
    ch   = m.get('channel_name', '—')
    return f"""
    <div class="match">
      <div class="mh">
        <span><span class="kw">{kw}</span>&nbsp;&nbsp;<span class="ch">{ch}</span></span>
        <span class="ts">{ts}</span>
      </div>
      <div class="txt">{text_hl}</div>
    </div>"""


def send_immediate(match: dict, cfg: dict) -> tuple[bool, str]:
    to = match.get('report_email', '')
    if not to:
        return False, 'Sin correo destino'
    kw  = match.get('keyword', '')
    ch  = match.get('channel_name', '—')
    sname = match.get('search_name', '')
    subject = f"\U0001f514 Alerta: «{kw}» detectado en {ch}"
    html = f"""<!DOCTYPE html><html><head>{_STYLE}</head><body><div class="wrap">
    <div class="hdr">
      <h1>Alerta detectada</h1>
      <p>Búsqueda: <strong>{sname}</strong></p>
    </div>
    {_match_block(match)}
    <div class="ftr">AlertaTV · {datetime.now().strftime('%d/%m/%Y %H:%M')}</div>
    </div></body></html>"""
    try:
        _send(cfg, to, subject, html)
        return True, f'Alerta enviada a {to}'
    except Exception as e:
        return False, str(e)


def send_report(search, matches: list[dict], cfg: dict, mode: str = 'manual') -> tuple[bool, str]:
    to   = search['report_email'] if isinstance(search, dict) else search.report_email
    name = search['name']         if isinstance(search, dict) else search.name
    if not to:
        return False, 'Sin correo destino configurado'
    titles = {'daily': 'Reporte Diario', 'final': 'Reporte Final', 'manual': 'Reporte Manual'}
    title  = titles.get(mode, 'Reporte')
    subject = f"\U0001f4ca {title}: {name} — {len(matches)} coincidencias"
    rows = ''.join(f"""<tr>
      <td><span class="kw">{m.get('keyword','')}</span></td>
      <td style="color:#3fb950">{m.get('channel_name','—')}</td>
      <td style="color:#8b949e">{str(m.get('timestamp',''))[:19]}</td>
      <td style="color:#c9d1d9">{str(m.get('matched_text',''))[:140]}</td>
    </tr>""" for m in matches[:1000])
    html = f"""<!DOCTYPE html><html><head>{_STYLE}</head><body><div class="wrap">
    <div class="hdr">
      <h1>{title}: {name}</h1>
      <p>Total: <strong>{len(matches)}</strong> coincidencias · {datetime.now().strftime('%d/%m/%Y %H:%M')}</p>
    </div>
    <table><tr><th>Palabra</th><th>Canal</th><th>Fecha/Hora</th><th>Texto</th></tr>
    {rows}
    </table>
    <div class="ftr">AlertaTV — Sistema de monitoreo TV</div>
    </div></body></html>"""
    try:
        _send(cfg, to, subject, html)
        return True, f'{title} enviado a {to}'
    except Exception as e:
        return False, str(e)


def send_daily_report(search, matches, cfg):
    send_report(search, matches, cfg, 'daily')

def send_final_report(search, matches, cfg):
    send_report(search, matches, cfg, 'final')
