#!/usr/bin/env python3
"""
Génère la maquette 3D interactive (HTML autonome) d'une commune analysée par solaire.py.

  python viewer3d.py Santeny                 # lit resultats/94070/
  python viewer3d.py --res resultats/94070

Écrit communes/<INSEE>.html (+ communes/<INSEE>.json) et régénère index.html (choix de la ville).
Les bâtiments de la BD TOPO sont extrudés à leur hauteur, posés sur l'orthophoto IGN, avec les
noms de rues et les adresses de la Base Adresse Nationale.
"""
import argparse
import base64
import json
import time
from io import StringIO
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
import shapely
from shapely.ops import linemerge, unary_union

import solaire as ss
import site_index

ORTHO_LAYER = "ORTHOIMAGERY.ORTHOPHOTOS"
ORTHO_PX = 4800  # côté le plus long, en pixels (max 5000 pour le WMS)
BAL_URL = "https://plateforme.adresse.data.gouv.fr/ban/communes/{}/download/csv-bal/adresses"


def fetch_ortho(bounds):
    minx, miny, maxx, maxy = bounds
    w, h = maxx - minx, maxy - miny
    px_w = ORTHO_PX if w >= h else int(ORTHO_PX * w / h)
    px_h = int(ORTHO_PX * h / w) if w >= h else ORTHO_PX
    px_w, px_h = min(px_w, 5000), min(px_h, 5000)
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


def fetch_addresses(insee):
    """Points adresse de la Base Adresse Nationale (Lambert-93)."""
    r = requests.get(BAL_URL.format(insee), timeout=120)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text), sep=";", dtype=str)
    df = df.dropna(subset=["x", "y", "numero", "voie_nom"])
    df["x"], df["y"] = df.x.astype(float), df.y.astype(float)
    df["suffixe"] = df.suffixe.fillna("")
    df["num"] = df.numero.str.strip() + df.suffixe.str.strip()
    df["label"] = df.num + " " + df.voie_nom
    return gpd.GeoDataFrame(df[["num", "label", "voie_nom"]],
                            geometry=gpd.points_from_xy(df.x, df.y), crs=ss.CRS)


def fetch_roads(bounds):
    """Tronçons de route nommés (BD TOPO)."""
    minx, miny, maxx, maxy = bounds
    r = requests.get(ss.WFS_URL, params=dict(
        SERVICE="WFS", VERSION="2.0.0", REQUEST="GetFeature", TYPENAMES="BDTOPO_V3:troncon_de_route",
        OUTPUTFORMAT="application/json", SRSNAME=ss.CRS, COUNT=5000,
        BBOX=f"{minx},{miny},{maxx},{maxy},{ss.CRS}"), timeout=180)
    r.raise_for_status()
    feats = r.json().get("features", [])
    if not feats:
        return None
    g = gpd.GeoDataFrame.from_features(feats, crs=ss.CRS)
    g["geometry"] = g.geometry.apply(shapely.force_2d)
    if "position_par_rapport_au_sol" in g:
        g = g[g.position_par_rapport_au_sol.astype(str) == "0"]

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


def local_geo(g, part, cx, cy):
    """Pans et zones annexes d'un bâtiment (pans_geo.json) restreints à `part`, en repère local."""
    if not g:
        return [], []
    xr, yr = g["ref"]

    def conv(polys_in):
        polys = []
        for ext, holes in polys_in:
            pt = shapely.Polygon(ext).representative_point()
            if part.buffer(0.3).contains(pt):
                polys.append([[[round(x - cx, 1), round(y - cy, 1)] for x, y in ext],
                              [[[round(x - cx, 1), round(y - cy, 1)] for x, y in h] for h in holes]])
        return polys

    def plane(pl):
        a, b, c = pl
        return [round(a, 4), round(b, 4), round(c + a * (cx - xr) + b * (cy - yr), 3)]

    out = []
    for p in g["pans"]:
        polys = conv(p["polys"])
        if polys:
            out.append(dict(
                pan=p["pan"], pl=plane(p["plane"]), zr=p["zr"], polys=polys,
                ratio=p["ratio"], y=p["y"], az=p["az"], tilt=p["tilt"], area=p["area"], n=p["n"], kwp=p["kwp"], kwh=p["kwh"],
                sh=p["sh"], sb=p["sb"], sa=p["sa"], flat=p["flat"]))
    xtra = []
    for e in g.get("extras", []):
        polys = conv(e["polys"])
        if polys:
            xtra.append(dict(pl=plane(e["plane"]), zr=e["zr"], polys=polys))
    return out, xtra


def rings(geom):
    return [geom] if geom.geom_type == "Polygon" else list(geom.geoms)


def build(res_dir, out_html=None, site_dir=".", home="../index.html"):
    res_dir, site_dir = Path(res_dir), Path(site_dir)
    meta = json.loads((res_dir / "meta.json").read_text())
    insee = meta["insee"]
    out_html = Path(out_html) if out_html else site_dir / "communes" / f"{insee}.html"
    out_html.parent.mkdir(parents=True, exist_ok=True)

    cgeom = ss.fetch_commune(insee).geometry.iloc[0]
    blds = ss.fetch_buildings(cgeom)
    res = pd.read_csv(res_dir / "batiments.csv").set_index("bat_id")
    geo = json.loads((res_dir / "pans_geo.json").read_text())
    blds["bat_id"] = blds["cleabs"] if "cleabs" in blds else blds.index

    minx, miny, maxx, maxy = cgeom.bounds
    pad = 80
    bounds = (minx - pad, miny - pad, maxx + pad, maxy + pad)
    cx, cy = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2

    def local(coords):
        return [[round(x - cx, 1), round(y - cy, 1)] for x, y in coords][:-1]

    try:
        addr = fetch_addresses(insee)
    except Exception as e:
        print(f"Adresses indisponibles ({e})")
        addr = gpd.GeoDataFrame({"label": []}, geometry=[], crs=ss.CRS)
    by_bld, near = {}, None
    if len(addr):
        near = gpd.sjoin_nearest(addr, blds[["geometry"]], how="left", max_distance=25)
        near = near[~near.index.duplicated()]
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
                         kwh=float(r.kwh_an_bons_pans), kw=float(r.kwp_bons_pans),
                         shade=float(r.shade_loss), sb=float(r.shade_bati), sa=float(r.shade_arbres))
                pans, xtra = local_geo(geo.get(b["bat_id"]), poly, cx, cy)
                if pans:
                    d["pans"] = pans
                    d["xtra"] = xtra
            out.append(d)

    pts = []
    if near is not None:
        for pi, a in addr.iterrows():
            bi = near.loc[pi, "index_right"] if pi in near.index else None
            mi = parts[int(bi)][0] if pd.notna(bi) and int(bi) in parts else -1
            pts.append([a.label, round(a.geometry.x - cx, 1), round(a.geometry.y - cy, 1), mi])

    try:
        roads = fetch_roads(cgeom.buffer(120).bounds)
        streets = road_anchors(roads, bounds, cx, cy) if roads is not None and len(roads) else []
    except Exception as e:
        print(f"Rues indisponibles ({e})")
        streets = []

    data = dict(
        w=round(bounds[2] - bounds[0], 1), h=round(bounds[3] - bounds[1], 1),
        meta=dict(nom=meta["nom"], insee=insee),
        buildings=out, addresses=pts, streets=streets)
    ortho = fetch_ortho(bounds)
    tpl = Path(__file__).with_name("viewer_template.html").read_text()
    html = (tpl.replace("__DATA__", json.dumps(data, separators=(",", ":")))
               .replace("__ORTHO__", ortho)
               .replace("__NAME__", meta["nom"]).replace("__HOME__", home))
    out_html.write_text(html)
    n = sum(1 for d in out if "ratio" in d)
    print(f"{len(pts)} adresses, {len(streets)} étiquettes de rues, {sum(1 for d in out if 'a' in d)} bâtiments adressés")
    print(f"{out_html} : {len(out)} bâtiments dont {n} analysés ({out_html.stat().st_size/1e6:.1f} Mo)")

    meta["fichier"] = out_html.name
    (out_html.parent / f"{insee}.json").write_text(json.dumps(meta, ensure_ascii=False))
    site_index.build_index(site_dir)
    return out_html


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("commune", nargs="?", help="nom ou code INSEE (lit resultats/<INSEE>)")
    ap.add_argument("--insee")
    ap.add_argument("--dep")
    ap.add_argument("--res", help="dossier de résultats (défaut : resultats/<INSEE>)")
    ap.add_argument("--out", help="fichier HTML de sortie (défaut : communes/<INSEE>.html)")
    ap.add_argument("--site", default=".", help="racine du site (index.html, communes/)")
    args = ap.parse_args(argv)
    if args.res:
        res = Path(args.res)
    else:
        c = ss.resolve_commune(args.commune, args.insee, args.dep)
        res = Path("resultats") / c["code"]
    return build(res, args.out, args.site)


if __name__ == "__main__":
    main()
