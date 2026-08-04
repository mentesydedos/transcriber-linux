"""
alerts/clips.py — Snapshot y clip de video centrados en una coincidencia.

Recorta de las grabaciones de 30 min en output_video/ (ver video_recorder.py).
Cada canal tiene su propia carpeta `canal_NN_Nombre_Canal/` con archivos
`..._YYYY-MM-DD_HH-MM.ts` que empiezan en el momento real en que arrancó ese
segmento (no siempre alineado a :00/:30, porque un reconnect de ffmpeg abre
un archivo nuevo en el momento del reinicio). Por eso la ubicación del
segmento correcto se hace por nombre de archivo + duración real (ffprobe),
no asumiendo bloques fijos de 1800s.
"""
import re
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR  = Path(__file__).parent.parent
VIDEO_DIR = BASE_DIR / 'output_video'
CACHE_DIR = BASE_DIR / 'alerts' / 'cache' / 'clips'
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_FILENAME_RE = re.compile(r'_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})\.ts$')


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def _channel_folder(channel_name: str) -> Path | None:
    safe = _safe_name(channel_name)
    if not VIDEO_DIR.is_dir():
        return None
    for folder in VIDEO_DIR.iterdir():
        if not folder.is_dir():
            continue
        m = re.match(r'canal_\d+_(.+)', folder.name)
        if m and m.group(1) == safe:
            return folder
    return None


def _segment_start(path: Path):
    m = _FILENAME_RE.search(path.name)
    if not m:
        return None
    return datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}", "%Y-%m-%d %H:%M")


def _list_segments(folder: Path) -> list[tuple[datetime, Path]]:
    segs = []
    for p in folder.glob("*.ts"):
        dt = _segment_start(p)
        if dt:
            segs.append((dt, p))
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


def _segment_at(segs: list[tuple[datetime, Path]], moment: datetime):
    """Segmento (start, path, duration) cuyo rango [start, start+dur) cubre `moment`."""
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


def locate_frame(channel_name: str, moment: datetime):
    """Devuelve (path, offset_seconds) del segmento que contiene `moment`."""
    folder = _channel_folder(channel_name)
    if not folder:
        return None
    segs = _list_segments(folder)
    if not segs:
        return None
    found = _segment_at(segs, moment)
    if not found:
        return None
    dt, path, _ = found
    return path, (moment - dt).total_seconds()


def _clip_window(channel_name: str, moment: datetime, before: float, after: float):
    """Lista de (path, offset_inicio, duracion) que concatenados dan el
    clip [moment-before, moment+after]. Salta huecos de grabación."""
    folder = _channel_folder(channel_name)
    if not folder:
        return []
    segs = _list_segments(folder)
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
        length = (piece_end - cur).total_seconds()
        if length <= 0:
            break
        offset = (cur - dt).total_seconds()
        pieces.append((path, max(0.0, offset), length))
        cur = piece_end
    return pieces


def extract_snapshot(channel_name: str, moment: datetime, out_path: Path) -> bool:
    found = locate_frame(channel_name, moment)
    if not found:
        return False
    path, offset = found
    cmd = ["ffmpeg", "-y", "-ss", f"{offset:.2f}", "-i", str(path),
           "-frames:v", "1", "-q:v", "3", str(out_path), "-loglevel", "error"]
    try:
        return subprocess.run(cmd, timeout=15).returncode == 0 and out_path.exists()
    except Exception:
        return False


def extract_clip(channel_name: str, moment: datetime, out_path: Path,
                  before: float = 10.0, after: float = 10.0) -> bool:
    pieces = _clip_window(channel_name, moment, before, after)
    if not pieces:
        return False

    if len(pieces) == 1:
        path, offset, length = pieces[0]
        cmd = ["ffmpeg", "-y", "-ss", f"{offset:.2f}", "-i", str(path),
               "-t", f"{length:.2f}",
               "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
               "-movflags", "+faststart", str(out_path), "-loglevel", "error"]
        try:
            return subprocess.run(cmd, timeout=30).returncode == 0 and out_path.exists()
        except Exception:
            return False

    # El clip cruza un límite entre dos segmentos: recorta cada parte y concatena.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        parts = []
        for i, (path, offset, length) in enumerate(pieces):
            part = tmp / f"part{i}.ts"
            cmd = ["ffmpeg", "-y", "-ss", f"{offset:.2f}", "-i", str(path),
                   "-t", f"{length:.2f}",
                   "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
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
