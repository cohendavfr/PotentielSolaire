"""
Cœur du calcul solaire : numpy + shapely uniquement (aucun réseau, aucun fichier).

Ce module tourne à l'identique dans le pipeline par commune (solaire.py) et dans le navigateur
(page « adresse », via Pyodide). Il prend un modèle numérique de surface (tableau + géoréférencement)
et l'emprise d'un bâtiment, et renvoie ses pans de toit : géométrie 3D, pente, orientation, ombrage
des arbres et du bâti, rendement et production.
"""
import math
from collections import namedtuple

import numpy as np
import shapely
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

# --------------------------------------------------------------------------- constantes
FLAT_SLOPE = 8.0       # en dessous : toit considéré plat (°)
MAX_SLOPE = 60.0       # au dessus : façade / bruit
ERODE = 0.6            # recul par rapport à l'emprise pour éviter murs et débords (m)
MIN_PAN_M2 = 6.0       # pan minimal retenu (surface réelle)
MIN_FLAT_M2 = 8.0      # idem pour un pan de toit plat
USABLE_RATIO = 0.85    # part d'un pan incliné réellement équipable (rives, cheminées, velux)
FLAT_USABLE = 0.85     # idem toit plat
FLAT_TILT = 15.0       # inclinaison supposée des structures sur toit plat
M2_PER_KWP = 5.0       # surface de toit par kWc (modules + espacement)
MODULE_KWP = 0.43      # puissance d'un module standard (kWc) : 430 Wc, environ 1,13 x 1,72 m
PAN_BIN = 20           # largeur des classes d'azimut pour regrouper les pans (°)
GOOD_RATIO = 0.85      # un pan est "bon" s'il fait au moins 85 % du rendement optimal
PLANE_TOL = 0.35       # écart maximal d'un pixel à son plan de toit (m)
WIN_SMALL = 7.0        # marge de la fenêtre « toit » (m) : sol alentour compris
HORIZON_RADIUS = 150.0  # distance de recherche des obstacles (m)
HORIZON_AZ = np.arange(0, 360, 5)  # azimuts du profil d'horizon (°, 0 = nord)
# Part du rayonnement direct qui traverse un obstacle non bâti (arbre), par mois.
# Hors feuillage (nov-mars) les branches laissent passer plus de soleil.
TREE_T_DIRECT = np.array([.55, .55, .50, .35, .15, .15, .15, .15, .15, .35, .55, .55])
TREE_T_DIFFUSE = 0.40   # idem pour le rayonnement diffus, moyenne annuelle
MID_MONTH_DOY = [15, 46, 74, 105, 135, 166, 196, 227, 258, 288, 319, 349]
TILTS = list(range(0, 91, 10))
AZS = list(range(0, 361, 30))  # azimut boussole : 0 = nord, 180 = sud

# géoréférencement d'un tableau : x = c + a * (col + 0.5), y = f + e * (row + 0.5), e < 0
Tf = namedtuple("Tf", "c a f e")


# --------------------------------------------------------------------------- projection
def lambert93(lon, lat):
    """WGS84 (degrés) -> Lambert-93 (m)."""
    n, C, e, xs, ys = 0.7256077650532670, 11754255.426096, 0.08181919104281579, 700000.0, 12655612.049876
    phi, lam = math.radians(lat), math.radians(lon)
    L = 0.5 * math.log((1 + math.sin(phi)) / (1 - math.sin(phi))) \
        - e / 2 * math.log((1 + e * math.sin(phi)) / (1 - e * math.sin(phi)))
    R = C * math.exp(-n * L)
    g = n * (lam - math.radians(3.0))
    return xs + R * math.sin(g), ys - R * math.cos(g)


# --------------------------------------------------------------------------- rendement
class YieldModel:
    """Rendement annuel (kWh/kWc) en fonction de (inclinaison, azimut boussole), interpolé."""

    def __init__(self, grid):
        g = np.asarray(grid, float)
        if g.shape[1] == len(AZS) - 1:  # colonne 360° = 0°
            g = np.hstack([g, g[:, :1]])
        self.grid = g
        self.ymax = float(g.max())

    def __call__(self, tilt, az):
        t = min(max(tilt, 0.0), 90.0) / 10.0
        i0 = min(int(t), len(TILTS) - 2)
        ft = t - i0
        a = (az % 360.0) / 30.0
        j0 = min(int(a), len(AZS) - 2)
        fa = a - j0
        g = self.grid
        return float((1 - ft) * ((1 - fa) * g[i0, j0] + fa * g[i0, j0 + 1])
                     + ft * ((1 - fa) * g[i0 + 1, j0] + fa * g[i0 + 1, j0 + 1]))


def _fine_ref(data, lat, lon):
    """Rendement du plan de référence (grille fine, 0,1°) interpolé sur les points terrestres voisins."""
    f = data.get("fine")
    if not f:
        return None
    g, step = f["grid"], f["step"]
    fi, fj = (lat - data["lat0"]) / step, (lon - data["lon0"]) / step
    i0, j0 = math.floor(fi), math.floor(fj)
    acc = wsum = 0.0
    for di in (0, 1):
        for dj in (0, 1):
            ii, jj = i0 + di, j0 + dj
            if 0 <= ii < len(g) and 0 <= jj < len(g[0]) and g[ii][jj] > 0:
                w = (1 - abs(fi - ii)) * (1 - abs(fj - jj))
                acc += w * g[ii][jj]
                wsum += w
    return acc / wsum if wsum > 0 else None


def france_model(data, lat, lon):
    """(YieldModel, H, Kd) interpolés dans la grille PVGIS précalculée de la France.

    La grille grossière (0,5°) donne la forme du rendement selon l'inclinaison et l'azimut ; la grille
    fine (0,1°), quand elle existe, recale le niveau (côtes, relief).
    """
    lat0, lon0, step = data["lat0"], data["lon0"], data["step"]
    fi, fj = (lat - lat0) / step, (lon - lon0) / step
    i0, j0 = math.floor(fi), math.floor(fj)
    acc_g = acc_h = acc_k = 0.0
    wsum = 0.0
    for di in (0, 1):
        for dj in (0, 1):
            nd = data["nodes"].get(f"{i0 + di},{j0 + dj}")
            if nd is None:
                continue
            w = (1 - abs(fi - (i0 + di))) * (1 - abs(fj - (j0 + dj)))
            if w <= 0:
                continue
            acc_g = acc_g + w * np.array(nd["g"], float)
            acc_h = acc_h + w * np.array(nd["H"], float)
            acc_k = acc_k + w * np.array(nd["Kd"], float)
            wsum += w
    if wsum == 0:  # hors de la grille : noeud le plus proche
        key = min(data["nodes"], key=lambda k: (int(k.split(",")[0]) - fi) ** 2 + (int(k.split(",")[1]) - fj) ** 2)
        nd = data["nodes"][key]
        grid, H, Kd = np.array(nd["g"], float), np.array(nd["H"], float), np.array(nd["Kd"], float)
    else:
        grid, H, Kd = acc_g / wsum, acc_h / wsum, acc_k / wsum
    ref = _fine_ref(data, lat, lon)
    if ref:
        rt, ra = data["fine"]["ref"]
        coarse_ref = float(grid[TILTS.index(rt), AZS.index(ra)]) if grid.shape[1] > AZS.index(ra) else 0.0
        if coarse_ref > 0:
            grid = grid * min(max(ref / coarse_ref, 0.75), 1.3)
    return YieldModel(grid), H, Kd


# --------------------------------------------------------------------------- soleil et ombrage
def solar_position(lat, lon, doy, utc_hour):
    """Azimut (0 = nord, horaire) et hauteur du soleil en radians (formules NOAA)."""
    g = 2 * math.pi / 365 * (doy - 1 + (utc_hour - 12) / 24)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    ha = math.radians((utc_hour * 60 + eqt + 4 * lon) / 4 - 180)
    la = math.radians(lat)
    cz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    el = math.asin(max(-1.0, min(1.0, cz)))
    az = math.atan2(math.sin(ha), math.cos(ha) * math.sin(la) - math.tan(decl) * math.cos(la)) + math.pi
    return az % (2 * math.pi), el


def make_sun_context(lat, lon, H, Kd, step_h=0.25):
    """Échantillons du soleil (12 jours types) pondérés par le rayonnement direct mensuel."""
    az, el, mo, w = [], [], [], []
    for m, doy in enumerate(MID_MONTH_DOY):
        pts = []
        for h in np.arange(0, 24, step_h):
            a, e = solar_position(lat, lon, doy, float(h))
            if e > math.radians(3):
                air = 1.0 / (math.sin(e) + 0.50572 * (math.degrees(e) + 6.07995) ** -1.6364)
                dni = 1353.0 * 0.7 ** (air ** 0.678)           # ciel clair (Meinel)
                pts.append((a, e, dni * step_h))
        if not pts:
            continue
        horiz = sum(d * math.sin(e) for _, e, d in pts)        # énergie sur le plan horizontal
        scale = H[m] * (1 - Kd[m]) / horiz                      # recalage sur le direct du mois
        for a, e, d in pts:
            az.append(a); el.append(e); mo.append(m); w.append(d * scale)
    sky_el = np.arange(1.5, 90, 3.0)
    return dict(
        az=np.array(az), el=np.array(el), month=np.array(mo), w=np.array(w),
        diffuse=float((np.asarray(H) * np.asarray(Kd)).sum()),
        sky_az=np.radians(HORIZON_AZ), sky_el=np.radians(sky_el), sky_el_deg=sky_el,
    )


def horizon_profile(Z, T, bmask, own, x, y, z0):
    """Hauteur angulaire (°) du plus haut obstacle par azimut : bâtiments (Hb) et autres (Hv)."""
    azs = np.radians(HORIZON_AZ)
    d = np.concatenate([np.arange(2.0, 40.0, 1.0), np.arange(40.0, HORIZON_RADIUS + 1, 2.5)])
    A, D = np.meshgrid(azs, d)
    X, Y = x + D * np.sin(A), y + D * np.cos(A)
    cols = np.floor((X - T.c) / T.a).astype(int)
    rows = np.floor((Y - T.f) / T.e).astype(int)
    ok = (rows >= 0) & (rows < Z.shape[0]) & (cols >= 0) & (cols < Z.shape[1])
    rows, cols = np.clip(rows, 0, Z.shape[0] - 1), np.clip(cols, 0, Z.shape[1] - 1)
    zz = np.where(ok & ~own[rows, cols], Z[rows, cols], np.nan)
    ang = np.degrees(np.arctan2(zz - z0, D))
    isb = bmask[rows, cols]
    Hb = np.where(np.isfinite(ang) & isb, ang, 0.0).max(axis=0)
    Hv = np.where(np.isfinite(ang) & ~isb, ang, 0.0).max(axis=0)
    return np.clip(Hb, 0, 90), np.clip(Hv, 0, 90)


def _circ(vals, az_deg):
    return np.interp(az_deg, np.append(HORIZON_AZ, 360), np.append(vals, vals[0]))


def shade_losses(Hb, Hv, tilt, az, sun):
    """(perte totale, perte due au seul bâti) en fraction de la production annuelle du pan."""
    bt, a = math.radians(tilt), math.radians(az)
    cosinc = math.cos(bt) * np.sin(sun["el"]) + math.sin(bt) * np.cos(sun["el"]) * np.cos(sun["az"] - a)
    wd = sun["w"] * np.clip(cosinc, 0, None)
    saz = np.degrees(sun["az"])
    el = np.degrees(sun["el"])
    hb, hv = _circ(Hb, saz), _circ(Hv, saz)
    tm = TREE_T_DIRECT[sun["month"]]
    t_tot = np.where(el < hb, 0.0, np.where(el < hv, tm, 1.0))
    t_bat = np.where(el < hb, 0.0, 1.0)
    # rayonnement diffus : isotrope, réduit selon la part de ciel masquée vue par le pan
    EL = sun["sky_el"][None, :]
    AZ = sun["sky_az"][:, None]
    g = np.clip(math.cos(bt) * np.sin(EL) + math.sin(bt) * np.cos(EL) * np.cos(AZ - a), 0, None) * np.cos(EL)
    elg = sun["sky_el_deg"][None, :]
    d_tot = np.where(elg < Hb[:, None], 0.0, np.where(elg < Hv[:, None], TREE_T_DIFFUSE, 1.0))
    d_bat = np.where(elg < Hb[:, None], 0.0, 1.0)
    gs = g.sum()
    dif_tot = float((g * d_tot).sum() / gs) if gs > 0 else 1.0
    dif_bat = float((g * d_bat).sum() / gs) if gs > 0 else 1.0
    D = sun["diffuse"] * (1 + math.cos(bt)) / 2
    den = wd.sum() + D
    if den <= 0:
        return 0.0, 0.0
    return (float(1 - ((wd * t_tot).sum() + D * dif_tot) / den),
            float(1 - ((wd * t_bat).sum() + D * dif_bat) / den))


# --------------------------------------------------------------------------- raster
def crop_window(Z, T, bounds):
    """Sous-tableau de Z couvrant `bounds` (minx, miny, maxx, maxy), avec son géoréférencement."""
    minx, miny, maxx, maxy = bounds
    c0 = int(math.floor((minx - T.c) / T.a))
    c1 = int(math.ceil((maxx - T.c) / T.a))
    r0 = int(math.floor((maxy - T.f) / T.e))
    r1 = int(math.ceil((miny - T.f) / T.e))
    out = np.full((max(r1 - r0, 1), max(c1 - c0, 1)), np.nan)
    rs, re_ = max(r0, 0), min(r1, Z.shape[0])
    cs, ce = max(c0, 0), min(c1, Z.shape[1])
    if re_ > rs and ce > cs:
        out[rs - r0:re_ - r0, cs - c0:ce - c0] = Z[rs:re_, cs:ce]
    return out, Tf(T.c + c0 * T.a, T.a, T.f + r0 * T.e, T.e)


def rasterize(geoms, shape, T):
    """Masque des pixels dont le centre tombe dans l'une des géométries."""
    mask = np.zeros(shape, bool)
    for g in geoms:
        if g.is_empty:
            continue
        minx, miny, maxx, maxy = g.bounds
        c0 = max(0, int(math.floor((minx - T.c) / T.a)))
        c1 = min(shape[1], int(math.ceil((maxx - T.c) / T.a)) + 1)
        r0 = max(0, int(math.floor((maxy - T.f) / T.e)))
        r1 = min(shape[0], int(math.ceil((miny - T.f) / T.e)) + 1)
        if c1 <= c0 or r1 <= r0:
            continue
        X = T.c + (np.arange(c0, c1) + 0.5) * T.a
        Y = T.f + (np.arange(r0, r1) + 0.5) * T.e
        XX, YY = np.meshgrid(X, Y)
        mask[r0:r1, c0:c1] |= shapely.contains_xy(g, XX, YY)
    return mask


def slope_aspect(Z, res):
    """Pente (°) et azimut de la ligne de plus grande pente descendante (0 = nord, horaire)."""
    drow, dcol = np.gradient(Z, res)
    dz_e, dz_n = dcol, -drow
    slope = np.degrees(np.arctan(np.hypot(dz_e, dz_n)))
    az = np.degrees(np.arctan2(-dz_e, -dz_n)) % 360
    return slope, az


_SHIFTS = [(dr, dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if (dr, dc) != (0, 0)]


def _shift(a, dr, dc, fill):
    out = np.full_like(a, fill)
    h, w = a.shape
    out[max(dr, 0):h + min(dr, 0), max(dc, 0):w + min(dc, 0)] = \
        a[max(-dr, 0):h + min(-dr, 0), max(-dc, 0):w + min(-dc, 0)]
    return out


def dilate(mask, n):
    """Dilatation d'un masque de n pixels (voisinage carré) — sans GEOS : certaines opérations
    buffer() de GEOS plantent définitivement le runtime Pyodide sur des emprises dégénérées."""
    for _ in range(n):
        out = mask.copy()
        for dr, dc in _SHIFTS:
            out |= _shift(mask, dr, dc, False)
        mask = out
    return mask


def erode(mask, n):
    return ~dilate(~mask, n)


def label8(mask):
    """Composantes connexes (8 voisins) : tableau d'entiers (-1 = fond) et nombre de composantes."""
    lab = np.where(mask, np.arange(mask.size).reshape(mask.shape), -1)
    while True:
        best = lab.copy()
        for dr, dc in _SHIFTS:
            nb = _shift(lab, dr, dc, -1)
            best = np.where(mask & (nb >= 0) & ((best < 0) | (nb < best)), nb, best)
        if np.array_equal(best, lab):
            break
        lab = best
    ids = np.unique(lab[lab >= 0])
    remap = {int(v): k for k, v in enumerate(ids)}
    out = np.full(mask.shape, -1, int)
    for v, k in remap.items():
        out[lab == v] = k
    return out, len(ids)


def nearest_fill(lab, region, max_iter=10**6):
    """Étend les étiquettes (>= 0) aux pixels de `region` sans étiquette, par dilatation."""
    lab = lab.copy()
    for _ in range(max_iter):
        miss = region & (lab < 0)
        if not miss.any():
            break
        cand = np.full(lab.shape, -1)
        for dr, dc in _SHIFTS:
            cand = np.maximum(cand, _shift(lab, dr, dc, -1))
        new = np.where(miss & (cand >= 0), cand, lab)
        if np.array_equal(new, lab):
            break
        lab = new
    return lab


# --------------------------------------------------------------------------- pans
def _group_ids(slope, az, area_px):
    """Identifiant de groupe par pixel : classes d'azimut, fusion des classes voisines."""
    nb = 360 // PAN_BIN
    idx = (np.floor((az + PAN_BIN / 2) / PAN_BIN) % nb).astype(int)
    bins = {}
    for k in range(nb):
        m = idx == k
        if m.any():
            bins[k] = (float(area_px[m].sum()), float((slope[m] * area_px[m]).sum()))
    parent = list(range(nb))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for k in bins:
        j = (k + 1) % nb
        if j in bins and bins[k][0] >= 1.5 and bins[j][0] >= 1.5:
            if abs(bins[k][1] / bins[k][0] - bins[j][1] / bins[j][0]) <= 6:
                parent[find(k)] = find(j)
    root = np.array([find(k) for k in range(nb)])
    return root[idx]


def _fit_plane(x, y, z, xr, yr):
    """Plan z = c + a (x - xr) + b (y - yr) par moindres carrés, avec un tour de rejet des aberrants."""
    A = np.column_stack([x - xr, y - yr, np.ones_like(x)])
    keep = np.ones(len(z), bool)
    coef = None
    for _ in range(2):
        if keep.sum() < 4:
            break
        coef = np.linalg.lstsq(A[keep], z[keep], rcond=None)[0]
        resid = np.abs(A @ coef - z)
        keep = resid < max(0.4, 2.0 * float(np.median(resid)))
    if coef is None:
        coef = np.array([0.0, 0.0, float(np.mean(z))])
    return float(coef[0]), float(coef[1]), float(coef[2])


def _split_facets(m, XX, YY, Zs, area_px, xr, yr):
    """Découpe une composante en facettes planes : ajuste un plan, garde les pixels qui s'y collent
    (écart < 0,35 m) par morceaux connexes, puis recommence sur le reste (4 passes au plus)."""
    out = []
    rest = m.copy()
    for _ in range(4):
        if float(area_px[rest].sum()) < MIN_PAN_M2:
            break
        a, b, c = _fit_plane(XX[rest], YY[rest], Zs[rest], xr, yr)
        resid = np.abs(c + a * (XX - xr) + b * (YY - yr) - Zs)
        inl = rest & (resid < PLANE_TOL)
        lab, n = label8(inl)
        took = np.zeros_like(rest)
        for k in range(n):
            part = lab == k
            if float(area_px[part].sum()) >= MIN_PAN_M2:
                out.append(part)
                took |= part
        if not took.any():
            break
        rest &= ~took
    return out


def _pixel_polys(lab_id_mask, T, res):
    """Union des pixels (carrés) d'un masque -> géométrie shapely."""
    rr, cc = np.nonzero(lab_id_mask)
    if len(rr) == 0:
        return None
    x0 = T.c + cc * T.a
    y1 = T.f + rr * T.e
    boxes = shapely.box(x0, y1 + T.e, x0 + T.a, y1)  # e < 0 : y1 + e est le bas
    return shapely.union_all(boxes)


def _rings(geom):
    polys = [geom] if geom.geom_type == "Polygon" else [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
    out = []
    for p in polys:
        if p.area < 0.5:
            continue
        p = shapely.geometry.polygon.orient(p, 1.0)  # extérieur antihoraire
        out.append([[(round(x, 2), round(y, 2)) for x, y in p.exterior.coords[:-1]],
                    [[(round(x, 2), round(y, 2)) for x, y in h.coords[:-1]] for h in p.interiors]])
    return out


def valid_polygon(g):
    """Polygone valide (les emprises BD TOPO sont parfois auto-intersectées) ; le plus grand morceau si besoin."""
    if g.is_valid and g.geom_type == "Polygon":
        return g
    g = shapely.make_valid(g) if not g.is_valid else g
    polys = [g] if g.geom_type == "Polygon" else [x for x in getattr(g, "geoms", []) if x.geom_type in ("Polygon", "MultiPolygon")]
    flat = []
    for x in polys:
        flat += [x] if x.geom_type == "Polygon" else list(x.geoms)
    return max(flat, key=lambda x: x.area) if flat else g


def analyze_roof(geom, Zs, Ts):
    """Pans géométriques d'un bâtiment sur le MNS : [dict(...)] et altitude du sol alentour.

    Chaque pan : composante connexe de toit de même orientation (classes de 20°), avec surface,
    pente et azimut (plan ajusté), centre, altitude moyenne, plan 3D et polygones.
    """
    geom = valid_polygon(geom)
    res = abs(Ts.a)
    ref = geom.representative_point()
    xr, yr = ref.x, ref.y
    fp0 = rasterize([geom], Zs.shape, Ts)
    mask = erode(fp0, max(1, round(ERODE / res)))     # recul du pourtour (murs, débords)
    if mask.sum() < 10 or np.isfinite(Zs[mask]).mean() < 0.7:
        return [], None, (xr, yr), []
    # altitude du sol : bas de la couronne autour du bâtiment, hors bâtiment
    ring = dilate(fp0, round((WIN_SMALL - 1.5) / res)) & ~dilate(fp0, round(1.0 / res))
    zr = Zs[ring & np.isfinite(Zs)]
    zg = float(np.percentile(zr, 10)) if len(zr) > 20 else float(np.nanmin(Zs[mask]) - 3.0)

    slope, az = slope_aspect(Zs, res)
    ok = np.isfinite(slope)
    pitched = mask & ok & (slope >= FLAT_SLOPE) & (slope <= MAX_SLOPE)
    flat = mask & ok & (slope < FLAT_SLOPE)
    area_px = res * res / np.cos(np.radians(np.where(ok, slope, 0.0)))
    flat_area = float(flat.sum() * res * res)
    pitched_area = float(area_px[pitched].sum())
    use_flat = flat_area >= 15 and flat_area > 0.5 * pitched_area

    Xc = Ts.c + (np.arange(Zs.shape[1]) + 0.5) * Ts.a
    Yc = Ts.f + (np.arange(Zs.shape[0]) + 0.5) * Ts.e
    XX, YY = np.meshgrid(Xc, Yc)

    cand = []   # (masque, is_flat)
    if pitched.any():
        gid = np.full(Zs.shape, -1)
        gid[pitched] = _group_ids(slope[pitched], az[pitched], area_px[pitched])
        for g in np.unique(gid[pitched]):
            lab, n = label8(gid == g)
            for k in range(n):
                m = lab == k
                if float(area_px[m].sum()) >= MIN_PAN_M2:
                    for f in _split_facets(m, XX, YY, Zs, area_px, xr, yr):
                        cand.append((f, False))
    if use_flat:
        lab, n = label8(flat)
        for k in range(n):
            m = lab == k
            if float(m.sum() * res * res) >= MIN_FLAT_M2:
                cand.append((m, True))
    if not cand:
        return [], zg, (xr, yr), []

    # étiquettes finales : les pans retenus s'étendent à toute l'emprise (sans trou ni recul)
    seeds = np.full(Zs.shape, -1)
    for i, (m, _) in enumerate(cand):
        seeds[m] = i
    footprint = dilate(fp0, 1)   # un peu plus large : le découpage final se fait sur l'emprise exacte
    full = nearest_fill(seeds, footprint, max_iter=8)   # au plus 4 m autour des pixels retenus
    zcap = round(float(max(np.nanmax(Zs[m]) for m, _ in cand)) - zg + 1.0, 2)   # plafond : le plus haut point du toit + 1 m
    pans = []
    for i, (m, is_flat) in enumerate(cand):
        w = area_px[m]
        x, y, z = XX[m], YY[m], Zs[m]
        sin_a = float((np.sin(np.radians(az[m])) * w).sum())
        cos_a = float((np.cos(np.radians(az[m])) * w).sum())
        tilt = float((slope[m] * w).sum() / w.sum())
        azi = math.degrees(math.atan2(sin_a, cos_a)) % 360
        a, b, c = _fit_plane(x, y, z, xr, yr)
        if is_flat:
            a = b = 0.0
            c = float(np.median(z) - 0.0)
            tilt, azi = FLAT_TILT, 180.0
        elif m.sum() >= 12 and math.hypot(a, b) > 0.02:   # plan ajusté : moins sensible au bruit du MNS
            pt = math.degrees(math.atan(math.hypot(a, b)))
            if FLAT_SLOPE <= pt <= MAX_SLOPE:
                tilt = pt
                azi = math.degrees(math.atan2(-a, -b)) % 360
        geo = _pixel_polys(full == i, Ts, res)
        polys = []
        if geo is not None:
            try:
                g2 = geo.intersection(geom).simplify(0.35)
            except Exception:
                g2 = shapely.make_valid(geo).intersection(shapely.make_valid(geom)).simplify(0.35)
            polys = _rings(g2)
        if not polys:
            continue
        pans.append(dict(
            area=float(w.sum()), tilt=tilt, az=azi, flat=is_flat,
            usable=FLAT_USABLE if is_flat else USABLE_RATIO,
            x=float(x.mean()), y=float(y.mean()), z=float(z.mean()),
            plane=(round(a, 4), round(b, 4), round(c - zg, 3)), polys=polys,
            zr=(0.0, zcap),
        ))
    # zones de toit non retenues comme pans (cheminées, lucarnes, arbres...) : géométrie seule
    extras = []
    leftover = footprint & (full < 0)
    lab, n = label8(leftover)
    for k in range(n):
        m = lab == k
        if m.sum() * res * res < 2.0:
            continue
        zz = Zs[m & np.isfinite(Zs)]
        if len(zz) >= 6:
            a, b, c = _fit_plane(XX[m & np.isfinite(Zs)], YY[m & np.isfinite(Zs)], zz, xr, yr)
            if math.hypot(a, b) > 1.5:   # plan aberrant : on aplatit
                a = b = 0.0
                c = float(np.median(zz))
        elif len(zz):
            a = b = 0.0
            c = float(np.median(zz))
        else:
            continue
        geo = _pixel_polys(m, Ts, res)
        try:
            polys = _rings(geo.intersection(geom).simplify(0.35)) if geo is not None else []
        except Exception:
            polys = []
        if polys:
            extras.append(dict(plane=(round(a, 4), round(b, 4), round(c - zg, 3)), polys=polys, zr=(0.0, zcap)))
    return pans, zg, (xr, yr), extras


def analyze_building(geom, Zb, Tb, nearby, yield_model, sun=None, shade=True):
    """Analyse complète d'un bâtiment.

    Zb, Tb : MNS autour du bâtiment, d'au moins HORIZON_RADIUS + 5 m si l'ombrage est demandé.
    nearby : géométries des bâtiments voisins (obstacles « bâti »). Renvoie dict ou None.
    """
    geom = valid_polygon(geom)
    nearby = [valid_polygon(g) for g in nearby]
    gx0, gy0, gx1, gy1 = geom.bounds
    Zs, Ts = crop_window(Zb, Tb, (gx0 - WIN_SMALL, gy0 - WIN_SMALL, gx1 + WIN_SMALL, gy1 + WIN_SMALL))
    pans, zg, ref, extras = analyze_roof(geom, Zs, Ts)
    if not pans:
        return None
    if shade and sun is not None:
        res_b = abs(Tb.a)
        bmask = dilate(rasterize(nearby, Zb.shape, Tb), max(1, round(0.8 / res_b)))
        own = dilate(rasterize([geom], Zb.shape, Tb), max(1, round(1.0 / res_b)))
    ymax = yield_model.ymax
    for p in pans:
        loss = loss_b = 0.0
        if shade and sun is not None:
            Hb, Hv = horizon_profile(Zb, Tb, bmask, own, p["x"], p["y"], p["z"])
            loss, loss_b = shade_losses(Hb, Hv, min(max(p["tilt"], 0), 90), p["az"], sun)
        y = yield_model(p["tilt"], p["az"])
        y_net = y * (1 - loss)
        n_mod = int(p["area"] * p["usable"] / M2_PER_KWP / MODULE_KWP + 1e-9)   # modules entiers
        kwp = n_mod * MODULE_KWP
        p.update(
            panneaux=n_mod,
            area_m2=round(p["area"], 1), shade_loss=round(loss, 3), shade_bati=round(loss_b, 3),
            shade_arbres=round(max(loss - loss_b, 0.0), 3),
            yield_kwh_kwp=round(y_net, 0), ratio=round(y_net / ymax, 3),
            kwp=round(kwp, 2), kwh_an=round(kwp * y_net, 0),
            az=round(p["az"], 1) % 360, tilt=round(p["tilt"], 1),
        )
    pans.sort(key=lambda p: (-p["ratio"], -p["area"]))
    for i, p in enumerate(pans, 1):
        p["pan"] = i
    return dict(pans=pans, extras=extras, zg=round(zg, 2), ref=(round(ref[0], 2), round(ref[1], 2)))
