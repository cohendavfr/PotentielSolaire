"""
Analyse solaire d'une adresse : numpy + shapely, sans réseau.

Les données (bâtiments BD TOPO, MNS LiDAR HD) sont téléchargées par l'appelant : la page « adresse »
du site, en JavaScript, qui exécute ce module dans le navigateur via Pyodide.

Trois étapes :
  locate_bbox(lon, lat)            -> emprise Lambert-93 où chercher les bâtiments
  pick_target(lon, lat, geojson)   -> bâtiment de l'adresse et emprise du MNS à télécharger
  analyze(...)                     -> JSON pour le visualiseur 3D
"""
import json
import math

import numpy as np
import shapely
from shapely.geometry import shape, Point

import solaire_core as core

SEARCH_RADIUS = 220.0   # m : bâtiments téléchargés autour de l'adresse
MAX_SNAP = 45.0         # m : distance max entre l'adresse et le bâtiment retenu
DSM_MARGIN = core.HORIZON_RADIUS + 15.0
DSM_RES = 0.5


def locate_bbox(lon, lat):
    x, y = core.lambert93(lon, lat)
    r = SEARCH_RADIUS
    return json.dumps(dict(x=x, y=y, bbox=[x - r, y - r, x + r, y + r]))


def _polys(feature):
    g = shapely.force_2d(shape(feature["geometry"]))
    return [g] if g.geom_type == "Polygon" else list(g.geoms)


def pick_target(lon, lat, geojson_text, prefer_id=None):
    """Bâtiment dont l'emprise contient l'adresse (sinon le plus proche), et emprise du MNS."""
    x, y = core.lambert93(lon, lat)
    pt = Point(x, y)
    best = None
    for f in json.loads(geojson_text)["features"]:
        for p in _polys(f):
            if p.area < 8:
                continue
            d = 0.0 if p.contains(pt) else p.distance(pt)
            # bâtiment demandé explicitement ; sinon le plus proche, et à égalité le plus grand
            want = bool(prefer_id) and f.get("properties", {}).get("cleabs") == prefer_id
            key = (not want, round(d, 1), -p.area)
            if best is None or key < best[0]:
                best = (key, f, p, d)
    if best is None or (best[3] > MAX_SNAP and not prefer_id):
        return json.dumps(dict(error="Aucun bâtiment trouvé à cette adresse dans la BD TOPO."))
    _, f, p, d = best
    minx, miny, maxx, maxy = p.bounds
    m = DSM_MARGIN
    # emprise alignée sur la grille de 0,5 m, au mètre près
    bbox = [math.floor(minx - m), math.floor(miny - m), math.ceil(maxx + m), math.ceil(maxy + m)]
    w = int(round((bbox[2] - bbox[0]) / DSM_RES))
    h = int(round((bbox[3] - bbox[1]) / DSM_RES))
    return json.dumps(dict(id=f.get("properties", {}).get("cleabs"), distance=round(d, 1), bbox=bbox, width=w, height=h,
                           x=x, y=y, area=round(p.area, 1)))


def _local_ring(coords, cx, cy):
    return [[round(x - cx, 1), round(y - cy, 1)] for x, y in coords]


def analyze(lon, lat, label, geojson_text, bil_bytes, bbox, width, height, grid_text, prefer_id=None):
    if isinstance(bbox, str):
        bbox = json.loads(bbox)
    if hasattr(bil_bytes, "to_py"):          # Uint8Array JavaScript -> memoryview
        bil_bytes = bil_bytes.to_py()
    x, y = core.lambert93(lon, lat)
    pt = Point(x, y)
    feats = json.loads(geojson_text)["features"]
    items = []   # (feature, polygon)
    for f in feats:
        for p in _polys(f):
            items.append((f, p))
    target = None
    for f, p in items:
        if p.area < 8:
            continue
        d = 0.0 if p.contains(pt) else p.distance(pt)
        want = bool(prefer_id) and f.get("properties", {}).get("cleabs") == prefer_id
        key = (not want, round(d, 1), -p.area)
        if target is None or key < target[0]:
            target = (key, f, p)
    _, tf, tgeom = target
    tprops = tf.get("properties", {})

    minx, miny, maxx, maxy = bbox
    Z = np.frombuffer(bytes(bil_bytes), dtype="<f4").reshape(height, width).astype(float)
    Z[Z < -100] = np.nan
    T = core.Tf(minx, (maxx - minx) / width, maxy, -(maxy - miny) / height)
    if np.isfinite(Z).mean() < 0.5:
        return json.dumps(dict(error="Le LiDAR HD de l'IGN ne couvre pas (encore) cette zone."))

    grid = json.loads(grid_text)
    ym, H, Kd = core.france_model(grid, lat, lon)
    sun = core.make_sun_context(lat, lon, H, Kd)
    nearby = [p for f, p in items if p is not tgeom]
    res = core.analyze_building(tgeom, Z, T, nearby, ym, sun, shade=True)
    if not res:
        return json.dumps(dict(error="Toit non analysable : surface trop petite ou mesures LiDAR insuffisantes."))

    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2   # origine locale = centre de l'emprise
    xr, yr = res["ref"]
    pans = []
    for p in res["pans"]:
        a, b, c = p["plane"]
        polys = [[_local_ring(ext, cx, cy), [_local_ring(h, cx, cy) for h in holes]] for ext, holes in p["polys"]]
        pans.append(dict(
            pan=p["pan"], pl=[round(a, 4), round(b, 4), round(c + a * (cx - xr) + b * (cy - yr), 3)], zr=p["zr"], polys=polys,
            ratio=p["ratio"], y=p["yield_kwh_kwp"], az=p["az"], tilt=p["tilt"], area=p["area_m2"], kwp=p["kwp"],
            kwh=p["kwh_an"], sh=p["shade_loss"], sb=p["shade_bati"], sa=p["shade_arbres"], flat=bool(p["flat"])))
    xtra = []
    for e in res["extras"]:
        a, b, c = e["plane"]
        xtra.append(dict(pl=[round(a, 4), round(b, 4), round(c + a * (cx - xr) + b * (cy - yr), 3)], zr=e["zr"],
                         polys=[[_local_ring(ext, cx, cy), [_local_ring(h, cx, cy) for h in holes]] for ext, holes in e["polys"]]))
    good = [p for p in res["pans"] if p["ratio"] >= core.GOOD_RATIO]
    best = res["pans"][0]

    buildings = []
    for f, p in items:
        if not p.intersects(shapely.box(minx, miny, maxx, maxy)):
            continue
        hgt = f.get("properties", {}).get("hauteur")
        hgt = float(hgt) if hgt else 6.0
        rp = p.representative_point()
        d = dict(id=f.get("properties", {}).get("cleabs"), r=_local_ring(p.exterior.coords[:-1], cx, cy), holes=[_local_ring(i.coords[:-1], cx, cy) for i in p.interiors],
                 h=round(hgt, 1), cx=round(rp.x - cx, 1), cy=round(rp.y - cy, 1))
        if p is tgeom:
            d.update(ratio=best["ratio"], az=best["az"], tilt=best["tilt"], y=best["yield_kwh_kwp"],
                     kwh=float(sum(q["kwh_an"] for q in good)), kw=float(sum(q["kwp"] for q in good)),
                     shade=best["shade_loss"], sb=best["shade_bati"], sa=best["shade_arbres"], pans=pans, xtra=xtra, a=label)
            target_index = len(buildings)
        buildings.append(d)

    return json.dumps(dict(
        w=round(maxx - minx, 1), h=round(maxy - miny, 1), buildings=buildings, addresses=[], streets=[],
        focus=target_index, meta=dict(rendement_optimal=round(ym.ymax), diffus=round(float((H * Kd).sum() / H.sum()), 2),
                                      batiment=tprops.get("cleabs"), usage=tprops.get("usage_1"))),
        separators=(",", ":"))
