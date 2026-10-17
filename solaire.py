#!/usr/bin/env python3
"""
Classement des toits d'une commune française pour le photovoltaïque.

Principe
  1. Contour de la commune (geo.api.gouv.fr) et bâtiments (BD TOPO, WFS Géoplateforme IGN).
  2. Modèle numérique de surface LiDAR HD (MNS) : GeoTIFF local (--mns) ou téléchargement par
     tuiles via le WMS de la Géoplateforme. Le MNS contient les toits, les arbres et le bâti voisin.
  3. Par bâtiment : pente, orientation et position de chaque pan de toit, calculées sur le MNS.
  4. Rendement (kWh/kWc/an) interpolé sur une grille de calculs PVGIS (cachée sur disque).
  5. Pans de toit : composantes connexes de même orientation, avec polygone et plan 3D.
  6. Ombrage par pan : profil d'horizon complet (72 azimuts, jusqu'à 150 m) séparant bâti et
     végétation, croisé avec la course du soleil (12 jours types x 24 h), pondérée par le
     rayonnement direct mensuel de PVGIS. Les arbres laissent passer une partie du soleil hors
     feuillage. Le rayonnement diffus est réduit selon la part de ciel masquée.
  7. Sorties : pans.csv, pans_geo.json (géométrie des pans), batiments.gpkg, batiments.csv,
     meta.json + top 20 dans la console.

Le calcul proprement dit est dans solaire_core.py (numpy + shapely), partagé avec la page
« adresse » du site, qui l'exécute dans le navigateur.

Installation
  pip install -r requirements.txt

Utilisation
  python solaire.py Santeny
  python solaire.py "Boissy-Saint-Léger" --dep 94
  python solaire.py --insee 94070 --limit 50           # test rapide
  Options : --all-usages, --no-shade, --mns mns.tif, --loss 14, --out DIR, --cache DIR

Limites : estimation indicative, à confirmer par une visite / un devis (charpente,
PLU, abords protégés, ombrages proches non captés par le MNS, cheminées, velux...).
"""
import argparse
import datetime
import json
import math
import re
import time
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import geopandas as gpd
import shapely
import rasterio
from rasterio import windows
from rasterio.merge import merge

import solaire_core as core
from solaire_core import (TILTS, AZS, HORIZON_RADIUS, GOOD_RATIO, YieldModel)  # noqa: F401

# --------------------------------------------------------------------------- constantes
CRS = "EPSG:2154"  # Lambert-93
GEO_API = "https://geo.api.gouv.fr/communes"
WFS_URL = "https://data.geopf.fr/wfs/ows"
WMS_URL = "https://data.geopf.fr/wms-r/wms"
MNS_LAYER = "IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"
MNS_RES = 0.5  # résolution demandée (m)
PVGIS_URL = "https://re.jrc.ec.europa.eu/api/v5_3/PVcalc"
PVGIS_MR_URL = "https://re.jrc.ec.europa.eu/api/v5_3/MRcalc"
LOSS_DEFAULT = 14.0    # pertes système PVGIS (%) : câbles, onduleur, salissures...
FRANCE_GRID = Path(__file__).with_name("data") / "pvgis_fr.json"   # grille PVGIS précalculée
# valeurs de repli si PVGIS ne répond pas (climat tempéré du nord de la France)
FALLBACK_H = [28, 50, 94, 146, 164, 210, 178, 160, 110, 66, 33, 22]
FALLBACK_KD = [.70, .55, .51, .42, .48, .38, .40, .42, .47, .55, .68, .72]


# --------------------------------------------------------------------------- commune
def _norm(s):
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def resolve_commune(query=None, insee=None, dep=None):
    """Retrouve une commune (nom, code INSEE ou code postal) via geo.api.gouv.fr."""
    fields = "nom,code,codeDepartement,codesPostaux,population"
    if not query and not insee:
        raise SystemExit("Indique une commune (nom ou code INSEE) ou --insee.")
    if insee or (query and re.fullmatch(r"\d{5}", query.strip())):
        code = insee or query.strip()
        r = requests.get(f"{GEO_API}/{code}", params={"fields": fields}, timeout=60)
        if r.status_code == 200:
            return r.json()
        if insee:
            raise SystemExit(f"Code INSEE inconnu : {insee}")
        r = requests.get(GEO_API, params={"codePostal": code, "fields": fields}, timeout=60)
        cand = r.json() if r.status_code == 200 else []
    else:
        params = dict(nom=query.strip(), fields=fields, boost="population", limit=10)
        if dep:
            params["codeDepartement"] = dep
        r = requests.get(GEO_API, params=params, timeout=60)
        cand = r.json() if r.status_code == 200 else []
        exact = [c for c in cand if _norm(c["nom"]) == _norm(query)]
        cand = exact or cand
        if len(exact) == 1:
            return exact[0]
    if not cand:
        raise SystemExit(f"Aucune commune trouvée pour « {query} ».")
    if len(cand) == 1:
        return cand[0]
    liste = "\n".join(f"  - {c['nom']} ({c['codeDepartement']}) : --insee {c['code']}" for c in cand[:8])
    raise SystemExit(f"« {query} » est ambigu, précise avec --dep ou --insee :\n{liste}")


def fetch_commune(insee):
    r = requests.get(
        f"{GEO_API}/{insee}",
        params={"fields": "nom,contour", "format": "geojson", "geometry": "contour"},
        timeout=60,
    )
    r.raise_for_status()
    return gpd.GeoDataFrame.from_features([r.json()], crs="EPSG:4326").to_crs(CRS)


# --------------------------------------------------------------------------- données
def fetch_buildings(zone):
    """Bâtiments BD TOPO dont le point représentatif tombe dans `zone` (polygone Lambert-93)."""
    minx, miny, maxx, maxy = zone.bounds
    feats, start, page = [], 0, 1000
    while True:
        params = dict(
            SERVICE="WFS", VERSION="2.0.0", REQUEST="GetFeature",
            TYPENAMES="BDTOPO_V3:batiment", OUTPUTFORMAT="application/json",
            SRSNAME=CRS, BBOX=f"{minx},{miny},{maxx},{maxy},{CRS}",
            COUNT=page, STARTINDEX=start,
        )
        r = requests.get(WFS_URL, params=params, timeout=180)
        r.raise_for_status()
        batch = r.json().get("features", [])
        feats += batch
        if len(batch) < page:
            break
        start += page
    if not feats:
        return gpd.GeoDataFrame(geometry=[], crs=CRS)
    gdf = gpd.GeoDataFrame.from_features(feats, crs=CRS)
    gdf["geometry"] = gdf.geometry.apply(shapely.force_2d)
    gdf = gdf[gdf.geometry.representative_point().within(zone)].copy()
    return gdf.reset_index(drop=True)


def download_mns(bounds, cache, tile=1000):
    """Télécharge le MNS LiDAR HD par tuiles via le WMS, puis les assemble."""
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / "mns.tif"
    if out.exists():
        return out
    minx, miny, maxx, maxy = [int(math.floor(v)) for v in bounds]
    files = []
    for x0 in range(minx, maxx, tile):
        for y0 in range(miny, maxy, tile):
            x1, y1 = min(x0 + tile, maxx), min(y0 + tile, maxy)
            f = cache / f"tuile_{x0}_{y0}.tif"
            if not f.exists():
                px = int((x1 - x0) / MNS_RES)
                params = dict(
                    SERVICE="WMS", VERSION="1.3.0", REQUEST="GetMap", LAYERS=MNS_LAYER,
                    STYLES="", CRS=CRS, BBOX=f"{x0},{y0},{x1},{y1}",
                    WIDTH=px, HEIGHT=int((y1 - y0) / MNS_RES), FORMAT="image/geotiff",
                )
                for attempt in range(6):  # le service répond parfois « couche inconnue »
                    r = requests.get(WMS_URL, params=params, timeout=300)
                    if r.status_code == 200 and "tif" in r.headers.get("content-type", "").lower():
                        break
                    time.sleep(2 * (attempt + 1))
                else:
                    raise RuntimeError(
                        "Échec du téléchargement WMS du MNS "
                        f"({r.status_code}) : {r.text[:300]}\n"
                        "Télécharge plutôt les dalles MNS LiDAR HD sur geoservices.ign.fr/lidarhd "
                        "et passe le GeoTIFF avec --mns."
                    )
                f.write_bytes(r.content)
            files.append(f)
    srcs = [rasterio.open(f) for f in files]
    mosaic, transform = merge(srcs)
    meta = srcs[0].meta.copy()
    meta.update(height=mosaic.shape[1], width=mosaic.shape[2], transform=transform, crs=CRS)
    for s in srcs:
        s.close()
    with rasterio.open(out, "w", **meta) as dst:
        dst.write(mosaic)
    return out


# --------------------------------------------------------------------------- PVGIS
def pvgis_live(lat, lon, cache_file, loss=LOSS_DEFAULT):
    """Grille rendement (kWh/kWc/an) en fonction de (inclinaison, azimut) : appels PVGIS en direct."""
    if cache_file.exists():
        return np.array(json.loads(cache_file.read_text()))
    grid = np.zeros((len(TILTS), len(AZS)))
    sess = requests.Session()
    for i, tilt in enumerate(TILTS):
        for j, az in enumerate(AZS[:-1]):
            params = dict(
                lat=lat, lon=lon, peakpower=1, loss=loss, angle=tilt,
                aspect=az - 180,  # PVGIS : 0 = sud, 90 = ouest, -90 = est
                outputformat="json",
            )
            for attempt in range(4):
                r = sess.get(PVGIS_URL, params=params, timeout=60)
                if r.status_code == 200:
                    break
                time.sleep(1.5 * (attempt + 1))
            r.raise_for_status()
            grid[i, j] = r.json()["outputs"]["totals"]["fixed"]["E_y"]
            time.sleep(0.15)
        grid[i, -1] = grid[i, 0]  # 360° = 0°
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(grid.tolist()))
    return grid


def pvgis_climate(lat, lon, cache_file):
    """Irradiation horizontale mensuelle (kWh/m²) et part de diffus (Kd), moyennes PVGIS."""
    if cache_file.exists():
        d = json.loads(cache_file.read_text())
        return np.array(d["H"]), np.array(d["Kd"])
    try:
        r = requests.get(PVGIS_MR_URL, params=dict(
            lat=lat, lon=lon, horirrad=1, d2g=1, outputformat="json",
            startyear=2015, endyear=2020), timeout=60)
        r.raise_for_status()
        df = pd.DataFrame(r.json()["outputs"]["monthly"])
        g = df.groupby("month")[["H(h)_m", "Kd"]].mean()
        H, Kd = g["H(h)_m"].to_numpy(), g["Kd"].to_numpy()
        assert len(H) == 12
    except Exception as e:  # repli : climat tempéré type
        print(f"  PVGIS (climat mensuel) indisponible ({e}) : valeurs de repli.")
        H, Kd = np.array(FALLBACK_H, float), np.array(FALLBACK_KD, float)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"H": H.tolist(), "Kd": Kd.tolist()}))
    return H, Kd


def yield_model(lat, lon, cache, loss, use_grid=False):
    """(YieldModel, H, Kd) : appels PVGIS en direct (exacts, mis en cache) ; en cas d'échec ou avec
    --grid-pvgis, grille précalculée de la France (interpolée, moins précise sur les côtes)."""
    if not use_grid or loss != LOSS_DEFAULT or not FRANCE_GRID.exists():
        try:
            print("Rendements PVGIS (grille inclinaison x azimut, appels directs, mis en cache)...")
            grid = pvgis_live(lat, lon, cache / f"pvgis_grille_l{loss:g}.json", loss)
            H, Kd = pvgis_climate(lat, lon, cache / "pvgis_climat.json")
            return YieldModel(grid), H, Kd
        except Exception as e:
            if loss != LOSS_DEFAULT or not FRANCE_GRID.exists():
                raise
            print(f"  PVGIS indisponible ({e}) : repli sur la grille précalculée.")
    print("Rendements PVGIS : grille précalculée de la France (interpolée).")
    return core.france_model(json.loads(FRANCE_GRID.read_text()), lat, lon)


# --------------------------------------------------------------------------- raster
def read_window(src, geom, pad):
    """MNS autour de `geom` (marge `pad` m) : tableau float, géoréférencement core.Tf."""
    minx, miny, maxx, maxy = geom.buffer(pad).bounds
    win = windows.from_bounds(minx, miny, maxx, maxy, src.transform).round_offsets().round_lengths()
    Z = src.read(1, window=win, boundless=True, fill_value=np.nan).astype(float)
    if src.nodata is not None:
        Z[Z == src.nodata] = np.nan
    Z[Z < -100] = np.nan
    T = src.window_transform(win)
    return Z, core.Tf(T.c, T.a, T.f, T.e)


# --------------------------------------------------------------------------- programme
def analyse(commune, out, cache, mns=None, all_usages=False, shade=True, limit=0,
            loss=LOSS_DEFAULT, max_batiments=0, grid_pvgis=False):
    """Analyse complète d'une commune (dict renvoyé par resolve_commune). Écrit dans `out`."""
    insee = commune["code"]
    if insee.startswith(("97", "98")):
        raise SystemExit("Outre-mer non pris en charge (BD TOPO / LiDAR HD en Lambert-93 métropole).")
    out, cache = Path(out), Path(cache) / insee
    out.mkdir(parents=True, exist_ok=True)

    print(f"{commune['nom']} ({commune['codeDepartement']}, INSEE {insee})")
    print("Contour communal et bâtiments BD TOPO...")
    cgeom = fetch_commune(insee).geometry.iloc[0]
    allb = fetch_buildings(cgeom.buffer(HORIZON_RADIUS + 20))
    blds = allb[allb.geometry.representative_point().within(cgeom)]
    if not all_usages and "usage_1" in blds:
        blds = blds[blds["usage_1"].fillna("").str.contains("sidentiel")]
    blds = blds[blds.geometry.area >= 30].reset_index(drop=True)
    if limit:
        blds = blds.head(limit)
    if max_batiments and len(blds) > max_batiments:
        raise SystemExit(f"{len(blds)} bâtiments à analyser : au-delà de la limite de {max_batiments}.")
    print(f"{len(blds)} bâtiments à analyser")
    if blds.empty:
        raise SystemExit("Aucun bâtiment résidentiel trouvé.")

    mns = Path(mns) if mns else download_mns(tuple(cgeom.buffer(HORIZON_RADIUS + 20).bounds), cache / "mns")
    c4326 = gpd.GeoSeries([cgeom.centroid], crs=CRS).to_crs("EPSG:4326").iloc[0]
    lat, lon = c4326.y, c4326.x
    ym, H, Kd = yield_model(lat, lon, cache, loss, grid_pvgis)
    print(f"Rendement optimal de la grille : {ym.ymax:.0f} kWh/kWc/an")
    sun = core.make_sun_context(lat, lon, H, Kd) if shade else None
    if shade:
        print(f"Part de rayonnement diffus : {(H * Kd).sum() / H.sum():.0%} (PVGIS)")

    rows, geo, errors = [], {}, 0
    sidx = allb.sindex
    pad = (HORIZON_RADIUS + 5) if shade else core.WIN_SMALL + 1
    with rasterio.open(mns) as src:
        for n, (i, b) in enumerate(blds.iterrows(), 1):
            bid = b.get("cleabs", i)
            Zb, Tb = read_window(src, b.geometry, pad)
            bounds = (Tb.c, Tb.f + Tb.e * Zb.shape[0], Tb.c + Tb.a * Zb.shape[1], Tb.f)
            nearby = list(allb.geometry.iloc[sidx.query(shapely.box(*bounds))]) if shade else []
            try:
                r = core.analyze_building(b.geometry, Zb, Tb, nearby, ym, sun, shade=shade)
            except Exception as e:   # un bâtiment à la géométrie douteuse ne doit pas arrêter la commune
                errors += 1
                print(f"  bâtiment {bid} ignoré : {e}")
                r = None
            if r:
                geo[bid] = dict(zg=r["zg"], ref=r["ref"], extras=r["extras"], pans=[
                    dict(pan=p["pan"], plane=p["plane"], zr=p["zr"], polys=p["polys"], ratio=p["ratio"],
                         y=p["yield_kwh_kwp"], az=p["az"], tilt=p["tilt"], area=p["area_m2"], n=p["panneaux"], kwp=p["kwp"],
                         kwh=p["kwh_an"], sh=p["shade_loss"], sb=p["shade_bati"], sa=p["shade_arbres"],
                         flat=bool(p["flat"])) for p in r["pans"]])
                for p in r["pans"]:
                    rows.append(dict(
                        bat_id=bid, pan=p["pan"], az=p["az"], tilt=p["tilt"], area_m2=p["area_m2"], plat=p["flat"],
                        shade_loss=p["shade_loss"], shade_bati=p["shade_bati"], shade_arbres=p["shade_arbres"],
                        yield_kwh_kwp=p["yield_kwh_kwp"], ratio=p["ratio"], panneaux=p["panneaux"], kwp=p["kwp"], kwh_an=p["kwh_an"]))
            if n % 100 == 0:
                print(f"  {n}/{len(blds)}", flush=True)
    pans = pd.DataFrame(rows)
    if pans.empty:
        raise SystemExit("Aucun pan détecté : le LiDAR HD couvre-t-il cette commune ?")
    pans.to_csv(out / "pans.csv", index=False)
    (out / "pans_geo.json").write_text(json.dumps(geo, separators=(",", ":")))

    good = pans[pans.ratio >= GOOD_RATIO]
    agg = pans.sort_values("ratio", ascending=False).groupby("bat_id").first()[
        ["az", "tilt", "ratio", "yield_kwh_kwp", "shade_loss", "shade_bati", "shade_arbres"]
    ].rename(columns={"az": "meilleur_az", "tilt": "meilleure_pente", "ratio": "meilleur_ratio"})
    agg["nb_pans"] = pans.groupby("bat_id").size()
    agg["kwh_an_bons_pans"] = good.groupby("bat_id").kwh_an.sum()
    agg["kwp_bons_pans"] = good.groupby("bat_id").kwp.sum()
    agg[["kwh_an_bons_pans", "kwp_bons_pans"]] = agg[["kwh_an_bons_pans", "kwp_bons_pans"]].fillna(0)
    blds["bat_id"] = blds["cleabs"] if "cleabs" in blds else blds.index
    res = blds.merge(agg, left_on="bat_id", right_index=True, how="inner")
    res = res.sort_values(["meilleur_ratio", "kwh_an_bons_pans"], ascending=False)
    res.to_file(out / "batiments.gpkg", driver="GPKG")
    res.drop(columns="geometry").to_csv(out / "batiments.csv", index=False)

    meta = dict(
        nom=commune["nom"], insee=insee, departement=commune["codeDepartement"],
        lat=round(lat, 5), lon=round(lon, 5), batiments=int(len(res)), pans=int(len(pans)),
        ratio_median=round(float(res.meilleur_ratio.median()), 3),
        ombrage_moyen=round(float(res.shade_loss.mean()), 3),
        ombrage_arbres_moyen=round(float(res.shade_arbres.mean()), 3),
        kwc_bons_pans=round(float(res.kwp_bons_pans.sum()), 0),
        gwh_an_bons_pans=round(float(res.kwh_an_bons_pans.sum()) / 1e6, 2),
        rendement_optimal=round(ym.ymax, 0), pertes_systeme=loss,
        ombrage="complet" if shade else "aucun",
        date=datetime.date.today().isoformat(),
    )
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))

    print("\nTop 20 (rendement relatif puis production potentielle) :")
    cols = ["bat_id", "nb_pans", "meilleur_az", "meilleure_pente", "meilleur_ratio",
            "yield_kwh_kwp", "shade_loss", "kwh_an_bons_pans"]
    print(res[cols].head(20).to_string(index=False))
    print(f"\nFichiers écrits dans {out}/ : pans.csv, pans_geo.json, batiments.csv, batiments.gpkg (QGIS), meta.json")
    return meta


def main(argv=None, default=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("commune", nargs="?", default=default, help="nom, code INSEE ou code postal")
    ap.add_argument("--insee", help="code INSEE (évite toute ambiguïté)")
    ap.add_argument("--dep", help="département, pour départager des homonymes (ex. 94)")
    ap.add_argument("--mns", help="GeoTIFF du MNS LiDAR HD (Lambert-93). Sinon téléchargement WMS.")
    ap.add_argument("--out", help="dossier de sortie (défaut : resultats/<INSEE>)")
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--all-usages", action="store_true", help="inclut tous les usages de bâtiment")
    ap.add_argument("--no-shade", action="store_true")
    ap.add_argument("--loss", type=float, default=LOSS_DEFAULT, help="pertes système PVGIS en %% (défaut 14)")
    ap.add_argument("--grid-pvgis", action="store_true", help="utilise la grille PVGIS précalculée au lieu d'appels directs")
    ap.add_argument("--limit", type=int, default=0, help="n'analyse que N bâtiments (test)")
    ap.add_argument("--max-batiments", type=int, default=0, help="refuse les communes plus grosses")
    args = ap.parse_args(argv)
    commune = resolve_commune(args.commune, args.insee, args.dep)
    out = args.out or str(Path("resultats") / commune["code"])
    return analyse(commune, out, args.cache, mns=args.mns, all_usages=args.all_usages,
                   shade=not args.no_shade, limit=args.limit, loss=args.loss,
                   max_batiments=args.max_batiments, grid_pvgis=args.grid_pvgis)


if __name__ == "__main__":
    main()
