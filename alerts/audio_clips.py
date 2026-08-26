"""
alerts/audio_clips.py — Clip de audio centrado en una coincidencia de radio.

Misma idea que alerts/clips.py para video (localizar el bloque de 30 min
que contiene el momento, local o NAS, recortar una ventana ±N segundos),
pero sin snapshot/frame -- solo el clip de audio. Reusa las rutas/ayudas ya
existentes de alerts/audio_library.py en vez de duplicar la búsqueda de
carpeta local (a diferencia de clips.py, que sí duplica esa lógica para
video porque ahí depende del NOMBRE saneado del canal, no de un id
numérico limpio como aquí).
"""
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from alerts.audio_library import _local_folder, _SEG_RE, NAS_AUDIO_ROOT

BASE_DIR  = Path(__file__).parent.parent
CACHE_DIR = BASE_DIR / 'alerts' / 'cache' / 'audio_clips'
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _segment_start(path: Path):
    m = _SEG_RE.search(path.name)
    if not m:
        return None
    date_str, hh, mm = m.groups()
    return datetime.strptime(f"{date_str} {hh}:{mm}", "%Y-%m-%d %H:%M")


def _nas_segments_for_day(station_num: int, day) -> list[tuple[datetime, Path]]:
    date_dir = NAS_AUDIO_ROOT / day.isoformat()
    segs = []
    try:
        if not date_dir.is_dir():
            return []
        prefix = f"canal_{station_num:02d}_"
        for block_dir in date_dir.iterdir():
            if not block_dir.is_dir():
                continue
            for p in block_dir.glob(f"{prefix}*.aac"):
                dt = _segment_start(p)
                if dt:
                    segs.append((dt, p))
    except OSError:
        return []
    return segs


def _list_segments(station_num: int, around: datetime | None = None) -> list[tuple[datetime, Path]]:
    segs = []
    folder = _local_folder(station_num)
    if folder is not None:
        for p in folder.glob("*.aac"):
            dt = _segment_start(p)
            if dt:
                segs.append((dt, p))
    if around is not None:
        # El bloque local (a lo más 1, el que sigue grabándose) casi
        # siempre ya se borró -- se completa con el NAS del día del
        # momento buscado (± 1 día, por si cae cerca de medianoche).
        seen = {p.name for _, p in segs}
        for delta in (0, -1, 1):
            day = (around + timedelta(days=delta)).date()
            for dt, p in _nas_segments_for_day(station_num, day):
                if p.name not in seen:
                    segs.append((dt, p))
                    seen.add(p.name)
    segs.sort(key=lambda x: x[0])
    return segs


def _ffprobe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def _driftlog_samples(path: Path) -> list[tuple[datetime, float]]:
    """Lee el sidecar .driftlog de un bloque (ver drift_sampler en
    radio_recorder.py): pares (hora real, duración real del archivo en ese
    momento), muestreados cada ~10s mientras se graba. Lista vacía si no
    existe (bloque viejo ya archivado al NAS, o de antes de este cambio)."""
    driftlog = path.with_suffix(".driftlog")
    samples = []
    try:
        for line in driftlog.read_text(encoding="utf-8").splitlines():
            ts_str, dur_str = line.rsplit(",", 1)
            samples.append((datetime.fromisoformat(ts_str), float(dur_str)))
    except (OSError, ValueError):
        return []
    samples.sort(key=lambda s: s[0])
    return samples


def _real_offset(path: Path, dt: datetime, moment: datetime) -> float:
    """Segundo real dentro de `path` que corresponde a `moment` -- usa el
    .driftlog si existe en vez de asumir que "segundo N del archivo" ==
    "segundo N transcurridos desde que inició el bloque". Las reconexiones
    de ffmpeg (-reconnect) no rellenan con silencio el tiempo perdido de la
    fuente, así que un bloque de 30 min puede tener bastante menos audio
    real del que el reloj indica (medido: 41s de menos en un bloque real de
    LOS40 GDL, 2026-08-26) -- sin esto, un clip puede caer en un momento
    completamente distinto al que dice el timestamp. Sin .driftlog, cae al
    cálculo lineal simple de siempre."""
    nominal = (moment - dt).total_seconds()
    samples = _driftlog_samples(path)
    if not samples:
        return nominal
    if moment <= samples[0][0]:
        # Antes de la primera muestra -- casi no puede haber drift
        # acumulado todavía (la primera muestra llega a los pocos segundos
        # de iniciado el bloque).
        return min(nominal, samples[0][1])
    if moment >= samples[-1][0]:
        # Después de la última muestra -- se asume sin drift adicional
        # desde entonces (a lo más falta cubrir DRIFT_SAMPLE_SEC de margen).
        return samples[-1][1] + (moment - samples[-1][0]).total_seconds()
    for (t0, d0), (t1, d1) in zip(samples, samples[1:]):
        if t0 <= moment <= t1:
            span = (t1 - t0).total_seconds()
            if span <= 0:
                return d0
            frac = (moment - t0).total_seconds() / span
            return d0 + (d1 - d0) * frac
    return nominal  # inalcanzable si samples no está vacío, pero por si acaso


def _segment_at(segs: list[tuple[datetime, Path]], moment: datetime):
    candidate = None
    for dt, path in segs:
        if dt <= moment:
            candidate = (dt, path)
        else:
            break
    if not candidate:
        return None
    dt, path = candidate
    dur = _ffprobe_duration(path)
    if dur and (moment - dt).total_seconds() >= dur:
        return None
    return dt, path, dur


def locate_segment(station_num: int, moment: datetime):
    """Devuelve (path, offset_seconds) del bloque de 30 min que contiene
    `moment` -- para reproducir la grabación completa (no solo el clip de
    ±10s) y poder adelantar/atrasar libremente, ver match_full_audio en
    alerts/app.py."""
    segs = _list_segments(station_num, around=moment)
    if not segs:
        return None
    found = _segment_at(segs, moment)
    if not found:
        return None
    dt, path, _ = found
    return path, _real_offset(path, dt, moment)


def _clip_window(station_num: int, moment: datetime, before: float, after: float):
    segs = _list_segments(station_num, around=moment)
    if not segs:
        return []

    win_start = moment - timedelta(seconds=before)
    win_end   = moment + timedelta(seconds=after)

    pieces = []
    cur = win_start
    guard = 0
    while cur < win_end and guard < 4:
        guard += 1
        found = _segment_at(segs, cur)
        if not found:
            nxt = next((dt for dt, _ in segs if dt > cur), None)
            if not nxt or nxt >= win_end:
                break
            cur = nxt
            continue
        dt, path, dur = found
        seg_end = dt + timedelta(seconds=dur) if dur else win_end
        piece_end = min(win_end, seg_end)
        if piece_end <= cur:
            break
        offset     = max(0.0, _real_offset(path, dt, cur))
        end_offset = max(offset, _real_offset(path, dt, piece_end))
        length = end_offset - offset
        if length <= 0:
            break
        pieces.append((path, offset, length))
        cur = piece_end
    return pieces


def extract_clip(station_num: int, moment: datetime, out_path: Path,
                  before: float = 10.0, after: float = 10.0) -> bool:
    pieces = _clip_window(station_num, moment, before, after)
    if not pieces:
        return False

    if len(pieces) == 1:
        path, offset, length = pieces[0]
        cmd = ["ffmpeg", "-y", "-ss", f"{offset:.2f}", "-i", str(path),
               "-t", f"{length:.2f}", "-c:a", "aac", "-b:a", "96k",
               "-movflags", "+faststart", str(out_path), "-loglevel", "error"]
        try:
            return subprocess.run(cmd, timeout=30).returncode == 0 and out_path.exists()
        except Exception:
            return False

    # El clip cruza un límite entre dos bloques: recorta cada parte y concatena.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        parts = []
        for i, (path, offset, length) in enumerate(pieces):
            part = tmp / f"part{i}.m4a"
            cmd = ["ffmpeg", "-y", "-ss", f"{offset:.2f}", "-i", str(path),
                   "-t", f"{length:.2f}", "-c:a", "aac", "-b:a", "96k",
                   str(part), "-loglevel", "error"]
            try:
                if subprocess.run(cmd, timeout=30).returncode != 0:
                    return False
            except Exception:
                return False
            parts.append(part)

        list_file = tmp / "concat.txt"
        list_file.write_text("".join(f"file '{p}'\n" for p in parts))
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
               "-c", "copy", "-movflags", "+faststart", str(out_path), "-loglevel", "error"]
        try:
            return subprocess.run(cmd, timeout=30).returncode == 0 and out_path.exists()
        except Exception:
            return False
