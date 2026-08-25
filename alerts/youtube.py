"""
alerts/youtube.py — Búsqueda de videos de YouTube por palabra clave + rango
de fecha (YouTube Data API v3), descarga del audio (yt-dlp) y transcripción
con el motor CTC español (transcriber_ctc_es.py) -- reutilizado tal cual,
por CPU, a propósito: la GPU de esta máquina ya corre al límite con NVENC +
Parakeet + Cohere en vivo (¡solo ~2GB libres de 12GB medido el 2026-08-24!,
ver el incidente de OOM documentado en video_recorder.py). Cargar un
segundo modelo GPU para esto arriesgaba tronar la transcripción de TV/radio
en vivo -- CTC-ES ya está probado en producción por CPU (12.5x tiempo real,
ver transcriber_ctc_es.py) y además es el motor con mejor desempeño en
español de los tres.

API key: se lee de settings (tabla `settings`, key='youtube_api_key') o de
la variable de entorno YOUTUBE_API_KEY -- se consigue en
https://console.cloud.google.com (habilitar "YouTube Data API v3" y crear
una API key). Cuota gratis: 10,000 unidades/día; search.list cuesta 100 c/u
(~100 búsquedas/día en total).
"""
import os
import json
import subprocess
from datetime import datetime
from pathlib import Path

import requests

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
SAMPLE_RATE = 16000

BASE_DIR = Path(__file__).parent.parent
# El venv del watcher (alerts-watcher.service) no tiene NeMo/torch/onnxruntime
# instalados -- son dependencias pesadas que solo necesita el motor de
# transcripción. venv-parakeet ya las tiene (las usa transcriber_ctc_es.py en
# producción), así que se invoca como subproceso en vez de duplicar ~GB de
# paquetes ML en el venv del watcher. Ver youtube_transcribe_worker.py.
TRANSCRIBE_PYTHON = BASE_DIR / "venv-parakeet" / "bin" / "python3"
TRANSCRIBE_WORKER = BASE_DIR / "youtube_transcribe_worker.py"


def _api_key(adb=None) -> str | None:
    if adb is not None:
        row = adb.execute("SELECT value FROM settings WHERE key='youtube_api_key'").fetchone()
        if row and row["value"]:
            return row["value"]
    return os.environ.get("YOUTUBE_API_KEY")


def search_videos(query: str, date_from: str | None, date_to: str | None,
                   api_key: str, limit: int = 25) -> list[dict]:
    """Busca videos publicados en [date_from, date_to] (YYYY-MM-DD, inclusive)
    que coincidan con `query`. Devuelve [{video_id, title, channel, published, url}]."""
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "order": "date",
        "maxResults": min(limit, 50),
        "relevanceLanguage": "es",
        "key": api_key,
    }
    if date_from:
        params["publishedAfter"] = f"{date_from}T00:00:00Z"
    if date_to:
        # publishedBefore es EXCLUSIVO del instante exacto -- +1 día para
        # que un date_to de hoy no excluya lo publicado hoy mismo (mismo
        # bug que se corrigió para Google Noticias, ver alerts/googlenews.py).
        end = datetime.fromisoformat(date_to)
        params["publishedBefore"] = end.strftime("%Y-%m-%dT23:59:59Z")

    resp = requests.get(YOUTUBE_SEARCH_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    out = []
    for item in data.get("items", []):
        vid = item.get("id", {}).get("videoId")
        if not vid:
            continue
        sn = item.get("snippet", {})
        out.append({
            "video_id": vid,
            "title": sn.get("title", ""),
            "channel": sn.get("channelTitle", ""),
            "published": sn.get("publishedAt", ""),  # ISO 8601 UTC
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return out


def download_audio(video_id: str, out_dir: Path) -> Path | None:
    """Descarga y extrae el audio del video a WAV mono 16kHz -- listo para
    el preprocesador del modelo, sin conversión aparte. None si falla
    (privado, borrado, restringido por región, etc.)."""
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(out_dir / f"{video_id}.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_tmpl,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav",
        }],
        "postprocessor_args": ["-ar", str(SAMPLE_RATE), "-ac", "1"],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    except Exception:
        return None

    wav = out_dir / f"{video_id}.wav"
    return wav if wav.exists() else None


def transcribe_video(path: Path, logger, timeout: int = 600) -> list[tuple[float, str]]:
    """Transcribe el WAV completo en ventanas de 30s (mismo tamaño que usa
    el resto del pipeline) -- devuelve [(offset_seg_inicio, texto), ...],
    saltando ventanas vacías/silencio. Corre en un subproceso bajo
    venv-parakeet (ver youtube_transcribe_worker.py) porque el venv del
    watcher no tiene NeMo/torch/onnxruntime instalados. timeout generoso
    (10 min) -- cargar el modelo ONNX desde cero toma varios segundos y un
    video largo puede tener muchas ventanas de 30s."""
    result = subprocess.run(
        [str(TRANSCRIBE_PYTHON), str(TRANSCRIBE_WORKER), str(path)],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        logger.error(f"[YouTube] worker de transcripción falló ({path.name}): {result.stderr[-2000:]}")
        return []
    segments = json.loads(result.stdout.strip().splitlines()[-1])
    return [(offset, text) for offset, text in segments]
