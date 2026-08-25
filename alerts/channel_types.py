"""
alerts/channel_types.py — Clasificación de canal por tipo de medio (TV, radio,
Google Noticias, y lo que se agregue después — ej. YouTube la próxima
semana), a partir de su número de canal (mismo id que usa todo el pipeline:
TVHeadend/M3U, worker.py, transcriptions.db, matches). Módulo compartido
entre watcher.py (filtrado por búsqueda) y alerts/app.py + templates
(mostrar el ícono correcto).

Rangos actuales (ver "TV audio.m3u"):
  1-26   TV (TVHeadend)
  27+    Radio FM (streaming directo, agregado 2026-08-06 -- hoy llega hasta
         el canal 64 con las 38 estaciones configuradas, así que YA NO hay
         un tope fijo de rango libre arriba de radio para YouTube; ver nota
         abajo sobre por qué YouTube usa un id fijo en vez de un rango)

Ni Google Noticias ni YouTube (búsqueda por palabra clave) tienen "canales"
reales -- cada resultado se guarda como un match con un channel_id FIJO
(NEWS_CHANNEL_ID / YOUTUBE_CHANNEL_ID; channel_name lleva el medio real, ej.
"PR Newswire" o el título del video) para poder reusar toda la tabla
`matches` sin tocar su esquema. Se usan números altos y fijos, fuera de
cualquier rango de canales reales (que YouTube no está pensado por-canal),
para que nunca choquen aunque crezca la numeración de radio.
"""

RADIO_CHANNEL_MIN  = 27
NEWS_CHANNEL_ID    = 9001
YOUTUBE_CHANNEL_ID = 9002

# Orden = el que se usa en los checkboxes de "nueva búsqueda".
MEDIA_TYPES = [
    ("tv",      "Televisión"),
    ("radio",   "Radio"),
    ("news",    "Google Noticias"),
    ("youtube", "YouTube"),
]
DEFAULT_MEDIA_TYPES = "tv,radio"  # búsquedas existentes sin media_types guardado -- no incluye "news"
                                  # ni "youtube" a propósito, para no activar de golpe un fetch externo
                                  # nuevo en búsquedas ya creadas antes de que existiera esa fuente.


def channel_type(channel_id: int) -> str:
    if channel_id is None:
        return "tv"
    if channel_id == NEWS_CHANNEL_ID:
        return "news"
    if channel_id == YOUTUBE_CHANNEL_ID:
        return "youtube"
    if channel_id >= RADIO_CHANNEL_MIN:
        return "radio"
    return "tv"


def parse_media_types(raw: str | None) -> set[str]:
    if not raw:
        return set(DEFAULT_MEDIA_TYPES.split(","))
    return {t.strip() for t in raw.split(",") if t.strip()}
