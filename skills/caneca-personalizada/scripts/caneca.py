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
    els = []
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
        for p in parts(g):
            if p.area > 30: els.append({'geom': p, 'cor': cor})
    return els, ['Logo em bitmap: vetorizado automaticamente. Cantos e curvas podem perder fidelidade – prefira SVG/PDF vetorial.']

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
            x, y = p.exterior.xy; ax.fill(x, y, color=e['cor'] if e['cor'] not in ('#ffffff',) else '#eeeeee', ec='k', lw=0.3)
            for h in p.interiors: x, y = h.xy; ax.fill(x, y, color='white', lw=0)
        c = e['geom'].representative_point()
        ax.text(c.x, c.y, str(e['id']), fontsize=13, weight='bold', color='white', ha='center', va='center',
                bbox=dict(boxstyle='round,pad=0.15', fc='#c00000', ec='none', alpha=.85))
    ax.set_aspect('equal'); ax.axis('off'); ax.set_title('Elementos do logo (número = id para o pedido.json)')
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
    _, _, _, unary_union, _ = _shp()
    narrow = g.buffer(mingap / 2, join_style=1).buffer(-mingap / 2, join_style=1).difference(g)
    narrow = unary_union([p for p in parts(narrow) if p.area > 0.02])
    return clean_geom(g.difference(narrow.buffer(0.15)))

def _backing(g, borda, fechar):
    Polygon, _, _, unary_union, _ = _shp()
    for r in [fechar, fechar * 1.5, fechar * 2, fechar * 3]:
        b = g.buffer(r, join_style=1).buffer(-r + borda, join_style=1).union(g.buffer(borda))
        b = unary_union([Polygon(p.exterior) for p in parts(b)])
        if len(parts(b)) == 1: return b, r
    return b, r

def _extrude(g, h, z):
    import manifold3d as mf, trimesh
    g = clean_geom(g.simplify(0.01))
    rings = []
    for p in parts(g):
        rings.append(np.array(p.exterior.coords[:-1]))
        rings += [np.array(i.coords[:-1]) for i in p.interiors]
    M = mf.CrossSection(rings, mf.FillRule.EvenOdd).extrude(h).translate((0, 0, z)).to_mesh()
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
    relatorio = {'cliente': cliente, 'pecas': []}; built = []
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
        layers = [(c, clean_geom(tf(g))) for c, g in layers]
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
        mb = _extrude(back, BASE_T, 0); fb = f'{nome}_1_BASE_contorno_{cor_base}.stl'; mb.export(os.path.join(out, fb)); files.append(fb)
        tops = []
        for i, (c, g) in enumerate(layers):
            mt = _extrude(g, ART_T, BASE_T); tops.append(mt)
            ft = f'{nome}_{i + 2}_ARTE_{_nome_cor(c)}.stl'; mt.export(os.path.join(out, ft)); files.append(ft)
        fu = f'{nome}_peca_unica_1cor.stl'; trimesh.util.concatenate([mb] + tops).export(os.path.join(out, fu)); files.append(fu)
        info = {'nome': nome, 'quantidade': pc.get('quantidade', 1), 'posicao': pc.get('posicao', ''),
                'tamanho_total_mm': [round(BW, 1), round(BH, 1)], 'tamanho_arte_mm': [round(w * s, 1), round(h * s, 1)],
                'espessura_mm': BASE_T + ART_T, 'cores_arte': [c for c, _ in layers], 'arquivos': files, 'avisos': avisos,
                'flecha_na_caneca_mm': round(flecha, 1)}
        relatorio['pecas'].append(info); built.append((pc, nome, back, layers))
        print(f"  {nome}: {BW:.1f} × {BH:.1f} mm  (arte {w * s:.1f} × {h * s:.1f})  x{info['quantidade']}  cores {[ _nome_cor(c) for c in info['cores_arte']]}")
        for x in avisos: print('     AVISO:', x)
    # prévia
    n = len(built)
    fig, axs = plt.subplots(1, n, figsize=(6.5 * n, 6)); axs = np.atleast_1d(axs)
    cb = {'preto': '#1a1a1a', 'branco': '#f4f4f4'}.get(cor_base, cor_base if cor_base.startswith('#') else '#1a1a1a')
    for ax, (pc, nome, back, layers) in zip(axs, built):
        for p in parts(back):
            x, y = p.exterior.xy; ax.fill(x, y, color=cb, ec='#888', lw=0.5)
        for c, g in layers:
            for p in parts(g):
                x, y = p.exterior.xy; ax.fill(x, y, color=c, lw=0)
                for hh in p.interiors: x, y = hh.xy; ax.fill(x, y, color=cb, lw=0)
            for p in _thin(g, BICO):
                x, y = p.exterior.xy; ax.plot(x, y, 'r', lw=1.2)
        bx0, by0, bx1, by1 = back.bounds
        ax.set_title(f'{nome}  ×{pc.get("quantidade", 1)}\n{bx1 - bx0:.1f} × {by1 - by0:.1f} mm (vermelho = traço < {BICO} mm)')
        ax.set_aspect('equal'); ax.grid(alpha=.25)
    plt.tight_layout(); plt.savefig(os.path.join(out, 'PREVIEW_pecas.png'), dpi=110); plt.close()
    # mockup
    try:
        _mockup(spec, P, out, built, R, cb)
    except Exception as ex:
        print('mockup falhou:', ex)
    # LEIA-ME + zip
    _leiame(spec, relatorio, out, cor_base, BASE_T, ART_T, BICO, VAO)
    json.dump(relatorio, open(os.path.join(out, 'relatorio.json'), 'w'), ensure_ascii=False, indent=1)
    zp = os.path.join(os.path.dirname(out.rstrip('/')), f'{cliente}_caneca_STL.zip')
    with zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(out)):
            if f.endswith(('.stl', '.png', '.txt')): z.write(os.path.join(out, f), f)
    print('Saída:', out); print('Zip:', zp)

POS = {'frente': (0, 0.72), 'lateral_esq': (-90, 0.57), 'lateral_dir': (90, 0.57), 'tras': (180, 0.6)}
def _mockup(spec, P, out, built, R, cb):
    import trimesh
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
    items = [(V, F, np.array([0.93, 0.93, 0.93]))]
    def wrap(geom, ang, hc, z0, t, col):
        m = _extrude(geom, t, z0); v, f = trimesh.remesh.subdivide_to_size(m.vertices, m.faces, max_edge=1.5)
        th = np.radians(ang) + v[:, 0] / R; r = R + 0.4 + v[:, 2]
        return (np.c_[r * np.sin(th), -r * np.cos(th), hc + v[:, 1]], f, np.array(matplotlib.colors.to_rgb(col)))
    for pc, nome, back, layers in built:
        poss = pc.get('posicoes') or ({'laterais': ['lateral_esq', 'lateral_dir']}.get(pc.get('posicao'), [pc.get('posicao', 'frente')]))
        for i, pos in enumerate(poss):
            ang, fr = POS.get(pos, (0, 0.6))
            hc = pc.get('altura_centro', fr * Hc)
            items.append(wrap(back, ang, hc, 0, 1.0, cb))
            for c, g in layers: items.append(wrap(g, ang, hc, 1.0, 1.0, c))
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
        ax.add_collection(PolyCollection(PP[o], facecolors=CC[o], edgecolors='none'))
        ax.set_xlim(-80, 80); ax.set_ylim(-5, Hc + 10); ax.set_aspect('equal'); ax.axis('off'); ax.set_title(t)
    plt.suptitle('Mockup aproximado das peças na caneca', y=0.98)
    plt.tight_layout(); plt.savefig(os.path.join(out, 'MOCKUP_caneca.png'), dpi=100); plt.close()

def _leiame(spec, rel, out, cor_base, BT, AT, BICO, VAO):
    L = [f"PEÇAS {rel['cliente']} – CANECA PERSONALIZADA", '']
    for p in rel['pecas']:
        L.append(f"- {p['nome']}  x{p['quantidade']}  ({p['posicao']})  peça {p['tamanho_total_mm'][0]} x {p['tamanho_total_mm'][1]} mm,"
                 f" arte {p['tamanho_arte_mm'][0]} x {p['tamanho_arte_mm'][1]} mm")
    if spec.get('observacao_dtf'): L += ['', f"DTF (não impresso em 3D): {spec['observacao_dtf']}"]
    L += ['', f'ESPESSURA: {BT} mm contorno + {AT} mm arte = {BT + AT} mm.', '',
          'ARQUIVOS POR PEÇA', f'- *_1_BASE_contorno_{cor_base}.stl -> filamento do contorno',
          '- *_2_ARTE_<cor>.stl (e _3_, _4_ se houver mais cores) -> filamento de cada cor da arte',
          '- *_peca_unica_1cor.stl -> mesma peça em um corpo só (1 cor / pintura)', '',
          'BAMBU STUDIO (multicor)', '1. Selecione juntos os arquivos _1_BASE e _N_ARTE da mesma peça e importe.',
          '2. Responda "Sim" para carregar como UM objeto com várias partes (já estão alinhadas).',
          '3. Atribua o filamento de cada parte. Imprima a quantidade indicada de cada peça.', '',
          'PONTOS DE ATENÇÃO', '- Peças planas: moldar na caneca com soprador, como os emblemas originais.',
          f'- Vãos da arte alargados para no mínimo {VAO} mm (bico {BICO}).']
    for p in rel['pecas']:
        for x in p['avisos']: L.append(f"- {p['nome']}: {x}")
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
