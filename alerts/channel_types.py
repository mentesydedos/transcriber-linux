"""
alerts/channel_types.py — Clasificación de canal por tipo de medio (TV, radio,
Google Noticias, y lo que se agregue después — ej. YouTube la próxima
semana), a partir de su número de canal (mismo id que usa todo el pipeline:
TVHeadend/M3U, worker.py, transcriptions.db, matches). Módulo compartido
entre watcher.py (filtrado por búsqueda) y alerts/app.py + templates
(mostrar el ícono correcto).

Rangos actuales (ver "TV audio.m3u"):
  1-26   TV (TVHeadend)
  27-51  Radio FM (streaming directo, agregado 2026-08-06)
  52+    reservado para YouTube (pendiente, próxima semana)

Google Noticias no tiene "canales" reales -- cada artículo se guarda como un
match con channel_id fijo NEWS_CHANNEL_ID (channel_name lleva el medio real,
ej. "PR Newswire") para poder reusar toda la tabla `matches` sin tocar su
esquema. Se usa un número alto y fijo, fuera de cualquier rango de canales
reales, para que nunca choque cuando se amplíe la numeración de TV/radio/
YouTube.
"""

RADIO_CHANNEL_MIN   = 27
YOUTUBE_CHANNEL_MIN = 52  # ajustar cuando se agreguen los canales reales
NEWS_CHANNEL_ID      = 9001

# Orden = el que se usa en los checkboxes de "nueva búsqueda".
MEDIA_TYPES = [
    ("tv",    "Televisión"),
    ("radio", "Radio"),
    ("news",  "Google Noticias"),
    # ("youtube", "YouTube"),  # descomentar cuando existan canales en ese rango
]
DEFAULT_MEDIA_TYPES = "tv,radio"  # búsquedas existentes sin media_types guardado -- no incluye "news"
                                  # a propósito, para no activar de golpe un fetch externo nuevo en
                                  # búsquedas ya creadas antes de que existiera esta fuente.


def channel_type(channel_id: int) -> str:
    if channel_id is None:
        return "tv"
    if channel_id == NEWS_CHANNEL_ID:
        return "news"
    if channel_id >= YOUTUBE_CHANNEL_MIN:
        return "youtube"
    if channel_id >= RADIO_CHANNEL_MIN:
        return "radio"
    return "tv"


def parse_media_types(raw: str | None) -> set[str]:
    if not raw:
        return set(DEFAULT_MEDIA_TYPES.split(","))
    return {t.strip() for t in raw.split(",") if t.strip()}
