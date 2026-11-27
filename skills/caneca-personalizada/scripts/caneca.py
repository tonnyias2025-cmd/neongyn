#!/usr/bin/env python3
"""Peças personalizadas para canecas (padrão Print4You) a partir de logo vetorial.

Subcomandos:
  analisar <modelo.3mf> --saida DIR   mede objetos/placas do 3MF, renderiza cada um, sugere referências
  logo     <logo.svg|pdf|png> --saida DIR   separa o logo em elementos numerados (mapa + tabela)
  gerar    <pedido.json>              gera STLs (contorno + arte por cor), checagens, prévia, mockup, LEIA-ME, zip

Dependências: pip install trimesh manifold3d shapely svgelements matplotlib lxml scikit-image
PDF -> SVG usa `pdftocairo` (poppler).
"""
import argparse, json, os, pickle, subprocess, sys, zipfile, math
import numpy as np

# ---------------------------------------------------------------- utilidades 2D
def _shp():
    from shapely.geometry import Polygon, MultiPolygon, LineString
    from shapely.ops import unary_union
    from shapely import affinity
    return Polygon, MultiPolygon, LineString, unary_union, affinity

def parts(g):
    if g is None or g.is_empty: return []
    if g.geom_type == 'Polygon': return [g]
    return [p for p in getattr(g, 'geoms', []) if p.geom_type == 'Polygon']

def clean_geom(g, min_area=0.05):
    Polygon, _, _, unary_union, _ = _shp()
    ps = [Polygon(p.exterior, [i for i in p.interiors if Polygon(i).area > min_area])
          for p in parts(g.buffer(0)) if p.area > min_area]
    return unary_union(ps) if ps else Polygon()

# ---------------------------------------------------------------- 3MF
NS = '{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}'
PN = '{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}'

def _T(s):
    v = [float(x) for x in s.split()]; M = np.eye(4)
    M[:3, :3] = np.array(v[:9]).reshape(3, 3); M[3, :3] = v[9:12]; return M

def load_3mf(path):
    """Retorna {build_object_id: [(part_id, V(n,3), F(m,3))]} já com transformações aplicadas + metadados."""
    from lxml import etree
    z = zipfile.ZipFile(path)
    cache = {}
    import re
    rx_obj = re.compile(rb'<object\b([^>]*)>(.*?)</object>', re.S)
    rx_att = re.compile(rb'([\w:]+)="([^"]*)"')
    rx_v = re.compile(rb'<vertex\s+x="([^"]+)"\s+y="([^"]+)"\s+z="([^"]+)"')
    rx_t = re.compile(rb'<triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"')
    rx_c = re.compile(rb'<component\b([^>]*)/>')
    def load_file(p):
        p = p.lstrip('/')
        if p in cache: return cache[p]
        objs = {}
        data = z.read(p)
        for m in rx_obj.finditer(data):
            at = {k.decode(): v.decode() for k, v in rx_att.findall(m.group(1))}
            body = m.group(2); oid = at.get('id')
            if b'<mesh' in body:
                V = np.array(rx_v.findall(body), dtype=float)
                F = np.array(rx_t.findall(body), dtype=np.int64)
                if len(V) != body.count(b'<vertex ') or len(F) != body.count(b'<triangle '):
                    raise ValueError(f'3MF com formato inesperado em {p} objeto {oid}')
                objs[oid] = ('mesh', V, F)
            else:
                comps = []
                for c in rx_c.findall(body):
                    ca = {k.decode(): v.decode() for k, v in rx_att.findall(c)}
                    comps.append((ca.get('p:path', '/' + p), ca.get('objectid'), _T(ca.get('transform', '1 0 0 0 1 0 0 0 1 0 0 0'))))
                objs[oid] = ('comp', comps)
        cache[p] = objs; return objs
    def resolve(p, oid, M):
        o = load_file(p).get(oid)
        if o is None: return []
        if o[0] == 'mesh':
            V = np.c_[o[1], np.ones(len(o[1]))] @ M
            return [(oid, V[:, :3], o[2])]
        out = []
        for cp, cid, CM in o[1]: out += resolve(cp, cid, CM @ M)
        return out
    root = etree.parse(z.open('3D/3dmodel.model')).getroot()
    res = {}
    for it in root.find(NS + 'build'):
        oid = it.get('objectid')
        res[oid] = resolve('/3D/3dmodel.model', oid, _T(it.get('transform', '1 0 0 0 1 0 0 0 1 0 0 0')))
    # metadados Bambu (nomes, subtipos, placas)
    meta = {'objetos': {}, 'placas': {}}
    if 'Metadata/model_settings.config' in z.namelist():
        r = etree.parse(z.open('Metadata/model_settings.config')).getroot()
        for o in r.findall('object'):
            md = {m.get('key'): m.get('value') for m in o.findall('metadata')}
            meta['objetos'][o.get('id')] = {
                'nome': md.get('name'),
                'partes': {p.get('id'): {'subtipo': p.get('subtype'),
                                          'nome': {m.get('key'): m.get('value') for m in p.findall('metadata')}.get('name')}
                           for p in o.findall('part')}}
        for p in r.findall('plate'):
            pm = {m.get('key'): m.get('value') for m in p.findall('metadata')}
            for inst in p.findall('model_instance'):
                e = inst.find("metadata[@key='object_id']")
                if e is not None: meta['placas'][e.get('value')] = (pm.get('plater_id'), pm.get('plater_name') or '')
    return res, meta

def _render_mesh_views(parts_list, views, out_png, title=''):
    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    pal = ['#d0d0d0', '#2050c0', '#e05050', '#40b040', '#e0a020', '#9050c0']
    fig, axs = plt.subplots(1, len(views), figsize=(5.5 * len(views), 5.5))
    axs = np.atleast_1d(axs)
    L = np.array([0.3, 0.5, 0.8]); L /= np.linalg.norm(L)
    for ax, (axes, depth, sign, t) in zip(axs, views):
        P = []; C = []; D = []
        for i, (pid, V, F) in enumerate(parts_list):
            T = V[F]; n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
            n /= np.linalg.norm(n, axis=1)[:, None] + 1e-12
            vis = n[:, depth] * sign > 0; T = T[vis]; n = n[vis]
            s = 0.35 + 0.65 * np.abs(n @ L)
            col = np.array(matplotlib.colors.to_rgb(pal[min(i, len(pal) - 1)]))
            P.append(T[:, :, axes]); C.append(s[:, None] * col); D.append(T[:, :, depth].mean(1) * sign)
        if P:
            P = np.concatenate(P); C = np.concatenate(C); D = np.concatenate(D); o = np.argsort(D)
            if len(P) > 60000:  # malha grande: pontos (rápido) em vez de polígonos
                cen = P.mean(1)
                ax.scatter(cen[o, 0], cen[o, 1], c=C[o], s=0.6, marker='s', linewidths=0)
            else:
                ax.add_collection(PolyCollection(P[o], facecolors=C[o], edgecolors='none'))
        ax.autoscale(); ax.set_aspect('equal'); ax.set_title(t); ax.grid(alpha=.3)
    fig.suptitle(title); plt.tight_layout(); plt.savefig(out_png, dpi=70); plt.close()

def cmd_analisar(a):
    os.makedirs(a.saida, exist_ok=True)
    res, meta = load_3mf(a.modelo)
    rel = []
    print(f"{'obj':>5} {'placa':>5}  {'X':>6} {'Y':>6} {'Z':>6}  tipo provável              nome")
    for oid, pl in res.items():
        if not pl: continue
        allV = np.vstack([p[1] for p in pl]); size = allV.max(0) - allV.min(0)
        # orienta tamanho de peças planas pelo retângulo mínimo
        tipo = 'outro'
        if size[2] <= 5:
            from shapely.geometry import MultiPoint
            mrr = MultiPoint(allV[::max(1, len(allV) // 20000), :2]).minimum_rotated_rectangle
            xs, ys = mrr.exterior.xy
            e = sorted({round(math.dist((xs[i], ys[i]), (xs[i + 1], ys[i + 1])), 1) for i in range(2)})
            if max(e) > 120: tipo = 'texto/faixa plana (anel da base?)'
            else: tipo = 'emblema/peça plana (referência de tamanho)'
            dims_planas = e
        elif size[2] > 60 and max(size[:2]) > 90: tipo = 'corpo da caneca'
        elif size[2] <= 30 and abs(size[0] - size[1]) < 5: tipo = 'base / anel'
        info = meta['objetos'].get(oid, {})
        placa = meta['placas'].get(oid, ('?', ''))
        neg = [p for p in info.get('partes', {}).values() if p['subtipo'] == 'negative_part']
        item = {'objeto': oid, 'placa': placa[0], 'nome_placa': placa[1], 'nome': info.get('nome'),
                'tamanho_xyz_mm': [round(float(x), 1) for x in size], 'tipo': tipo,
                'partes': len(pl), 'partes_negativas': len(neg)}
        if size[2] <= 5: item['tamanho_real_plano_mm'] = dims_planas
        rel.append(item)
        print(f"{oid:>5} {placa[0]:>5}  {size[0]:6.1f} {size[1]:6.1f} {size[2]:6.1f}  {tipo:<26} {info.get('nome') or ''} {placa[1]}")
        c = allV.mean(0)
        pls = [(p[0], p[1] - [c[0], c[1], 0], p[2]) for p in pl]
        views = [((0, 1), 2, 1, 'topo')] if size[2] <= 5 else \
                [((0, 2), 1, -1, 'frente -Y'), ((1, 2), 0, 1, 'lado +X'), ((0, 2), 1, 1, 'trás +Y'), ((0, 1), 2, 1, 'topo')]
        _render_mesh_views(pls, views, os.path.join(a.saida, f'obj_{oid}.png'), f'obj {oid} – {tipo}')
    json.dump(rel, open(os.path.join(a.saida, 'analise_3mf.json'), 'w'), ensure_ascii=False, indent=1)
    print(f"\nImagens e analise_3mf.json em {a.saida}")

# ---------------------------------------------------------------- LOGO
def _svg_elements(svg_path):
    from svgelements import SVG, Path, Shape, Move, Close, Color
    Polygon, _, LineString, unary_union, _ = _shp()
    from shapely.validation import make_valid
    svg = SVG.parse(svg_path)
    PT = 25.4 / 72  # unidades do pdftocairo = pt; SVGs comuns em px (96dpi) – escala é refeita depois
    els = []; avisos = []
    raw = open(svg_path, encoding='utf-8', errors='ignore').read()
    if '<image' in raw: avisos.append('O arquivo contém IMAGEM embutida (bitmap). Essa parte não é vetor e foi ignorada.')
    if '<text' in raw: avisos.append('O arquivo contém TEXTO não convertido em curvas. Converta em curvas (Corel: Ctrl+Q) e exporte de novo.')
    for e in svg.elements():
        if not isinstance(e, Shape): continue
        p = Path(e) if not isinstance(e, Path) else e
        fill = e.fill if e.fill is not None and e.fill.value is not None and e.fill.alpha != 0 else None
        stroke = e.stroke if e.stroke is not None and e.stroke.value is not None and e.stroke.alpha != 0 else None
        sw = float(e.stroke_width or 0) if stroke is not None else 0
        subs = []; cur = []
        for seg in p.segments():
            if isinstance(seg, Move):
                if len(cur) > 1: subs.append(cur)
                cur = [(seg.end.x, seg.end.y)]
            elif isinstance(seg, Close):
                if len(cur) > 1: subs.append(cur + [cur[0]])
                cur = []
            else:
                try: Lseg = seg.length(error=1e-3)
                except Exception: Lseg = 1
                n = max(2, int(np.ceil(Lseg / 0.25)))
                for t in np.linspace(0, 1, n)[1:]:
                    q = seg.point(t); cur.append((q.x, q.y))
        if len(cur) > 1: subs.append(cur)
        g = None
        if fill is not None:
            for s in subs:
                if len(s) < 3: continue
                P = make_valid(Polygon(s)).buffer(0)
                g = P if g is None else g.symmetric_difference(P)
            cor = fill.hex[:7]
        elif stroke is not None and sw > 0:
            g = unary_union([LineString(s).buffer(sw / 2, cap_style=2, join_style=2) for s in subs if len(s) > 1])
            cor = stroke.hex[:7]
            avisos.append(f'Elemento com só contorno (traço) convertido em forma: largura {sw:.2f} un.')
        if g is None or g.is_empty: continue
        from shapely import affinity
        g = affinity.scale(g, PT, -PT, origin=(0, 0))
        els.append({'geom': g, 'cor': cor.lower()})
    return els, avisos

def _raster_elements(img_path, n_cores=3):
    from PIL import Image
    from skimage import measure
    Polygon, _, _, unary_union, _ = _shp()
    im = Image.open(img_path).convert('RGBA'); A = np.asarray(im).astype(float)
    alpha = A[:, :, 3] > 128
    rgb = A[:, :, :3]
    if alpha.all():  # fundo = cor dominante das bordas
        borda = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
        bg = np.median(borda, 0); mask = np.linalg.norm(rgb - bg, axis=2) > 60
    else:
        mask = alpha
    px = rgb[mask]
    # k-means simples
    rng = np.random.default_rng(0); cent = px[rng.choice(len(px), n_cores, replace=False)]
    for _ in range(15):
        lab = np.argmin(((px[:, None] - cent[None]) ** 2).sum(2), 1)
        cent = np.array([px[lab == k].mean(0) if (lab == k).any() else cent[k] for k in range(n_cores)])
    full = -np.ones(mask.shape, int); full[mask] = lab
    els = []; avisos_r = []
    H = mask.shape[0]
    for k in range(n_cores):
        m = np.pad(full == k, 1)
        if m.sum() < 50: continue
        g = None
        for c in measure.find_contours(m.astype(float), 0.5):
            if len(c) < 4: continue
            P = Polygon(np.c_[c[:, 1], H - c[:, 0]]).buffer(0)
            g = P if g is None else g.symmetric_difference(P)
        if g is None: continue
        g = g.simplify(0.6).buffer(0)
        cor = '#%02x%02x%02x' % tuple(int(x) for x in cent[k])
        pequenos = 0
        for p in parts(g):
            if p.area > 30: els.append({'geom': p, 'cor': cor})
            elif p.area > 2: pequenos += 1
        if pequenos and any(e['cor'] == cor for e in els): avisos_r.append(f'{pequenos} detalhe(s) muito pequeno(s) na cor {cor} foram descartados (pingos de i, acentos?). Confira no mapa.')
    return els, avisos_r + ['Logo em bitmap: vetorizado automaticamente. Cantos e curvas podem perder fidelidade – prefira SVG/PDF vetorial.']

def _fill(ax, poly, color, **kw):
    """Desenha polígono com furos de verdade (não pinta por cima do que está dentro do furo)."""
    from matplotlib.path import Path as MPath
    from matplotlib.patches import PathPatch
    from shapely.geometry.polygon import orient
    poly = orient(poly, 1.0)
    rings = [np.asarray(poly.exterior.coords)] + [np.asarray(i.coords) for i in poly.interiors]
    verts = np.concatenate(rings); codes = []
    for r in rings: codes += [MPath.MOVETO] + [MPath.LINETO] * (len(r) - 2) + [MPath.CLOSEPOLY]
    ax.add_patch(PathPatch(MPath(verts, codes), facecolor=color, **kw))

def cmd_logo(a):
    import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
    os.makedirs(a.saida, exist_ok=True)
    src = a.logo; ext = os.path.splitext(src)[1].lower()
    if ext == '.pdf':
        svg = os.path.join(a.saida, 'logo.svg')
        subprocess.run(['pdftocairo', '-svg', '-f', '1', '-l', '1', src, svg], check=True)
        els, av = _svg_elements(svg)
    elif ext == '.svg':
        els, av = _svg_elements(src)
    elif ext in ('.png', '.jpg', '.jpeg', '.webp'):
        els, av = _raster_elements(src, a.cores)
    else:
        sys.exit(f'Formato {ext} não suportado. Use SVG, PDF, AI/EPS exportado para PDF, ou PNG.')
    # descarta fundos: retângulo (quase) cheio, desenhado por baixo, que contém a maioria dos outros elementos
    keep = []
    for i, e in enumerate(els):
        g = e['geom']; x0, y0, x1, y1 = g.bounds; bba = max((x1 - x0) * (y1 - y0), 1e-9)
        outros = [o['geom'].representative_point() for o in els[i + 1:]]
        dentro = sum(1 for c in outros if x0 <= c.x <= x1 and y0 <= c.y <= y1)
        if len(outros) >= 1 and g.area / bba > 0.97 and dentro >= 0.8 * len(outros) and i <= 1:
            av.append(f'Elemento {i} parece ser um FUNDO retangular ({e["cor"]}) e foi descartado.'); continue
        keep.append(e)
    els = keep
    for i, e in enumerate(els): e['id'] = i
    allb = np.array([e['geom'].bounds for e in els]); X0, Y0 = allb[:, :2].min(0); X1, Y1 = allb[:, 2:].max(0)
    tab = []
    for e in els:
        x0, y0, x1, y1 = e['geom'].bounds
        e['regiao_norm'] = [round(float((x0 - X0) / (X1 - X0)), 3), round(float((Y1 - y1) / (Y1 - Y0)), 3),
                            round(float((x1 - X0) / (X1 - X0)), 3), round(float((Y1 - y0) / (Y1 - Y0)), 3)]
        tab.append({'id': e['id'], 'cor': e['cor'], 'ilhas': len(parts(e['geom'])),
                    'regiao_norm_x0y0x1y1_topo_esq': e['regiao_norm'], 'area_rel': round(float(e['geom'].area / ((X1 - X0) * (Y1 - Y0))), 4)})
    pickle.dump({'elementos': els, 'bbox': [X0, Y0, X1, Y1], 'fonte': src}, open(os.path.join(a.saida, 'logo_elementos.pkl'), 'wb'))
    json.dump({'elementos': tab, 'avisos': av}, open(os.path.join(a.saida, 'logo_elementos.json'), 'w'), ensure_ascii=False, indent=1)
    # mapa numerado
    fig, ax = plt.subplots(figsize=(14, 14 * (Y1 - Y0) / (X1 - X0) + 1))
    for e in els:
        for p in parts(e['geom']):
            _fill(ax, p, e['cor'] if e['cor'] not in ('#ffffff',) else '#eeeeee', ec='k', lw=0.3)
        c = e['geom'].representative_point()
        ax.text(c.x, c.y, str(e['id']), fontsize=13, weight='bold', color='white', ha='center', va='center',
                bbox=dict(boxstyle='round,pad=0.15', fc='#c00000', ec='none', alpha=.85))
    ax.autoscale(); ax.set_aspect('equal'); ax.axis('off'); ax.set_title('Elementos do logo (número = id para o pedido.json)')
    plt.tight_layout(); plt.savefig(os.path.join(a.saida, 'logo_mapa.png'), dpi=80); plt.close()
    print(f'{len(els)} elementos. Mapa: {a.saida}/logo_mapa.png')
    for t in tab: print(f"  id {t['id']:>3}  cor {t['cor']}  ilhas {t['ilhas']:>3}  região {t['regiao_norm_x0y0x1y1_topo_esq']}  área {t['area_rel']}")
    for x in av: print('AVISO:', x)

# ---------------------------------------------------------------- GERAR
def _select(els, sel, bbox):
    X0, Y0, X1, Y1 = bbox; out = []
    for e in els:
        if 'ids' in sel and e['id'] in sel['ids']: out.append(e); continue
        if 'excluir_ids' in sel and e['id'] in sel['excluir_ids']: continue
        if 'regiao' in sel:
            rx0, ry0, rx1, ry1 = sel['regiao']
            c = e['geom'].representative_point()
            u = (c.x - X0) / (X1 - X0); v = (Y1 - c.y) / (Y1 - Y0)
            if rx0 <= u <= rx1 and ry0 <= v <= ry1: out.append(e)
    return out

def _widen_gaps(g, mingap):
    """Alarga só vãos estreitos ENTRE partes diferentes (ex.: letras vizinhas). Cantos internos da mesma letra ficam intactos."""
    _, _, _, unary_union, _ = _shp()
    ilhas = parts(g)
    narrow = g.buffer(mingap / 2, join_style=1).buffer(-mingap / 2, join_style=1).difference(g)
    vaos = []
    for p in parts(narrow):
        if p.area <= 0.02: continue
        pb = p.buffer(0.02)
        if sum(1 for il in ilhas if il.intersects(pb)) >= 2: vaos.append(p)
    if not vaos: return g
    return clean_geom(g.difference(unary_union(vaos).buffer(0.15)))

def _alisar_anel(coords, sigma=0.25, passo=0.05, janela=1.0, canto_graus=40, protege=0.25):
    """Alisa degraus/serrilhados pequenos do contorno preservando cantos reais.
    Canto real = giro acumulado grande numa janela de ±janela mm; degrau = dois giros opostos que se anulam."""
    P = np.asarray(coords)[:-1]
    if len(P) < 4: return coords
    seg = np.diff(np.vstack([P, P[:1]]), axis=0); L = np.hypot(seg[:, 0], seg[:, 1]); per = L.sum()
    if per < 1: return coords
    n = max(16, int(per / passo)); t = np.linspace(0, per, n, endpoint=False)
    cum = np.concatenate([[0], np.cumsum(L)])
    Q = np.c_[np.interp(t, cum, np.r_[P[:, 0], P[0, 0]]), np.interp(t, cum, np.r_[P[:, 1], P[0, 1]])]
    d = np.roll(Q, -1, 0) - Q; ang = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))
    turn = np.diff(np.r_[ang, ang[0] + (ang[-1] - ang[0]) + (np.arctan2(*(d[0][::-1])) - np.arctan2(*(d[-1][::-1])))])
    turn = (turn + np.pi) % (2 * np.pi) - np.pi
    w = max(1, int(janela / passo)); k = np.ones(2 * w + 1)
    net = np.convolve(np.r_[turn[-w:], turn, turn[:w]], k, 'valid')
    canto = np.abs(np.degrees(net)) > canto_graus
    # canto real = pico local do giro acumulado acima do limite; protege só a vizinhança imediata
    pico = canto & (np.abs(net) >= np.maximum(np.abs(np.roll(net, 1)), np.abs(np.roll(net, -1))))
    wp = max(1, int(protege / passo)); kp = np.ones(2 * wp + 1)
    canto = np.convolve(np.r_[pico[-wp:], pico, pico[:wp]].astype(float), kp, 'valid') > 0
    sg = sigma / passo; m = int(3 * sg); x = np.arange(-m, m + 1); G = np.exp(-0.5 * (x / sg) ** 2); G /= G.sum()
    S = np.c_[np.convolve(np.r_[Q[-m:, 0], Q[:, 0], Q[:m, 0]], G, 'valid'), np.convolve(np.r_[Q[-m:, 1], Q[:, 1], Q[:m, 1]], G, 'valid')]
    R = np.where(canto[:, None], Q, S)
    return np.vstack([R, R[:1]])

def _suavizar(g, r):
    """Remove dentes, degraus e serrilhados do vetor (< ~2r) ilha por ilha, mantendo cantos reais das letras."""
    if r <= 0: return g
    Polygon, _, _, unary_union, _ = _shp()
    out = []
    for p in parts(g):
        q = p.buffer(r, join_style=1).buffer(-r, join_style=1)      # fecha entalhes pequenos
        q = q.buffer(-r, join_style=1).buffer(r, join_style=1)      # tira pontas/rebarbas pequenas
        for qq in parts(q.intersection(p.buffer(r))):
            ext = _alisar_anel(qq.exterior.coords, sigma=max(r, 0.15) * 1.6)
            holes = [_alisar_anel(h.coords, sigma=max(r, 0.15) * 1.6) for h in qq.interiors]
            out.append(Polygon(ext, holes).buffer(0))
    return clean_geom(unary_union(out))

def _backing(g, borda, fechar):
    Polygon, _, _, unary_union, _ = _shp()
    for r in [fechar, fechar * 1.5, fechar * 2, fechar * 3]:
        b = g.buffer(r, join_style=1).buffer(-r + borda, join_style=1).union(g.buffer(borda))
        b = unary_union([Polygon(p.exterior) for p in parts(b)])
        # peça única E sem "cintura" fina (ligação < ~2 mm quebra ao moldar)
        if len(parts(b)) == 1 and len([p for p in parts(b.buffer(-1.0)) if p.area > 1]) == 1: return b, r
    return b, r

def _extrude(g, h, z, como_manifold=False):
    import manifold3d as mf, trimesh
    g = clean_geom(g.simplify(0.01))
    rings = []
    for p in parts(g):
        rings.append(np.array(p.exterior.coords[:-1]))
        rings += [np.array(i.coords[:-1]) for i in p.interiors]
    MF = mf.CrossSection(rings, mf.FillRule.EvenOdd).extrude(h).translate((0, 0, z))
    if como_manifold: return MF
    M = MF.to_mesh()
    m = trimesh.Trimesh(M.vert_properties[:, :3], M.tri_verts, process=False)
    assert m.is_watertight and m.is_winding_consistent and m.volume > 0, 'malha não estanque'
    return m

def _thin(g, w):
    op = g.buffer(-w / 2).buffer(w / 2)
    return [p for p in parts(g.difference(op)) if p.area > 0.3]

NOME_COR = {'#000000': 'preto', '#ffffff': 'branco'}
def _nome_cor(hexc):
    if hexc in NOME_COR: return NOME_COR[hexc]
    r, g, b = [int(hexc[i:i + 2], 16) for i in (1, 3, 5)]
    h = __import__('colorsys').rgb_to_hsv(r / 255, g / 255, b / 255)
    if h[1] < 0.15: return 'cinza' if 0.15 < h[2] < 0.85 else ('branco' if h[2] >= 0.85 else 'preto')
    nomes = [(15, 'vermelho'), (40, 'laranja'), (70, 'amarelo'), (160, 'verde'), (200, 'ciano'), (260, 'azul'), (300, 'roxo'), (345, 'rosa'), (360, 'vermelho')]
    deg = h[0] * 360
    if deg <= 45 and h[2] < 0.55: return 'marrom'
    return next(n for lim, n in nomes if deg <= lim)

def cmd_gerar(a):
    import trimesh
    import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
    Polygon, _, _, unary_union, affinity = _shp()
    spec = json.load(open(a.pedido)); base_dir = os.path.dirname(os.path.abspath(a.pedido))
    P = lambda p: p if os.path.isabs(p) else os.path.join(base_dir, p)
    out = P(spec.get('saida', 'saida')); os.makedirs(out, exist_ok=True)
    L = pickle.load(open(P(spec['logo_elementos']), 'rb'))
    BASE_T = spec.get('espessura_base', 1.0); ART_T = spec.get('espessura_arte', 1.0)
    BORDA = spec.get('borda', 1.2); BICO = spec.get('bico', 0.4); VAO = spec.get('vao_min', max(0.5, BICO + 0.1))
    R = spec.get('raio_caneca', 49.0)
    cor_base = spec.get('cor_contorno', 'preto')
    cliente = spec.get('cliente', 'CLIENTE').upper().replace(' ', '_')
    relatorio = {'cliente': cliente, 'pecas': []}; built = []; malhas = []
    print(f'== {cliente} ==')
    for pc in spec['pecas']:
        nome = pc['nome'].upper().replace(' ', '_')
        sel = _select(L['elementos'], pc['selecao'], L['bbox'])
        if not sel: sys.exit(f'Peça {nome}: seleção vazia.')
        # camadas por cor (ordem do desenho: posteriores ficam por cima)
        if pc.get('arte_cor_unica', spec.get('arte_cor_unica', False)) or len({e['cor'] for e in sel}) == 1:
            cor = pc.get('cor_arte') or sel[0]['cor']
            layers = [(cor, unary_union([e['geom'] for e in sel]))]
        else:
            layers = []
            for i, e in enumerate(sel):
                above = unary_union([x['geom'] for x in sel[i + 1:] if x['cor'] != e['cor']]) if i + 1 < len(sel) else None
                g = e['geom'].difference(above) if above is not None and not above.is_empty else e['geom']
                layers.append((e['cor'], g))
            merged = {}
            for c, g in layers: merged[c] = g if c not in merged else merged[c].union(g)
            layers = list(merged.items())
        allg = unary_union([g for _, g in layers]); x0, y0, x1, y1 = allg.bounds
        w, h = x1 - x0, y1 - y0
        if 'largura' in pc: s = pc['largura'] / w
        elif 'altura' in pc: s = pc['altura'] / h
        elif 'caber' in pc: s = min((pc['caber'][0] - 2 * BORDA) / w, (pc['caber'][1] - 2 * BORDA) / h)
        else: sys.exit(f'Peça {nome}: informe largura, altura ou caber.')
        tf = lambda g: affinity.translate(affinity.scale(g, s, s, origin=(0, 0)), -(x0 + x1) / 2 * s, -(y0 + y1) / 2 * s)
        SUAV = pc.get('suavizar', spec.get('suavizar', 0.15))
        layers = [(c, _suavizar(clean_geom(tf(g)), SUAV)) for c, g in layers]
        art = clean_geom(unary_union([g for _, g in layers]))
        art_w = _widen_gaps(art, VAO)
        layers = [(c, clean_geom(g.intersection(art_w))) for c, g in layers]
        layers = [(c, g) for c, g in layers if not g.is_empty]
        back, rfechar = _backing(art_w, BORDA, spec.get('fechar', 3.0))
        bx0, by0, bx1, by1 = back.bounds; BW, BH = bx1 - bx0, by1 - by0
        avisos = []
        if len(parts(back)) > 1: avisos.append(f'contorno ficou em {len(parts(back))} pedaços (elementos muito afastados)')
        finos = sum(len(_thin(g, BICO)) for _, g in layers)
        if finos: avisos.append(f'{finos} detalhe(s) da arte com menos de {BICO} mm – podem afinar ou sumir (bico 0,2 resolve)')
        flecha = R - math.sqrt(max(R * R - (BW / 2) ** 2, 0)) if BW / 2 < R else float('inf')
        if BW > 60: avisos.append(f'largura {BW:.0f} mm em caneca de raio {R:.0f} mm: flecha {flecha:.1f} mm – moldagem mais difícil, teste antes')
        ref = pc.get('referencia_mm')
        if ref and (BW > ref[0] * 1.15 or BH > ref[1] * 1.15):
            avisos.append(f'peça {BW:.1f}×{BH:.1f} mm passa >15% da referência {ref[0]}×{ref[1]} mm')
        files = []
        bt = pc.get('espessura_base', BASE_T); at = pc.get('espessura_arte', ART_T)
        for e_ in (bt, at):
            if abs(e_ / 0.2 - round(e_ / 0.2)) > 1e-6: avisos.append(f'espessura {e_} mm não é múltiplo de 0,2 mm (altura de camada): o fatiador vai arredondar')
        acab = pc.get('acabamento', spec.get('acabamento', 'nivelado'))
        if acab == 'nivelado':  # arte embutida: contorno com 'janelas' na camada de cima, tudo na mesma altura
            janela = clean_geom(back.difference(unary_union([g for _, g in layers])))
            U = (_extrude(back, bt, 0, True) + _extrude(janela, at, bt, True)).to_mesh()
            mb = trimesh.Trimesh(U.vert_properties[:, :3], U.tri_verts, process=False)
            assert mb.is_watertight and mb.volume > 0, 'contorno nivelado não estanque'
        else:
            mb = _extrude(back, bt, 0)
        fb = f'{nome}_1_BASE_contorno_{cor_base}.stl'; mb.export(os.path.join(out, fb)); files.append(fb)
        tops = []
        for i, (c, g) in enumerate(layers):
            mt = _extrude(g, at, bt); tops.append(mt)
            ft = f'{nome}_{i + 2}_ARTE_{_nome_cor(c)}.stl'; mt.export(os.path.join(out, ft)); files.append(ft)
        if acab == 'nivelado':
            mu = _extrude(back, bt + at, 0)
        else:
            U = _extrude(back, bt, 0, True)
            for c, g in layers: U = U + _extrude(g, at, bt, True)
            U = U.to_mesh(); mu = trimesh.Trimesh(U.vert_properties[:, :3], U.tri_verts, process=False)
        fu = f'{nome}_peca_unica_1cor.stl'; mu.export(os.path.join(out, fu)); files.append(fu)
        info = {'nome': nome, 'quantidade': pc.get('quantidade', 1), 'posicao': pc.get('posicao', ''),
                'tamanho_total_mm': [round(BW, 1), round(BH, 1)], 'tamanho_arte_mm': [round(w * s, 1), round(h * s, 1)],
                'espessura_mm': round(bt + at, 2), 'espessura_camadas_mm': [bt, at], 'acabamento': acab, 'cores_arte': [c for c, _ in layers], 'arquivos': files, 'avisos': avisos,
                'flecha_na_caneca_mm': round(flecha, 1)}
        relatorio['pecas'].append(info); built.append((pc, nome, back, layers))
        partes_m = [('contorno', mb, None)] + [(f'arte {_nome_cor(c)}', mt, c) for (c, _), mt in zip(layers, tops)]
        variantes = None
        if pc.get('espelhar_par'):
            # par para os dois lados da caneca: um normal (lado direito, vendo de frente) e um espelhado (lado esquerdo)
            esp = lambda m: trimesh.Trimesh(m.vertices * [-1, 1, 1], m.faces[:, ::-1], process=False)
            partes_e = [(pn, esp(m), c) for pn, m, c in partes_m]
            for (pn, m, c), f0 in zip(partes_e, [fb] + [f'{nome}_{i + 2}_ARTE_{_nome_cor(c)}.stl' for i, (c, _) in enumerate(layers)]):
                fe = f0.replace(nome, nome + '_ESPELHADO', 1); m.export(os.path.join(out, fe)); files.append(fe)
            fe = f'{nome}_ESPELHADO_peca_unica_1cor.stl'; esp(mu).export(os.path.join(out, fe)); files.append(fe)
            variantes = [(' - lado direito', partes_m), (' ESPELHADO - lado esquerdo', partes_e)]
            info['espelhado'] = True
        malhas.append((nome, pc.get('quantidade', 1), partes_m, variantes))
        print(f"  {nome}: {BW:.1f} × {BH:.1f} mm  (arte {w * s:.1f} × {h * s:.1f})  x{info['quantidade']}  cores {[ _nome_cor(c) for c in info['cores_arte']]}")
        for x in avisos: print('     AVISO:', x)
    # prévia
    n = len(built)
    fig, axs = plt.subplots(1, n, figsize=(6.5 * n, 6)); axs = np.atleast_1d(axs)
    cb = {'preto': '#1a1a1a', 'branco': '#f4f4f4'}.get(cor_base, cor_base if cor_base.startswith('#') else '#1a1a1a')
    for ax, (pc, nome, back, layers) in zip(axs, built):
        for p in parts(back):
            _fill(ax, p, cb, ec='#888', lw=0.5)
        for c, g in layers:
            for p in parts(g):
                _fill(ax, p, c, lw=0)
            for p in _thin(g, BICO):
                x, y = p.exterior.xy; ax.plot(x, y, 'r', lw=1.2)
        bx0, by0, bx1, by1 = back.bounds
        ax.set_title(f'{nome}  ×{pc.get("quantidade", 1)}\n{bx1 - bx0:.1f} × {by1 - by0:.1f} mm (vermelho = traço < {BICO} mm)')
        ax.autoscale(); ax.set_aspect('equal'); ax.grid(alpha=.25)
    plt.tight_layout(); plt.savefig(os.path.join(out, 'PREVIEW_pecas.png'), dpi=110); plt.close()
    # mockup
    try:
        _mockup(spec, P, out, built, R, cb)
    except Exception as ex:
        print('mockup falhou:', ex)
    # projeto Bambu Studio (.3mf): uma placa por peça, partes com filamento atribuído
    try:
        relatorio['projeto_3mf'] = _projeto_3mf(spec, P, out, cliente, malhas, cor_base)
    except Exception as ex:
        print('3MF falhou:', ex); relatorio['projeto_3mf'] = {'erro': str(ex)}
    # LEIA-ME + zip
    _leiame(spec, relatorio, out, cor_base, BASE_T, ART_T, BICO, VAO)
    json.dump(relatorio, open(os.path.join(out, 'relatorio.json'), 'w'), ensure_ascii=False, indent=1)
    zp = os.path.join(os.path.dirname(out.rstrip('/')), f'{cliente}_caneca_STL.zip')
    with zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(out)):
            if f.endswith(('.stl', '.png', '.txt', '.3mf')): z.write(os.path.join(out, f), f)
    print('Saída:', out); print('Zip:', zp)

HEX_COR = {'preto': '#000000', 'branco': '#FFFFFF', 'cinza': '#808080', 'amarelo': '#FFCC29', 'vermelho': '#D62828',
           'azul': '#1F4FBF', 'verde': '#2E9E44', 'laranja': '#F08A24', 'marrom': '#6B3E1F', 'rosa': '#F28CB1', 'roxo': '#7B3FA0'}
def _projeto_3mf(spec, P, out, cliente, malhas, cor_base):
    """Projeto Bambu Studio: cada peça numa placa (quantidade = cópias na placa), cada peça = 1 objeto com partes
    (contorno + arte por cor) e filamento atribuído. Configurações de impressora/filamento copiadas do 3MF Print4You."""
    import uuid
    tpl = spec.get('projeto_modelo_3mf') or spec.get('mockup', {}).get('modelo_3mf')
    proj = None; fil_cfgs = {}
    if tpl and os.path.exists(P(tpl)):
        zt = zipfile.ZipFile(P(tpl))
        if 'Metadata/project_settings.config' in zt.namelist():
            proj = json.loads(zt.read('Metadata/project_settings.config'))
            for n in zt.namelist():
                if n.startswith('Metadata/filament_settings_'): fil_cfgs[n] = zt.read(n)
    # filamentos: 1 = contorno, 2.. = cores da arte (na ordem em que aparecem)
    hb = cor_base if cor_base.startswith('#') else HEX_COR.get(cor_base, '#000000')
    cores = [hb.upper()]
    for _, _, partes, _v in malhas:
        for _, _, c in partes:
            if c and c.upper() not in cores: cores.append(c.upper())
    n_tpl = len(proj['filament_colour']) if proj else len(cores)
    avisos = []
    if len(cores) > n_tpl:
        avisos.append(f'O projeto modelo tem {n_tpl} filamentos e as peças usam {len(cores)} cores: as cores extras ficaram '
                      f'no filamento {n_tpl}. No Bambu Studio, adicione filamento (+) e troque o filamento dessas partes.')
    fil_of = {c: min(i + 1, n_tpl) for i, c in enumerate(cores)}
    if proj and spec.get('costura_escondida', True):
        proj['seam_position'] = 'back'            # emenda atrás (alça virada para trás)
        proj['seam_slope_type'] = 'external'      # costura chanfrada no contorno externo: marca mais discreta
    if proj:
        fc = list(proj['filament_colour'])
        for c, i in fil_of.items(): fc[i - 1] = c
        proj['filament_colour'] = fc
    # geometria das placas (grade igual ao Bambu Studio: colunas = teto(raiz(n)), passo = mesa x 1,2)
    if proj:
        pa = [tuple(float(v) for v in p.split('x')) for p in proj['printable_area']]
        bw = max(p[0] for p in pa) - min(p[0] for p in pa); bd = max(p[1] for p in pa) - min(p[1] for p in pa)
    else:
        bw = bd = 256.0
    cn = spec.get('caneca')
    base = _caneca_base(P(cn['modelo_3mf']), cn) if cn else None
    n0 = base['nplacas'] if base else 0
    nplates = n0 + len(malhas); cols = _cols(nplates)
    model_files = {}; root_objs = []; build = []; ms_objs = []; plates = []
    oid = (base['max_id'] + 1) if base else 1; ident = 5000
    for pi0, (nome, qtd, partes, variantes) in enumerate(malhas):
        pi = n0 + pi0
        col, row = pi % cols, pi // cols
        ox, oy = col * bw * 1.2, -row * bd * 1.2
        allv = np.vstack([m.vertices for _, m, _ in partes]); x0, y0 = allv[:, :2].min(0); x1, y1 = allv[:, :2].max(0)
        wpc, hpc = x1 - x0, y1 - y0; gap = 8.0
        porlinha = max(1, int((bw - 20) // (wpc + gap)))
        inst = []
        for k in range(qtd):
            r_, c_ = divmod(k, porlinha); inst.append((c_, r_))
        ncol = min(qtd, porlinha); nrow = (qtd - 1) // porlinha + 1
        model_ids = []
        for k, (c_, r_) in enumerate(inst):
            suf = ''
            if variantes: suf, partes = variantes[k % len(variantes)]
            fname = f'3D/Objects/object_{oid}.model'
            sub = []; comps = []; ms_parts = []
            for j, (pn, m, c) in enumerate(partes):
                vid = oid * 1000 + j + 1
                V = m.vertices - [(x0 + x1) / 2, (y0 + y1) / 2, 0]
                vx = ''.join(f'<vertex x="{a:.5f}" y="{b:.5f}" z="{c2:.5f}"/>' for a, b, c2 in V)
                tx = ''.join(f'<triangle v1="{a}" v2="{b}" v3="{c2}"/>' for a, b, c2 in m.faces)
                sub.append(f'<object id="{vid}" p:UUID="{uuid.uuid4()}" type="model"><mesh><vertices>{vx}</vertices>'
                           f'<triangles>{tx}</triangles></mesh></object>')
                comps.append(f'<component p:path="/{fname}" objectid="{vid}" p:UUID="{uuid.uuid4()}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>')
                fil = 1 if c is None else fil_of[c.upper()]
                ms_parts.append(f'<part id="{vid}" subtype="normal_part"><metadata key="name" value="{nome} - {pn}"/>'
                                f'<metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>'
                                f'<metadata key="extruder" value="{fil}"/><mesh_stat face_count="{len(m.faces)}" edges_fixed="0" '
                                f'degenerate_facets="0" facets_removed="0" facets_reversed="0" backwards_edges="0"/></part>')
            model_files[fname] = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
                'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
                'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n'
                '<metadata name="BambuStudio:3mfVersion">1</metadata>\n<resources>' + ''.join(sub) + '</resources>\n<build/>\n</model>\n')
            obj_name = (nome + suf) if variantes else (nome if qtd == 1 else f'{nome} {k + 1}')
            root_objs.append(f'<object id="{oid}" p:UUID="{uuid.uuid4()}" type="model" name="{obj_name}"><components>{"".join(comps)}</components></object>')
            cx = ox + bw / 2 + (c_ - (ncol - 1) / 2) * (wpc + gap); cy = oy + bd / 2 - (r_ - (nrow - 1) / 2) * (hpc + gap)
            build.append(f'<item objectid="{oid}" p:UUID="{uuid.uuid4()}" transform="1 0 0 0 1 0 0 0 1 {cx:.4f} {cy:.4f} 0" printable="1"/>')
            ms_objs.append(f'<object id="{oid}"><metadata key="name" value="{obj_name}"/><metadata key="extruder" value="1"/>'
                           f'<metadata face_count="{sum(len(m.faces) for _, m, _ in partes)}"/>{"".join(ms_parts)}</object>')
            ident += 1; model_ids.append((oid, ident)); oid += 1
        insts = ''.join(f'<model_instance><metadata key="object_id" value="{o}"/><metadata key="instance_id" value="0"/>'
                        f'<metadata key="identify_id" value="{i}"/></model_instance>' for o, i in model_ids)
        plates.append(f'<plate><metadata key="plater_id" value="{pi + 1}"/><metadata key="plater_name" value="{cliente} - {nome} (x{qtd})"/>'
                      f'<metadata key="locked" value="false"/>{insts}</plate>')
    root = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n'
            f'<metadata name="Application">BambuStudio-{(proj or {}).get("version", "02.08.02.61")}</metadata>\n'
            '<metadata name="BambuStudio:3mfVersion">1</metadata>\n'
            f'<metadata name="Title">{cliente} - pecas caneca</metadata>\n'
            '<resources>' + ''.join(root_objs) + '</resources>\n<build>' + ''.join(build) + '</build>\n</model>\n')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
            ''.join(f'<Relationship Target="/{f}" Id="rel-{i + 1}" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
                    for i, f in enumerate(model_files)) + '</Relationships>\n')
    ms = '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n' + ''.join(ms_objs) + ''.join(plates) + '\n</config>\n'
    fn = os.path.join(out, f'PROJETO_{cliente}_caneca.3mf')
    if base:
        root, rels, ms = _mesclar_caneca(base, cols, root_objs, build, ms_objs, plates, model_files, cliente)
    with zipfile.ZipFile(fn, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                   '<Default Extension="png" ContentType="image/png"/><Default Extension="gcode" ContentType="text/x.gcode"/></Types>\n')
        z.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>\n')
        z.writestr('3D/3dmodel.model', root)
        z.writestr('3D/_rels/3dmodel.model.rels', rels)
        if base:
            for f, data in base['arquivos'].items(): z.writestr(f, data)
        for f, txt in model_files.items(): z.writestr(f, txt)
        z.writestr('Metadata/model_settings.config', ms)
        if proj:
            z.writestr('Metadata/project_settings.config', json.dumps(proj, indent=4, ensure_ascii=False))
            for n, b in fil_cfgs.items(): z.writestr(n, b)
    if base:
        avisos += base['avisos']
    if not proj: avisos.append('Sem 3MF modelo da Print4You: o projeto saiu sem configurações de impressora/filamento.')
    for a in avisos: print('  AVISO 3MF:', a)
    mapa = {f'filamento {i}': c for c, i in fil_of.items()}
    print(f'  Projeto 3MF: {fn}  ({nplates} placas, filamentos {mapa})')
    return {'arquivo': os.path.basename(fn), 'placas': nplates, 'filamentos': mapa, 'avisos': avisos,
            'placas_caneca': base['nomes'] if base else []}

def _cols(n):
    v = math.sqrt(n); return int(round(v)) + (1 if v > round(v) else 0)

def _caneca_base(path3mf, cn):
    """Lê o 3MF Print4You e separa as peças da caneca pedidas (ex.: base, corpo inferior, corpo superior),
    removendo partes indicadas (ex.: texto recortado do anel)."""
    import re
    from lxml import etree
    z = zipfile.ZipFile(path3mf)
    keep = {str(k): v for k, v in cn['objetos'].items()}
    remover = {str(k): [str(x) for x in v] for k, v in cn.get('remover_partes', {}).items()}
    root = etree.fromstring(z.read('3D/3dmodel.model'))
    res = root.find(NS + 'resources'); bld = root.find(NS + 'build')
    ms = etree.fromstring(z.read('Metadata/model_settings.config'))
    # placas antigas: objeto -> índice da placa
    old_plate = {}
    for p in ms.findall('plate'):
        pid = int(p.find("metadata[@key='plater_id']").get('value'))
        for mi in p.findall('model_instance'):
            old_plate[mi.find("metadata[@key='object_id']").get('value')] = pid - 1
    old_cols = _cols(len(ms.findall('plate')))
    pa = json.loads(z.read('Metadata/project_settings.config'))['printable_area'] if 'Metadata/project_settings.config' in z.namelist() else ['0x0', '256x0', '256x256', '0x256']
    pa = [tuple(float(v) for v in q.split('x')) for q in pa]
    bw = max(q[0] for q in pa) - min(q[0] for q in pa); bd = max(q[1] for q in pa) - min(q[1] for q in pa)
    objs = {o.get('id'): o for o in res.findall(NS + 'object')}
    usados = {}  # arquivo -> ids de malha usados
    root_objs = []; builds = []
    ordem = [k for k in keep]  # ordem das placas novas
    for k in ordem:
        o = objs[k]; comps = o.find(NS + 'components')
        for c in list(comps):
            if c.get('objectid') in remover.get(k, []): comps.remove(c); continue
            usados.setdefault(c.get(PN + 'path').lstrip('/'), set()).add(c.get('objectid'))
        o.set('name', keep[k])
        root_objs.append(etree.tostring(o).decode())
        it = next(i for i in bld if i.get('objectid') == k)
        builds.append((k, it.get('transform'), old_plate.get(k, 0)))
    # arquivos de malha: mantém só objetos usados
    arquivos = {}
    rx_obj = re.compile(rb'<object\b[^>]*\bid="(\d+)"[^>]*>.*?</object>', re.S)
    for f, ids in usados.items():
        data = z.read(f)
        data = rx_obj.sub(lambda m: m.group(0) if m.group(1).decode() in ids else b'', data)
        arquivos[f] = data
    # model_settings dos objetos mantidos
    ms_map = {}
    for o in ms.findall('object'):
        k = o.get('id')
        if k not in keep: continue
        for p in o.findall('part'):
            if p.get('id') in remover.get(k, []): o.remove(p)
        nm = o.find("metadata[@key='name']")
        if nm is not None: nm.set('value', keep[k])
        ms_map[k] = etree.tostring(o).decode()
    ms_objs = [ms_map[k] for k in ordem]  # mesma ordem de root_objs
    ids = [int(o.get('id')) for o in res.findall(NS + 'object')]
    for f in z.namelist():
        if f.startswith('3D/Objects/'):
            ids += [int(x) for x in re.findall(rb'<object\b[^>]*\bid="(\d+)"', z.read(f))]
    # alça para trás (+Y da mesa): com costura 'traseira' a emenda fica escondida atrás da alça, alinhada
    if cn.get('alca_para_tras', True):
        for i_obj, (k, tr, oldi) in enumerate(builds):
            o_el = etree.fromstring(root_objs[i_obj]); TB = _T(tr); W = []
            for c in o_el.find(NS + 'components'):
                src = c.get(PN + 'path').lstrip('/')
                m = re.search(rb'<object\b[^>]*\bid="' + c.get('objectid').encode() + rb'"[^>]*>(.*?)</object>', z.read(src), re.S)
                if not m: continue
                Vv = np.array(re.findall(rb'<vertex\s+x="([^"]+)"\s+y="([^"]+)"\s+z="([^"]+)"', m.group(1))[::7], dtype=float)
                if len(Vv): W.append((np.c_[Vv, np.ones(len(Vv))] @ _T(c.get('transform', '1 0 0 0 1 0 0 0 1 0 0 0')) @ TB)[:, :3])
            if not W: continue
            W = np.vstack(W); zc = W[:, 2]
            base = W[zc < zc.min() + 0.3 * (zc.max() - zc.min())]
            cx, cy = np.median(base[:, 0]), np.median(base[:, 1])
            r = np.hypot(W[:, 0] - cx, W[:, 1] - cy); rb_ = np.percentile(r, 60)
            if (r > rb_ + 12).sum() < 50: continue  # sem alça (ex.: aro)
            al = W[r >= np.percentile(r, 99.7)]      # ponta mais distante = direção aproximada da alça
            ang = math.atan2(np.mean(al[:, 1] - cy), np.mean(al[:, 0] - cx))
            # refina pelo plano de simetria da caneca (alça centrada): menor diferença entre a peça e seu espelho
            from scipy.spatial import cKDTree
            Sx = W[np.random.default_rng(0).choice(len(W), min(len(W), 15000), replace=False)][:, :2] - [cx, cy]
            def assim(a):
                ca, sa = math.cos(math.pi / 2 - a), math.sin(math.pi / 2 - a)
                P = Sx @ np.array([[ca, sa], [-sa, ca]]); P[:, 0] -= np.median(P[:, 0])
                M_ = P * [-1, 1]; return cKDTree(P).query(M_)[0].mean()
            cand = [ang + math.radians(x) for x in np.arange(-20, 20.5, 1.0)]
            ang = min(cand, key=assim)
            ang = min([ang + math.radians(x) for x in np.arange(-1, 1.05, 0.1)], key=assim)
            d = math.pi / 2 - ang
            if abs(d) < math.radians(3): continue
            c_, s_ = math.cos(d), math.sin(d)
            R = np.eye(4); R[:2, :2] = [[c_, s_], [-s_, c_]]  # rotação no eixo z (convenção de vetor-linha)
            px, py = (W[:, 0].max() + W[:, 0].min()) / 2, (W[:, 1].max() + W[:, 1].min()) / 2
            Tn = TB.copy(); Tn[:3, :3] = TB[:3, :3] @ R[:3, :3]
            t = TB[3, :3] - [px, py, 0]; t = t @ R[:3, :3] + [px, py, 0]; Tn[3, :3] = t
            vals = list(Tn[:3, :3].reshape(-1)) + list(Tn[3, :3])
            builds[i_obj] = (k, ' '.join(repr(float(v)) for v in vals), oldi)
    # 'pintar': separa uma faixa de altura de um objeto da caneca numa parte própria com outro filamento
    import uuid, manifold3d as mf
    prox = max(ids) + 1
    for regra in cn.get('pintar', []):
        k = str(regra['objeto']); i_obj = ordem.index(k)
        o_el = etree.fromstring(root_objs[i_obj]); comps = o_el.find(NS + 'components')
        ms_el = etree.fromstring(ms_objs[i_obj])
        neg_ids = {p.get('id') for p in ms_el.findall('part') if p.get('subtype') != 'normal_part'}
        novos_sub = []; novos_comps = []; novas_partes = []
        for c in list(comps):
            cid = c.get('objectid')
            if cid in neg_ids: continue
            src = c.get(PN + 'path').lstrip('/')
            m = re.search(rb'<object\b[^>]*\bid="' + cid.encode() + rb'"[^>]*>(.*?)</object>', z.read(src), re.S).group(1)
            V = np.array(re.findall(rb'<vertex\s+x="([^"]+)"\s+y="([^"]+)"\s+z="([^"]+)"', m), dtype=float)
            Fc = np.array(re.findall(rb'<triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"', m), dtype=np.int64)
            V = (np.c_[V, np.ones(len(V))] @ _T(c.get('transform', '1 0 0 0 1 0 0 0 1 0 0 0')))[:, :3]
            M = mf.Manifold(mf.Mesh(np.asarray(V, np.float32), np.asarray(Fc, np.uint32)))
            # 'z' é medido de baixo da peça já posicionada na mesa (mm reais); converte para o sistema local do objeto
            TB = _T(builds[i_obj][1]); Wz = (np.c_[V, np.ones(len(V))] @ TB)[:, 2]; sz = TB[2, 2]
            z0, z1 = [(Wz.min() + zz - TB[3, 2]) / sz for zz in regra['z']]
            R = float(np.abs(V[:, :2] - V[:, :2].mean(0)).max() * 2 + 50)
            cx, cy = (V[:, :2].max(0) + V[:, :2].min(0)) / 2
            slab = mf.Manifold.cube((2 * R, 2 * R, z1 - z0)).translate((cx - R, cy - R, z0))
            for nome_p, G, fil in [(regra.get('nome', 'faixa'), M ^ slab, regra['filamento']), ('resto', M - slab, None)]:
                mm = G.to_mesh(); Vn = np.asarray(mm.vert_properties)[:, :3]; Fn = np.asarray(mm.tri_verts)
                if len(Fn) == 0: continue
                pid = prox; prox += 1
                vx = ''.join(f'<vertex x="{a:.5f}" y="{b:.5f}" z="{q:.5f}"/>' for a, b, q in Vn)
                tx = ''.join(f'<triangle v1="{a}" v2="{b}" v3="{q}"/>' for a, b, q in Fn)
                novos_sub.append(f'<object id="{pid}" p:UUID="{uuid.uuid4()}" type="model"><mesh><vertices>{vx}</vertices><triangles>{tx}</triangles></mesh></object>')
                novos_comps.append((cid, pid))
                ext = f'<metadata key="extruder" value="{fil}"/>' if fil else ''
                novas_partes.append(f'<part id="{pid}" subtype="normal_part"><metadata key="name" value="{keep[k]} - {nome_p}"/>'
                                    f'<metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>{ext}'
                                    f'<mesh_stat face_count="{len(Fn)}" edges_fixed="0" degenerate_facets="0" facets_removed="0" facets_reversed="0" backwards_edges="0"/></part>')
        fname = f'3D/Objects/object_pintura_{k}.model'
        arquivos[fname] = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n'
            '<metadata name="BambuStudio:3mfVersion">1</metadata>\n<resources>' + ''.join(novos_sub) + '</resources>\n<build/>\n</model>\n').encode()
        trocados = {cid for cid, _ in novos_comps}
        for c in list(comps):
            if c.get('objectid') in trocados: comps.remove(c)
        for _, pid in novos_comps:
            e = etree.SubElement(comps, NS + 'component'); e.set(PN + 'path', '/' + fname); e.set('objectid', str(pid))
            e.set(PN + 'UUID', str(uuid.uuid4())); e.set('transform', '1 0 0 0 1 0 0 0 1 0 0 0')
        for p in ms_el.findall('part'):
            if p.get('id') in trocados: ms_el.remove(p)
        txt = etree.tostring(ms_el).decode(); txt = txt.replace('</object>', ''.join(novas_partes) + '</object>')
        root_objs[i_obj] = etree.tostring(o_el).decode(); ms_objs[i_obj] = txt
        ids.append(prox)
    # remove arquivos de malha que ficaram sem uso
    usados_final = set(re.findall(r'p:path="/([^"]+)"', ''.join(root_objs)))
    arquivos = {f: d for f, d in arquivos.items() if f in usados_final}
    meta = [etree.tostring(m).decode() for m in root.findall(NS + 'metadata') if m.get('name') not in ('Title', 'Thumbnail_Middle', 'Thumbnail_Small', 'DesignerCover', 'ProfileCover')]
    return {'nplacas': len(ordem), 'root_objs': root_objs, 'builds': builds, 'arquivos': arquivos, 'ms_objs': ms_objs,
            'max_id': max(ids), 'old_cols': old_cols, 'bw': bw, 'bd': bd, 'meta': meta, 'nomes': [keep[k] for k in ordem],
            'ordem': ordem, 'avisos': []}

def _mesclar_caneca(base, cols, root_objs, build, ms_objs, plates, model_files, cliente):
    bw, bd = base['bw'], base['bd']
    org = lambda i, c: np.array([(i % c) * bw * 1.2, -(i // c) * bd * 1.2])
    b_items = []; b_plates = []; ident = 4000
    for newi, (k, tr, oldi) in enumerate(base['builds']):
        v = [float(x) for x in tr.split()]
        d = org(newi, cols) - org(oldi, base['old_cols']); v[9] += d[0]; v[10] += d[1]
        b_items.append(f'<item objectid="{k}" transform="{" ".join(repr(float(x)) for x in v)}" printable="1"/>')
        ident += 1
        b_plates.append(f'<plate><metadata key="plater_id" value="{newi + 1}"/><metadata key="plater_name" value="{base["nomes"][newi]}"/>'
                        f'<metadata key="locked" value="false"/><model_instance><metadata key="object_id" value="{k}"/>'
                        f'<metadata key="instance_id" value="0"/><metadata key="identify_id" value="{ident}"/></model_instance></plate>')
    root = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n' +
            ''.join(m for m in base['meta']) + f'<metadata name="Title">{cliente} - caneca completa</metadata>\n'
            '<resources>' + ''.join(base['root_objs']) + ''.join(root_objs) + '</resources>\n<build>' +
            ''.join(b_items) + ''.join(build) + '</build>\n</model>\n')
    allf = list(base['arquivos']) + list(model_files)
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
            ''.join(f'<Relationship Target="/{f}" Id="rel-{i + 1}" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
                    for i, f in enumerate(allf)) + '</Relationships>\n')
    ms = '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n' + ''.join(base['ms_objs']) + ''.join(ms_objs) + ''.join(b_plates) + ''.join(plates) + '\n</config>\n'
    return root, rels, ms

POS = {'frente': (0, 0.72), 'lateral_esq': (-90, 0.57), 'lateral_dir': (90, 0.57), 'tras': (180, 0.6)}
def _mockup(spec, P, out, built, R, cb):
    import trimesh
    from shapely import affinity
    import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    mk = spec.get('mockup', {})
    if mk.get('modelo_3mf') and mk.get('objeto_corpo'):
        res, _ = load_3mf(P(mk['modelo_3mf']))
        pl = res[str(mk['objeto_corpo'])]; V = np.vstack([p[1] for p in pl]); off = 0; Fs = []
        for p in pl: Fs.append(p[2] + off); off += len(p[1])
        F = np.vstack(Fs)
        if mk.get('alca_em', '+Y') == '-Y': V = V * [1, -1, 1]; F = F[:, ::-1]
        cyc = mk.get('centro_xy')
        if cyc is None:  # centro do círculo do corpo: ajuste de círculo na metade oposta à alça
            yc = np.median(V[:, 1]); Q = V[V[:, 1] < yc][:, :2]
            Q = Q[np.random.default_rng(0).choice(len(Q), min(len(Q), 40000), replace=False)]
            A = np.c_[2 * Q, np.ones(len(Q))]; bb = (Q ** 2).sum(1)
            cx, cy, _ = np.linalg.lstsq(A, bb, rcond=None)[0]; cyc = [cx, cy]
        V = V - [cyc[0], cyc[1], 0]
        if mk.get('inverter', False): V[:, 2] = V[:, 2].max() - V[:, 2]; F = F[:, ::-1]
        V[:, 2] -= V[:, 2].min(); Hc = V[:, 2].max()
    else:
        cyl = trimesh.creation.cylinder(radius=R, height=95, sections=128); cyl.apply_translation([0, 0, 47.5])
        V, F = cyl.vertices, cyl.faces; Hc = 95
    cor_corpo = mk.get('cor_corpo', spec.get('cor_caneca', '#eeeeee'))
    items = [(V, F, np.array(matplotlib.colors.to_rgb(cor_corpo)))]
    def wrap(geom, ang, hc, z0, t, col):
        m = _extrude(geom, t, z0); v, f = trimesh.remesh.subdivide_to_size(m.vertices, m.faces, max_edge=1.5)
        th = np.radians(ang) + v[:, 0] / R; r = R + 0.4 + v[:, 2]
        return (np.c_[r * np.sin(th), -r * np.cos(th), hc + v[:, 1]], f, np.array(matplotlib.colors.to_rgb(col)))
    for pc, nome, back, layers in built:
        poss = pc.get('posicoes') or ({'laterais': ['lateral_esq', 'lateral_dir']}.get(pc.get('posicao'), [pc.get('posicao', 'frente')]))
        for i, pos in enumerate(poss):
            ang, fr = POS.get(pos, (0, 0.6))
            hc = pc.get('altura_centro', fr * Hc)
            esp = pc.get('espelhar_par') and pos == 'lateral_esq'
            E = (lambda g: affinity.scale(g, -1, 1, origin=(0, 0))) if esp else (lambda g: g)
            items.append(wrap(E(back), ang, hc, 0, 1.0, cb))
            for c, g in layers: items.append(wrap(E(g), ang, hc, 1.0, 1.0, c))
    Lgt = np.array([-0.4, -0.8, 0.5]); Lgt /= np.linalg.norm(Lgt)
    fig, axs = plt.subplots(1, 3, figsize=(18, 6.5))
    for ax, (yaw, t) in zip(axs, [(0, 'Frente (oposta à alça)'), (-90, 'Lateral'), (90, 'Outra lateral')]):
        c, s = np.cos(np.radians(yaw)), np.sin(np.radians(yaw)); Rm = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
        PP = []; CC = []; DD = []
        for V_, F_, col in items:
            T = (V_ @ Rm.T)[F_]; n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]); n /= np.linalg.norm(n, axis=1)[:, None] + 1e-12
            vis = n[:, 1] < 0; T = T[vis]; n = n[vis]
            sh = 0.45 + 0.55 * np.clip(n @ Lgt, 0, 1)
            PP.append(T[:, :, [0, 2]]); CC.append(sh[:, None] * col); DD.append(-T[:, :, 1].mean(1))
        PP = np.concatenate(PP); CC = np.clip(np.concatenate(CC), 0, 1); DD = np.concatenate(DD); o = np.argsort(DD)
        ax.add_collection(PolyCollection(PP[o], facecolors=CC[o], edgecolors=CC[o], linewidths=0.25))
        ax.set_xlim(-80, 80); ax.set_ylim(-5, Hc + 10); ax.set_aspect('equal'); ax.axis('off'); ax.set_title(t)
    plt.suptitle('Mockup aproximado das peças na caneca', y=0.98)
    plt.tight_layout(); plt.savefig(os.path.join(out, 'MOCKUP_caneca.png'), dpi=100); plt.close()

def _leiame(spec, rel, out, cor_base, BT, AT, BICO, VAO):
    L = [f"PEÇAS {rel['cliente']} – CANECA PERSONALIZADA", '']
    for p in rel['pecas']:
        L.append(f"- {p['nome']}  x{p['quantidade']}  ({p['posicao']})  peça {p['tamanho_total_mm'][0]} x {p['tamanho_total_mm'][1]} mm,"
                 f" arte {p['tamanho_arte_mm'][0]} x {p['tamanho_arte_mm'][1]} mm, espessura {p['espessura_mm']} mm ({p['espessura_camadas_mm'][0]} contorno + {p['espessura_camadas_mm'][1]} arte)")
    if spec.get('observacao_dtf'): L += ['', f"DTF (não impresso em 3D): {spec['observacao_dtf']}"]
    L += ['', 'Acabamento: ' + (rel['pecas'][0].get('acabamento', 'nivelado') if rel['pecas'] else '') + ' (nivelado = arte embutida no contorno, superfície lisa; relevo = arte 1 mm acima do contorno).', '',
          'ARQUIVOS POR PEÇA', f'- *_1_BASE_contorno_{cor_base}.stl -> filamento do contorno',
          '- *_2_ARTE_<cor>.stl (e _3_, _4_ se houver mais cores) -> filamento de cada cor da arte',
          '- *_peca_unica_1cor.stl -> mesma peça em um corpo só (1 cor / pintura)', '',
          'PROJETO BAMBU STUDIO (recomendado): PROJETO_<cliente>_caneca.3mf',
          '- Abra pelo Bambu Studio (Arquivo > Abrir projeto). Cada peça está numa placa, já com as cópias necessárias.',
          '- Cada peça é um objeto com partes: contorno (filamento 1) e arte (filamento 2, 3...).',
          '- Para mudar a cor: troque a cor do filamento na lista "Filamentos do Projeto", ou o filamento da parte na lista de objetos.',
          '- O projeto vem com o perfil de impressora do modelo Print4You; se a sua impressora for outra, selecione-a e confirme.', '',
          'STLs SOLTOS (alternativa)', '1. Selecione juntos os arquivos _1_BASE e _N_ARTE da mesma peça e importe.',
          '2. Responda "Sim" para carregar como UM objeto com várias partes (já estão alinhadas).',
          '3. Atribua o filamento de cada parte. Abrir só um STL mostra só aquela camada.', '',
          'PONTOS DE ATENÇÃO', '- Peças planas: moldar na caneca com soprador, como os emblemas originais.',
          f'- Vãos da arte alargados para no mínimo {VAO} mm (bico {BICO}).']
    for p in rel['pecas']:
        if p.get('espelhado'):
            L.append(f"- {p['nome']}: par para os dois lados – arquivo normal = lado DIREITO da caneca (vista de frente),"
                     f" arquivo _ESPELHADO = lado ESQUERDO. No projeto 3MF os dois já estão na mesma placa, com esses nomes.")
    for p in rel['pecas']:
        for x in p['avisos']: L.append(f"- {p['nome']}: {x}")
    for x in rel.get('projeto_3mf', {}).get('avisos', []): L.append(f'- 3MF: {x}')
    open(os.path.join(out, 'LEIA-ME.txt'), 'w').write('\n'.join(L) + '\n')

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest='cmd', required=True)
    s = sp.add_parser('analisar'); s.add_argument('modelo'); s.add_argument('--saida', default='analise_3mf'); s.set_defaults(f=cmd_analisar)
    s = sp.add_parser('logo'); s.add_argument('logo'); s.add_argument('--saida', default='logo'); s.add_argument('--cores', type=int, default=3); s.set_defaults(f=cmd_logo)
    s = sp.add_parser('gerar'); s.add_argument('pedido'); s.set_defaults(f=cmd_gerar)
    a = ap.parse_args(); a.f(a)

if __name__ == '__main__':
    main()
