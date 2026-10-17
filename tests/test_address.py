"""Test de bout en bout de solaire_address (réseau requis : BAN, BD TOPO, LiDAR HD).
Reproduit ce que fait la page web : localiser, choisir le bâtiment, télécharger le MNS, analyser."""
import json
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import solaire_address as sa

WFS = "https://data.geopf.fr/wfs/ows"
WMS = "https://data.geopf.fr/wms-r/wms"


def run(query, grid_text):
    r = requests.get("https://api-adresse.data.gouv.fr/search/", params=dict(q=query, limit=1), timeout=30).json()
    f = r["features"][0]
    lon, lat = f["geometry"]["coordinates"]
    label = f["properties"]["label"]
    loc = json.loads(sa.locate_bbox(lon, lat))
    b = loc["bbox"]
    gj = requests.get(WFS, params=dict(SERVICE="WFS", VERSION="2.0.0", REQUEST="GetFeature", TYPENAMES="BDTOPO_V3:batiment",
                                       OUTPUTFORMAT="application/json", SRSNAME="EPSG:2154", COUNT=1000,
                                       BBOX=",".join(map(str, b)) + ",EPSG:2154"), timeout=120).text
    t = json.loads(sa.pick_target(lon, lat, gj))
    assert "error" not in t, t
    bb = t["bbox"]
    bil = requests.get(WMS, params=dict(SERVICE="WMS", VERSION="1.3.0", REQUEST="GetMap",
                                        LAYERS="IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93", STYLES="",
                                        CRS="EPSG:2154", BBOX=",".join(map(str, bb)), WIDTH=t["width"], HEIGHT=t["height"],
                                        FORMAT="image/x-bil;bits=32"), timeout=120).content
    out = json.loads(sa.analyze(lon, lat, label, gj, bil, bb, t["width"], t["height"], grid_text))
    return label, t, out


if __name__ == "__main__":
    import numpy as np
    g = json.load(open(os.path.join(os.path.dirname(__file__), "..", "cache", "94070", "pvgis_grille_l14.json")))
    node = dict(g=np.array(g)[:, :-1].tolist(), H=[28, 50, 94, 146, 164, 210, 178, 160, 110, 66, 33, 22], Kd=[.7, .55, .51, .42, .48, .38, .4, .42, .47, .55, .68, .72])
    grid = dict(lat0=48.5, lon0=2.5, step=0.5, nodes={f"{i},{j}": node for i in (0, 1) for j in (0, 1)})
    label, t, out = run("10 Rue du Clos Santenois 94440 Santeny", json.dumps(grid))
    print(label, t)
    b = out["buildings"][out["focus"]]
    print(len(out["buildings"]), "bâtiments ; pans :", [(p["pan"], p["az"], p["tilt"], p["area"], p["y"], p["sh"]) for p in b["pans"]])
    assert len(b["pans"]) >= 2 and b["y"] > 800
    print("OK")
