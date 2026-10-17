#!/usr/bin/env python3
"""
Génère une maquette 3D interactive (HTML autonome) de Santeny à partir de resultats/batiments.csv.

  python viewer3d.py --res resultats --out santeny_3d.html

Les bâtiments de la BD TOPO sont extrudés à leur hauteur, posés sur l'orthophoto IGN.
Les bâtiments analysés sont colorés selon leur meilleur rendement relatif ; les autres sont gris.
"""
import argparse
import base64
import json
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
import shapely
from shapely.ops import linemerge, unary_union

import santeny_solaire as ss

ORTHO_LAYER = "ORTHOIMAGERY.ORTHOPHOTOS"
ORTHO_PX = 3000


def fetch_ortho(bounds):
    minx, miny, maxx, maxy = bounds
    w, h = maxx - minx, maxy - miny
    px_w = ORTHO_PX
    px_h = int(ORTHO_PX * h / w)
    params = dict(
        SERVICE="WMS", VERSION="1.3.0", REQUEST="GetMap", LAYERS=ORTHO_LAYER, STYLES="",
        CRS=ss.CRS, BBOX=f"{minx},{miny},{maxx},{maxy}", WIDTH=px_w, HEIGHT=px_h,
        FORMAT="image/jpeg")
    for attempt in range(8):  # le service répond parfois « couche inconnue » : on relance
        r = requests.get(ss.WMS_URL, params=params, timeout=300)
        if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
            break
        time.sleep(3)
    else:
        raise RuntimeError(f"Orthophoto indisponible : {r.text[:200]}")
    return base64.b64encode(r.content).decode()


BAL_URL = "https://plateforme.adresse.data.gouv.fr/ban/communes/{}/download/csv-bal/adresses"


def fetch_addresses():
    """Points adresse de la Base Adresse Nationale (Lambert-93)."""
    r = requests.get(BAL_URL.format(ss.INSEE), timeout=120)
    r.raise_for_status()
    from io import StringIO
    df = pd.read_csv(StringIO(r.text), sep=";", dtype=str)
    df = df.dropna(subset=["x", "y", "numero", "voie_nom"])
    df["x"], df["y"] = df.x.astype(float), df.y.astype(float)
    df["suffixe"] = df.suffixe.fillna("")
    df["num"] = df.numero.str.strip() + df.suffixe.str.strip()
    df["label"] = df.num + " " + df.voie_nom
    return gpd.GeoDataFrame(df[["num", "label", "voie_nom"]],
                            geometry=gpd.points_from_xy(df.x, df.y), crs=ss.CRS)


def fetch_roads(bounds):
    """Tronçons de route nommés (BD TOPO) -> ancres d'étiquettes (point + tangente)."""
    minx, miny, maxx, maxy = bounds
    r = requests.get(ss.WFS_URL, params=dict(
        SERVICE="WFS", VERSION="2.0.0", REQUEST="GetFeature", TYPENAMES="BDTOPO_V3:troncon_de_route",
        OUTPUTFORMAT="application/json", SRSNAME=ss.CRS, COUNT=5000,
        BBOX=f"{minx},{miny},{maxx},{maxy},{ss.CRS}"), timeout=180)
    r.raise_for_status()
    feats = r.json().get("features", [])
    if not feats:
        return []
    g = gpd.GeoDataFrame.from_features(feats, crs=ss.CRS)
    g["geometry"] = g.geometry.apply(shapely.force_2d)
    g = g[g.get("position_par_rapport_au_sol", "0").astype(str) == "0"] if "position_par_rapport_au_sol" in g else g

    def name(row):
        for k in ("nom_voie_ban_gauche", "nom_voie_ban_droite", "nom_collaboratif_gauche", "nom_collaboratif_droite"):
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None
    g["nom"] = g.apply(name, axis=1)
    g = g.dropna(subset=["nom"])
    return g[["nom", "importance", "geometry"]]


def road_anchors(roads, bounds, cx, cy, spacing=220.0):
    """Une étiquette par voie (et d'autres sur les longues voies) : [nom, x, y, dx, dy, priorité]."""
    from shapely.geometry import box
    clip = box(*bounds)
    out = []
    for nom, grp in roads.groupby("nom"):
        imp = pd.to_numeric(grp.importance, errors="coerce").min()
        imp = 5 if pd.isna(imp) else imp
        u = unary_union(list(grp.geometry))
        merged = linemerge(u) if u.geom_type == "MultiLineString" else u
        if merged.geom_type == "LineString":
            lines = [merged]
        else:
            lines = [g for g in getattr(merged, "geoms", []) if g.geom_type == "LineString"]
        for ln in lines:
            ln = ln.intersection(clip)
            if ln.is_empty or ln.geom_type != "LineString" or ln.length < 40:
                continue
            n = max(1, int(ln.length // spacing))
            for k in range(n):
                d = ln.length * (k + 0.5) / n
                a, b = ln.interpolate(max(d - 6, 0)), ln.interpolate(min(d + 6, ln.length))
                p = ln.interpolate(d)
                out.append([nom, round(p.x - cx, 1), round(p.y - cy, 1),
                            round(b.x - a.x, 2), round(b.y - a.y, 2), round(float(imp) + k * 0.01, 2)])
    return out


def rings(geom):
    polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    return polys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", default="resultats")
    ap.add_argument("--out", default="santeny_3d.html")
    args = ap.parse_args()

    commune = ss.fetch_commune()
    cgeom = commune.geometry.iloc[0]
    blds = ss.fetch_buildings(cgeom)
    res = pd.read_csv(Path(args.res) / "batiments.csv")
    key = "cleabs" if "cleabs" in blds else None
    blds["bat_id"] = blds[key] if key else blds.index
    res = res.set_index("bat_id")

    minx, miny, maxx, maxy = cgeom.bounds
    pad = 80
    bounds = (minx - pad, miny - pad, maxx + pad, maxy + pad)
    cx, cy = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2

    def local(coords):
        return [[round(x - cx, 1), round(y - cy, 1)] for x, y in coords][:-1]

    addr = fetch_addresses()
    near = gpd.sjoin_nearest(addr, blds[["geometry"]], how="left", max_distance=25)
    near = near[~near.index.duplicated()]
    by_bld = {}
    for pi, row in near.iterrows():
        if pd.notna(row.get("index_right")):
            by_bld.setdefault(int(row.index_right), []).append(row.label)

    out, parts = [], {}
    for bi, b in blds.iterrows():
        h = b.get("hauteur")
        h = float(h) if pd.notna(h) and h else 6.0
        r = res.loc[b["bat_id"]] if b["bat_id"] in res.index else None
        for poly in rings(b.geometry):
            d = dict(r=local(poly.exterior.coords),
                     holes=[local(i.coords) for i in poly.interiors], h=round(h, 1))
            labs = sorted(set(by_bld.get(bi, [])))
            rp = poly.representative_point()
            d.update(cx=round(rp.x - cx, 1), cy=round(rp.y - cy, 1))
            if labs:
                d["a"] = " · ".join(labs[:4]) + (f" (+{len(labs) - 4})" if len(labs) > 4 else "")
                d["n"] = labs[0].split(" ")[0]
            parts.setdefault(bi, []).append(len(out))
            if r is not None:
                d.update(ratio=float(r.meilleur_ratio), az=float(r.meilleur_az),
                         tilt=float(r.meilleure_pente), y=float(r.yield_kwh_kwp),
                         kwh=float(r.kwh_an_bons_pans), shade=float(r.shade_loss))
            out.append(d)

    pts = []
    for pi, a in addr.iterrows():
        bi = near.loc[pi, "index_right"] if pi in near.index else None
        mi = parts[int(bi)][0] if pd.notna(bi) and int(bi) in parts else -1
        pts.append([a.label, round(a.geometry.x - cx, 1), round(a.geometry.y - cy, 1), mi])

    roads = fetch_roads(cgeom.buffer(120).bounds)
    streets = road_anchors(roads, bounds, cx, cy) if len(roads) else []

    data = dict(
        w=round(bounds[2] - bounds[0], 1), h=round(bounds[3] - bounds[1], 1),
        buildings=out, addresses=pts, streets=streets)
    ortho = fetch_ortho(bounds)
    tpl = Path(__file__).with_name("viewer_template.html").read_text()
    html = tpl.replace("__DATA__", json.dumps(data, separators=(",", ":"))).replace("__ORTHO__", ortho)
    Path(args.out).write_text(html)
    n = sum(1 for d in out if "ratio" in d)
    print(f"{len(pts)} adresses, {len(streets)} étiquettes de rues, {sum(1 for d in out if 'a' in d)} bâtiments adressés")
    print(f"{args.out} : {len(out)} bâtiments dont {n} analysés ({Path(args.out).stat().st_size/1e6:.1f} Mo)")


if __name__ == "__main__":
    main()
