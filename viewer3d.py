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
from pathlib import Path

import pandas as pd
import requests

import santeny_solaire as ss

ORTHO_LAYER = "ORTHOIMAGERY.ORTHOPHOTOS"
ORTHO_PX = 2400


def fetch_ortho(bounds):
    minx, miny, maxx, maxy = bounds
    w, h = maxx - minx, maxy - miny
    px_w = ORTHO_PX
    px_h = int(ORTHO_PX * h / w)
    r = requests.get(ss.WMS_URL, params=dict(
        SERVICE="WMS", VERSION="1.3.0", REQUEST="GetMap", LAYERS=ORTHO_LAYER, STYLES="",
        CRS=ss.CRS, BBOX=f"{minx},{miny},{maxx},{maxy}", WIDTH=px_w, HEIGHT=px_h,
        FORMAT="image/jpeg"), timeout=300)
    r.raise_for_status()
    return base64.b64encode(r.content).decode()


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

    out = []
    for _, b in blds.iterrows():
        h = b.get("hauteur")
        h = float(h) if pd.notna(h) and h else 6.0
        r = res.loc[b["bat_id"]] if b["bat_id"] in res.index else None
        for poly in rings(b.geometry):
            d = dict(r=local(poly.exterior.coords),
                     holes=[local(i.coords) for i in poly.interiors], h=round(h, 1))
            if r is not None:
                d.update(ratio=float(r.meilleur_ratio), az=float(r.meilleur_az),
                         tilt=float(r.meilleure_pente), y=float(r.yield_kwh_kwp),
                         kwh=float(r.kwh_an_bons_pans), shade=float(r.shade_loss))
            out.append(d)

    data = dict(
        w=round(bounds[2] - bounds[0], 1), h=round(bounds[3] - bounds[1], 1),
        outline=local(list(cgeom.convex_hull.exterior.coords)) if False else None,
        buildings=out)
    ortho = fetch_ortho(bounds)
    tpl = Path(__file__).with_name("viewer_template.html").read_text()
    html = tpl.replace("__DATA__", json.dumps(data, separators=(",", ":"))).replace("__ORTHO__", ortho)
    Path(args.out).write_text(html)
    n = sum(1 for d in out if "ratio" in d)
    print(f"{args.out} : {len(out)} bâtiments dont {n} analysés ({Path(args.out).stat().st_size/1e6:.1f} Mo)")


if __name__ == "__main__":
    main()
