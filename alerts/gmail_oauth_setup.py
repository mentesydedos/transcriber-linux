#!/usr/bin/env python3
"""
alerts/gmail_oauth_setup.py — Autorización única de Gmail API (OAuth, flujo
"aplicación de escritorio" / loopback). Se usa cuando el envío SMTP directo
no funciona (ej. la red bloquea los puertos 25/465/587) -- ver settings.html,
sección "Gmail API (OAuth)".

Requiere haber guardado antes gmail_client_id / gmail_client_secret en
/settings (panel web), con credenciales tipo "Aplicación de escritorio"
creadas en Google Cloud Console.

Cómo correrlo (el navegador que abre la URL debe ser el TUYO, no el del
servidor -- por eso el túnel SSH):

    ssh -L 8080:localhost:8080 transcriber@148.201.38.17
    cd transcriber-linux
    venv/bin/python3 alerts/gmail_oauth_setup.py

Abre en tu navegador la URL que imprime, inicia sesión con la cuenta de
Gmail que quieres usar para enviar correos, acepta el permiso. El
resultado (refresh token + correo autorizado) se guarda directo en
alerts.db -- no hace falta hacer nada más después de eso.
"""
import http.server
import secrets
import sqlite3
import sys
import urllib.parse
from pathlib import Path

import requests

BASE_DIR  = Path(__file__).parent.parent
ALERTS_DB = BASE_DIR / 'alerts.db'
PORT      = 8080
REDIRECT  = f'http://localhost:{PORT}/'
SCOPES    = 'https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/userinfo.email'


def _get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else ''


def _set_setting(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))


def main():
    conn = sqlite3.connect(str(ALERTS_DB))
    client_id     = _get_setting(conn, 'gmail_client_id')
    client_secret = _get_setting(conn, 'gmail_client_secret')
    if not client_id or not client_secret:
        print("Falta gmail_client_id / gmail_client_secret -- guárdalos primero en /settings.")
        sys.exit(1)

    state = secrets.token_urlsafe(16)
    auth_url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
        'client_id': client_id,
        'redirect_uri': REDIRECT,
        'response_type': 'code',
        'scope': SCOPES,
        'access_type': 'offline',
        'prompt': 'consent',
        'state': state,
    })

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            if params.get('state', [''])[0] != state:
                self.wfile.write(b'<h1>Error: state invalido.</h1> Cierra esta pestana e intenta de nuevo.')
                return
            if 'error' in params:
                self.wfile.write(f"<h1>Error: {params['error'][0]}</h1>".encode())
                result['error'] = params['error'][0]
                return
            result['code'] = params['code'][0]
            self.wfile.write('<h1>Listo, ya puedes cerrar esta pestana.</h1>'.encode('utf-8'))

        def log_message(self, *a):
            pass

    print("\nAbre esta URL en TU navegador (no en el servidor):\n")
    print(auth_url)
    print("\nEsperando autorización...\n")

    httpd = http.server.HTTPServer(('localhost', PORT), Handler)
    while 'code' not in result and 'error' not in result:
        httpd.handle_request()

    if 'error' in result:
        print(f"Autorización cancelada/fallida: {result['error']}")
        sys.exit(1)

    tok = requests.post('https://oauth2.googleapis.com/token', data={
        'code': result['code'],
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': REDIRECT,
        'grant_type': 'authorization_code',
    }, timeout=15).json()

    if 'refresh_token' not in tok:
        print(f"No se recibió refresh_token. Respuesta de Google: {tok}")
        print("Si ya habías autorizado esta app antes, revoca el acceso en "
              "https://myaccount.google.com/permissions y vuelve a intentar "
              "(Google solo manda refresh_token la primera vez que autorizas).")
        sys.exit(1)

    userinfo = requests.get('https://www.googleapis.com/oauth2/v2/userinfo',
                             headers={'Authorization': f"Bearer {tok['access_token']}"},
                             timeout=15).json()
    email = userinfo.get('email', '')

    _set_setting(conn, 'gmail_refresh_token', tok['refresh_token'])
    _set_setting(conn, 'gmail_authorized_email', email)
    conn.commit()
    print(f"\nListo. Autorizado como: {email}")
    print("Ya puedes enviar correos de prueba desde /settings.")


if __name__ == '__main__':
    main()
