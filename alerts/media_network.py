"""
alerts/media_network.py — Análisis de "comportamiento de los medios" para una
búsqueda: quién parece originar un tema y quién lo repite después (red de
eco), cómo fluye cada palabra clave entre medios y canales (Sankey), y el
pulso temporal de la cobertura por tipo de medio (stream chart).

Todo se calcula sobre las mismas filas de `matches` que ya usa
search_detail (mismo WHERE filtrado) -- esto NO vuelve a tocar
transcriptions.db, solo reorganiza lo que ya se encontró.
"""
from collections import defaultdict
from datetime import datetime, timedelta

from alerts.channel_types import channel_type

# Ventana para considerar que un canal "hace eco" de otro tras la misma
# palabra clave -- ni tan corta que solo capture cobertura simultánea de una
# noticia de último momento (eso no es "eco", es que ambos lo vieron a la
# vez), ni tan larga que conecte cosas ya no relacionadas. 60 min es un
# punto medio razonable para radio/TV/prensa mexicana (un tema "rebota"
# entre boletines y noticieros en ese rango).
ECHO_WINDOW_MIN = 60
MIN_EDGE_WEIGHT = 1          # con el esquema "primero del día" cada punto ya es significativo -- ver build_echo_network
MAX_NODES       = 80         # tope de canales mostrados en el grafo (por volumen)

MEDIA_COLORS = {
    'tv':      '#3b82f6',  # accent
    'radio':   '#10b981',  # green
    'news':    '#f59e0b',  # amber
    'youtube': '#ef4444',  # red
}
MEDIA_SHAPES = {
    'tv':      'dot',
    'radio':   'square',
    'news':    'triangle',
    'youtube': 'star',
}
MEDIA_LABELS = {'tv': 'TV', 'radio': 'Radio', 'news': 'Prensa/Noticias', 'youtube': 'YouTube'}

# Paleta de comunidades -- distinguible en fondo oscuro y claro, evita rojo/
# verde puros (ya son semánticos en el resto del dashboard: verde=activo,
# rojo=alerta) para no confundir con esos usos.
COMMUNITY_PALETTE = [
    '#60a5fa', '#f472b6', '#facc15', '#34d399', '#c084fc',
    '#fb923c', '#22d3ee', '#a3e635', '#f87171', '#818cf8',
    '#2dd4bf', '#e879f9', '#fbbf24', '#4ade80', '#93c5fd',
]


def _parse_ts(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts[:19])
    except ValueError:
        return None


def build_echo_network(rows) -> dict:
    """rows: filas con channel_id, channel_name, keyword, timestamp.
    Devuelve {nodes: [...], edges: [...], communities: n, leaders: [...],
    followers: [...]} listo para vis-network.

    Una arista A→B cuenta cada DÍA que A tocó una palabra clave antes que B
    (usando solo la PRIMERA mención de cada canal ese día para esa palabra,
    no cada repetición) -- con un tema sostenido durante horas, contar cada
    repetición hacía que cualquier par de canales activos ese día quedara
    conectado decenas de veces (medido: un canal con 12 menciones reales
    acumulaba peso 432), ahogando la señal real de "quién lo dijo primero"
    bajo puro volumen de conversación simultánea. Con un solo punto por
    canal/palabra/día, el peso de una arista es directamente "en cuántos
    días distintos A se adelantó a B" -- comparable entre pares."""
    node_count = defaultdict(int)
    node_kind  = {}
    # first_of_day[(keyword, day)][channel] = primera hora ese día
    first_of_day: dict[tuple, dict[str, datetime]] = defaultdict(dict)
    for r in rows:
        ts = _parse_ts(r['timestamp'])
        if ts is None:
            continue
        ch = r['channel_name'] or '?'
        node_count[ch] += 1
        node_kind.setdefault(ch, channel_type(r['channel_id']))
        key = (r['keyword'], ts.date())
        day_map = first_of_day[key]
        if ch not in day_map or ts < day_map[ch]:
            day_map[ch] = ts

    window = timedelta(minutes=ECHO_WINDOW_MIN)
    edge_weight = defaultdict(int)
    for (kw, day), day_map in first_of_day.items():
        items = sorted(day_map.items(), key=lambda x: x[1])  # (canal, hora) cronológico
        for i, (ch, ts) in enumerate(items):
            for other_ch, other_ts in items[:i]:
                if (ts - other_ts) <= window:
                    edge_weight[(other_ch, ch)] += 1

    # Recorta a los MAX_NODES canales con más coincidencias -- de lo
    # contrario un grafo de 90 canales con nombres largos es ilegible, y los
    # canales de una sola mención suelta no aportan estructura a la red.
    top_channels = {ch for ch, _ in sorted(node_count.items(), key=lambda x: -x[1])[:MAX_NODES]}
    edges_kept = {(a, b): w for (a, b), w in edge_weight.items()
                  if w >= MIN_EDGE_WEIGHT and a in top_channels and b in top_channels}

    import networkx as nx
    G = nx.DiGraph()
    for ch in top_channels:
        G.add_node(ch)
    for (a, b), w in edges_kept.items():
        G.add_edge(a, b, weight=w)

    UG = G.to_undirected()
    if UG.number_of_edges() > 0:
        communities = list(nx.algorithms.community.greedy_modularity_communities(UG, weight='weight'))
    else:
        communities = [{ch} for ch in top_channels]
    community_of = {}
    for i, com in enumerate(communities):
        for node in com:
            community_of[node] = i

    max_count = max(node_count.values(), default=1)
    nodes = []
    for ch in top_channels:
        cid = community_of.get(ch, 0)
        kind = node_kind.get(ch, 'tv')
        nodes.append({
            'id': ch,
            'label': ch,
            'value': node_count[ch],
            # tamaño con raíz cuadrada -- si no, un canal con 10x más
            # menciones se ve 10x más grande y aplasta visualmente al resto.
            'size': round(14 + 26 * (node_count[ch] / max_count) ** 0.5, 1),
            'group': cid,
            'color': COMMUNITY_PALETTE[cid % len(COMMUNITY_PALETTE)],
            'shape': MEDIA_SHAPES.get(kind, 'dot'),
            'kind': kind,
            'kind_label': MEDIA_LABELS.get(kind, kind),
            'title': f'{ch} ({MEDIA_LABELS.get(kind, kind)}) — {node_count[ch]} coincidencia{"s" if node_count[ch] != 1 else ""}',
        })

    max_w = max(edges_kept.values(), default=1)
    edges = [{
        'from': a, 'to': b, 'value': w,
        'width': round(1 + 6 * (w / max_w), 2),
        'title': f'{a} → {b}: {w} eco{"s" if w != 1 else ""} (misma palabra, dentro de {ECHO_WINDOW_MIN} min)',
    } for (a, b), w in edges_kept.items()]

    # "Modos de proceder": quién origina más de lo que repite (líder de
    # narrativa) vs quién repite más de lo que origina (caja de resonancia).
    out_w = defaultdict(int)
    in_w  = defaultdict(int)
    for (a, b), w in edges_kept.items():
        out_w[a] += w
        in_w[b]  += w
    balance = []
    for ch in top_channels:
        o, i = out_w.get(ch, 0), in_w.get(ch, 0)
        if o + i == 0:
            continue
        balance.append({'channel': ch, 'out': o, 'in': i, 'balance': o - i, 'kind_label': MEDIA_LABELS.get(node_kind.get(ch, 'tv'), '')})
    leaders   = sorted(balance, key=lambda x: -x['balance'])[:8]
    followers = sorted(balance, key=lambda x: x['balance'])[:8]

    return {
        'nodes': nodes, 'edges': edges,
        'n_communities': len(communities),
        'leaders': [b for b in leaders if b['balance'] > 0],
        'followers': [b for b in followers if b['balance'] < 0],
        'window_min': ECHO_WINDOW_MIN,
    }


def build_sankey(rows, top_channels_per_medium: int = 6) -> dict:
    """keyword -> medio -> (top N canales de ese medio). Formato listo para
    plotly.js (trace type 'sankey')."""
    kw_medium = defaultdict(int)
    medium_channel = defaultdict(int)
    channel_medium = {}
    for r in rows:
        kw, ch = r['keyword'], (r['channel_name'] or '?')
        kind = channel_type(r['channel_id'])
        kw_medium[(kw, kind)] += 1
        medium_channel[(kind, ch)] += 1
        channel_medium[ch] = kind

    # Top canales por medio -- el resto se agrupa en "Otros (<medio>)" para
    # no saturar el diagrama con decenas de nodos de una sola mención.
    by_medium: dict[str, list] = defaultdict(list)
    for (kind, ch), cnt in medium_channel.items():
        by_medium[kind].append((ch, cnt))
    kept_channels = set()
    other_totals = defaultdict(int)
    for kind, items in by_medium.items():
        items.sort(key=lambda x: -x[1])
        for ch, cnt in items[:top_channels_per_medium]:
            kept_channels.add(ch)
        for ch, cnt in items[top_channels_per_medium:]:
            other_totals[kind] += cnt

    keywords = sorted({kw for kw, _ in kw_medium})
    media    = [m for m in ('tv', 'radio', 'news', 'youtube') if any(k == m for _, k in kw_medium)]
    channels = sorted(kept_channels)

    labels, colors = [], []
    idx = {}
    for kw in keywords:
        idx[('kw', kw)] = len(labels); labels.append(kw); colors.append('#818cf8')
    for m in media:
        idx[('m', m)] = len(labels); labels.append(MEDIA_LABELS.get(m, m)); colors.append(MEDIA_COLORS.get(m, '#94a3b8'))
    for ch in channels:
        idx[('c', ch)] = len(labels); labels.append(ch); colors.append(MEDIA_COLORS.get(channel_medium.get(ch, ''), '#94a3b8'))
    for m in media:
        if other_totals.get(m):
            idx[('o', m)] = len(labels); labels.append(f'Otros ({MEDIA_LABELS.get(m, m)})'); colors.append('#475569')

    src, tgt, val = [], [], []
    for (kw, m), cnt in kw_medium.items():
        if m not in media:
            continue
        src.append(idx[('kw', kw)]); tgt.append(idx[('m', m)]); val.append(cnt)
    for (m, ch), cnt in medium_channel.items():
        key = ('c', ch) if ch in kept_channels else ('o', m)
        if key not in idx:
            continue
        src.append(idx[('m', m)]); tgt.append(idx[key]); val.append(cnt)

    return {'labels': labels, 'colors': colors, 'source': src, 'target': tgt, 'value': val}


def build_stream_timeline(rows) -> dict:
    """Serie temporal por hora, apilada por tipo de medio -- el "pulso" de
    la cobertura: cuándo arranca un tema y qué medio reacciona primero."""
    buckets = defaultdict(lambda: defaultdict(int))  # 'YYYY-MM-DD HH:00' -> medio -> cnt
    for r in rows:
        ts = _parse_ts(r['timestamp'])
        if ts is None:
            continue
        key = ts.strftime('%Y-%m-%d %H:00')
        buckets[key][channel_type(r['channel_id'])] += 1

    hours = sorted(buckets.keys())
    media = ['tv', 'radio', 'news', 'youtube']
    series = {m: [buckets[h].get(m, 0) for h in hours] for m in media}
    return {
        'hours': hours, 'series': series,
        'labels': {m: MEDIA_LABELS[m] for m in media},
        'colors': {m: MEDIA_COLORS[m] for m in media},
    }
