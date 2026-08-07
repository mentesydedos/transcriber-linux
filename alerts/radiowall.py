"""
alerts/radiowall.py — Monitor de radio FM: lista de estaciones y proxy de
streaming en vivo para el dashboard AlertaTV.

Reutiliza el mismo M3U que usa la transcripción (`TV audio.m3u`), leyendo solo
las entradas de radio (posición >= RADIO_CHANNEL_MIN). El proxy existe porque
algunas estaciones (las que van detrás de Zeno.fm) exigen un header
Origin/Referer del sitio dueño — un <audio src="..."> directo del navegador
manda el Origin del propio dashboard y esas estaciones lo rechazan con 401.
Sirviendo el audio desde aquí, el request saliente lleva el header correcto
(ver #EXTHEADER en el M3U) y el navegador solo ve un stream normal.
"""
import re
import requests
from pathlib import Path
from typing import Iterator, Optional

BASE_DIR          = Path(__file__).parent.parent
M3U_FILE          = BASE_DIR / "TV audio.m3u"
RADIO_CHANNEL_MIN = 27


def _parse_m3u(filepath: Path) -> list[dict]:
    """Mismo parser que manager.py (incluye #EXTHEADER) — copiado en vez de
    importado para no acoplar el dashboard Flask al pipeline de transcripción
    (manager.py trae dependencias pesadas, torch/nemo, que no hacen falta aquí)."""
    channels, current_name, current_headers = [], None, None
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line == "#EXTM3U":
                continue
            if line.startswith("#EXTINF"):
                m = re.search(r',(.+)$', line)
                current_name = m.group(1).strip() if m else f"Canal_{len(channels)+1}"
            elif line.startswith("#EXTHEADER:"):
                current_headers = line[len("#EXTHEADER:"):].strip()
            elif not line.startswith("#"):
                channels.append({"name": current_name or f"Canal_{len(channels)+1}",
                                 "url": line, "headers": current_headers})
                current_name, current_headers = None, None
    return channels


def list_radio_stations() -> list[dict]:
    """Estaciones de radio (posición >= RADIO_CHANNEL_MIN en el M3U), con su
    número de canal (mismo id que usa la transcripción, útil para cruzar con
    transcriptions.db si hiciera falta más adelante)."""
    channels = _parse_m3u(M3U_FILE)
    out = []
    for i, ch in enumerate(channels, start=1):
        if i >= RADIO_CHANNEL_MIN:
            out.append({"num": i, "name": ch["name"], "url": ch["url"], "headers": ch["headers"]})
    return out


def _station_by_num(num: int) -> Optional[dict]:
    return next((s for s in list_radio_stations() if s["num"] == num), None)


def stream_proxy(num: int) -> tuple[Optional[Iterator[bytes]], Optional[str]]:
    """Generador de bytes crudos del stream en vivo + el Content-Type real de
    la fuente (para que el navegador sepa si es mp3/aac). None, None si el
    canal no existe o la conexión falla."""
    station = _station_by_num(num)
    if station is None:
        return None, None

    req_headers = {}
    if station["headers"]:
        key, _, val = station["headers"].partition(":")
        req_headers[key.strip()] = val.strip()

    try:
        upstream = requests.get(station["url"], headers=req_headers, stream=True, timeout=10)
        upstream.raise_for_status()
    except Exception:
        return None, None

    content_type = upstream.headers.get("Content-Type", "audio/mpeg")

    def _gen():
        try:
            for chunk in upstream.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return _gen(), content_type
