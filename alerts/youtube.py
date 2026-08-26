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
import re
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
SAMPLE_RATE = 16000

# Tope para guardar la transcripción COMPLETA del video (no solo el
# fragmento donde cayó la palabra clave) -- a pedido explícito, para no
# guardar transcripciones enteras de streams/podcasts de horas por cada
# video que toque una palabra clave una sola vez. Con captions nativos la
# duración casi no importa en costo (no hay descarga de audio ni CPU de
# transcripción); el límite real es para acotar el respaldo por
# transcripción local (CTC-ES por CPU, ~12.5x tiempo real medido en
# producción -- 30 min de video son ~2-3 min de CPU, razonable).
FULL_TRANSCRIPT_MAX_SEC = 30 * 60

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


_SRT_TS_RE = re.compile(r'(\d\d):(\d\d):(\d\d),(\d\d\d)\s*-->\s*(\d\d):(\d\d):(\d\d),(\d\d\d)')


def _video_meta(video_id: str) -> dict | None:
    """Metadata sin descargar nada (duración, disponibilidad de captions) --
    para decidir el flujo antes de bajar audio o subtítulos."""
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL({'skip_download': True, 'quiet': True, 'no_warnings': True}) as ydl:
            return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except Exception:
        return None


def download_captions_srt(video_id: str, out_dir: Path, lang: str = 'es') -> Path | None:
    """Baja la transcripción NATIVA de YouTube (subida por el creador o
    generada automáticamente) en formato SRT, sin descargar audio ni video
    -- None si el idioma pedido no está disponible para este video."""
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(out_dir / f"{video_id}.%(ext)s")
    ydl_opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [lang],
        "subtitlesformat": "srt",
        "outtmpl": out_tmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    except Exception:
        return None

    srt = out_dir / f"{video_id}.{lang}.srt"
    return srt if srt.exists() else None


def parse_srt(path: Path) -> list[tuple[float, str]]:
    """SRT -> [(segundo_inicio, texto), ...]. A diferencia del VTT "rolling"
    que usa YouTube en pantalla, el SRT que exporta yt-dlp ya viene con un
    cue por frase/fragmento sin texto duplicado entre cues consecutivos --
    concatenar en orden reconstruye el texto continuo tal cual."""
    segments = []
    text = path.read_text(encoding='utf-8', errors='replace')
    for block in text.split('\n\n'):
        lines = [l for l in block.splitlines() if l.strip()]
        ts_idx = next((i for i, l in enumerate(lines) if '-->' in l), None)
        if ts_idx is None:
            continue
        m = _SRT_TS_RE.search(lines[ts_idx])
        if not m:
            continue
        h, mi, se, ms = (int(g) for g in m.groups()[:4])
        start = h * 3600 + mi * 60 + se + ms / 1000
        content = ' '.join(l.strip() for l in lines[ts_idx + 1:])
        content = re.sub(r'<[^>]+>', '', content).strip()  # tags de estilo/posición ocasionales
        if content:
            segments.append((start, content))
    return segments


def _rebin_segments(segments: list[tuple[float, str]], bucket_sec: float = 30.0) -> list[tuple[float, str]]:
    """Agrupa cues finos de captions (unos segundos cada uno) en ventanas de
    ~30s -- misma granularidad que ya produce transcribe_video(), para que
    el resto del pipeline (empate de keywords, contexto mostrado) no note
    diferencia según el origen de la transcripción."""
    if not segments:
        return []
    buckets: dict[int, list[str]] = defaultdict(list)
    for ts, txt in segments:
        buckets[int(ts // bucket_sec)].append(txt)
    return [(b * bucket_sec, ' '.join(buckets[b])) for b in sorted(buckets)]


ALLOWED_LANGS = {'es', 'en'}


def detect_language(text: str) -> str | None:
    """Idioma dominante del texto ('es', 'en', etc.) -- None si no se pudo
    determinar (texto muy corto/ambiguo). Usa langdetect (heurística
    estadística de n-gramas, sin modelos pesados ni GPU) -- la metadata de
    YouTube (info['language']) viene vacía en muchos videos, no sirve como
    filtro confiable por sí sola."""
    from langdetect import detect, LangDetectException
    try:
        return detect(text)
    except LangDetectException:
        return None


def get_transcript(video_id: str, out_dir: Path, logger) -> dict | None:
    """Transcripción de un video: intenta primero los captions nativos de
    YouTube (rápido, sin CPU); si no hay disponibles en español, cae al
    método anterior (descarga de audio + transcripción local por CTC-ES).
    None si no se pudo obtener transcripción de ninguna forma (video
    privado/borrado/restringido, o sin audio hablado detectable).

    Devuelve {segments, raw_segments, source, duration}:
      - segments: [(offset, texto)] en ventanas de ~30s, para el empate de
        keywords existente (misma forma que siempre esperó ese código).
      - raw_segments: la transcripción tal cual llegó (cues finos de
        captions, o ya en 30s si vino de ASR) -- para guardar el
        texto completo con mejor granularidad cuando aplica.
      - source: 'captions' o 'asr'.
      - duration: segundos del video, o None si no se pudo determinar."""
    meta = _video_meta(video_id)
    duration = meta.get('duration') if meta else None
    has_captions = bool(meta and (
        (meta.get('subtitles') or {}).get('es') or (meta.get('automatic_captions') or {}).get('es')
    ))

    if has_captions:
        srt = download_captions_srt(video_id, out_dir, 'es')
        if srt:
            raw = parse_srt(srt)
            srt.unlink(missing_ok=True)
            if raw:
                return {'segments': _rebin_segments(raw), 'raw_segments': raw,
                        'source': 'captions', 'duration': duration}

    wav = download_audio(video_id, out_dir)
    if wav is None:
        return None
    try:
        segs = transcribe_video(wav, logger)
    finally:
        wav.unlink(missing_ok=True)
    if not segs:
        return None
    return {'segments': segs, 'raw_segments': segs, 'source': 'asr', 'duration': duration}


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
