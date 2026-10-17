#!/usr/bin/env python3
"""
Classement des toits de Santeny (94069) pour le photovoltaïque.

Principe
  1. Contour de la commune (geo.api.gouv.fr) et bâtiments (BD TOPO, WFS Géoplateforme IGN).
  2. Modèle numérique de surface LiDAR HD (MNS) : soit un GeoTIFF local (--mns, recommandé),
     soit un téléchargement par tuiles via le WCS de la Géoplateforme.
  3. Par bâtiment : pente et orientation de chaque pan de toit calculées sur le MNS.
  4. Rendement (kWh/kWc/an) interpolé sur une grille de calculs PVGIS (cachée sur disque).
  5. Perte par ombrage lointain estimée depuis le MNS (indicatif, secteur sud).
  6. Sorties : pans.csv, batiments.gpkg, batiments.csv + top 20 dans la console.

Installation
  pip install geopandas rasterio shapely scipy numpy requests pandas

Utilisation
  python santeny_solaire.py --out resultats
  python santeny_solaire.py --mns mns_santeny.tif --out resultats
  Options : --all-usages (inclut annexes, commerces...), --no-shade, --limit 50 (test rapide)

Limites : estimation indicative, à confirmer par une visite / un devis (charpente,
PLU, abords protégés, ombrages proches non captés par le MNS, cheminées, velux...).
"""
import argparse
import math
import time
import json
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import geopandas as gpd
import shapely
import rasterio
from rasterio import features, windows
from rasterio.merge import merge
from scipy.interpolate import RegularGridInterpolator

# --------------------------------------------------------------------------- constantes
INSEE = "94069"  # Santeny
CRS = "EPSG:2154"  # Lambert-93
WFS_URL = "https://data.geopf.fr/wfs/ows"
WCS_URL = "https://data.geopf.fr/wcs/ows"
# À vérifier dans le GetCapabilities du WCS si le téléchargement échoue
MNS_COVERAGE = "IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"
PVGIS_URL = "https://re.jrc.ec.europa.eu/api/v5_3/PVcalc"

FLAT_SLOPE = 8.0       # en dessous : toit considéré plat (°)
MAX_SLOPE = 60.0       # au dessus : façade / bruit
ERODE = 0.6            # recul par rapport à l'emprise pour éviter murs et débords (m)
MIN_PAN_M2 = 6.0       # pan minimal retenu
USABLE_RATIO = 0.6     # part du pan réellement équipable (rives, cheminées, velux)
FLAT_USABLE = 0.45     # idem toit plat (espacement des rangées)
FLAT_TILT = 15.0       # inclinaison supposée des structures sur toit plat
M2_PER_KWP = 5.0       # surface de panneaux par kWc
PAN_BIN = 20           # largeur des classes d'azimut pour regrouper les pans (°)
SHADE_RADIUS = 40.0    # distance de recherche des obstacles (m)
GOOD_RATIO = 0.85      # un pan est "bon" s'il fait au moins 85 % du rendement optimal

TILTS = list(range(0, 91, 10))
AZS = list(range(0, 361, 30))  # azimut boussole : 0 = nord, 180 = sud


# --------------------------------------------------------------------------- données
def fetch_commune():
    r = requests.get(
        f"https://geo.api.gouv.fr/communes/{INSEE}",
        params={"fields": "nom,contour", "format": "geojson", "geometry": "contour"},
        timeout=60,
    )
    r.raise_for_status()
    return gpd.GeoDataFrame.from_features([r.json()], crs="EPSG:4326").to_crs(CRS)


def fetch_buildings(commune_geom):
    minx, miny, maxx, maxy = commune_geom.bounds
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
    gdf = gpd.GeoDataFrame.from_features(feats, crs=CRS)
    gdf["geometry"] = gdf.geometry.apply(shapely.force_2d)
    gdf = gdf[gdf.geometry.representative_point().within(commune_geom)].copy()
    return gdf.reset_index(drop=True)


def download_mns(bounds, cache, tile=1000):
    """Télécharge le MNS LiDAR HD par tuiles via le WCS, puis les assemble."""
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
                params = [
                    ("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"),
                    ("COVERAGEID", MNS_COVERAGE), ("FORMAT", "image/tiff"),
                    ("SUBSET", f"x({x0},{x1})"), ("SUBSET", f"y({y0},{y1})"),
                    ("SUBSETTINGCRS", CRS), ("OUTPUTCRS", CRS),
                ]
                r = requests.get(WCS_URL, params=params, timeout=300)
                if r.status_code != 200 or "tiff" not in r.headers.get("content-type", "").lower():
                    raise RuntimeError(
                        "Échec du téléchargement WCS du MNS "
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
def pvgis_interpolator(lat, lon, cache_file):
    """Grille rendement (kWh/kWc/an) en fonction de (inclinaison, azimut boussole)."""
    if cache_file.exists():
        grid = np.array(json.loads(cache_file.read_text()))
    else:
        grid = np.zeros((len(TILTS), len(AZS)))
        sess = requests.Session()
        for i, tilt in enumerate(TILTS):
            for j, az in enumerate(AZS[:-1]):
                params = dict(
                    lat=lat, lon=lon, peakpower=1, loss=14, angle=tilt,
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
    interp = RegularGridInterpolator((TILTS, AZS), grid, bounds_error=False, fill_value=None)
    return interp, float(grid.max())


# --------------------------------------------------------------------------- géométrie des toits
def read_window(src, geom, pad):
    minx, miny, maxx, maxy = geom.buffer(pad).bounds
    win = windows.from_bounds(minx, miny, maxx, maxy, src.transform).round_offsets().round_lengths()
    Z = src.read(1, window=win, boundless=True, fill_value=np.nan).astype(float)
    if src.nodata is not None:
        Z[Z == src.nodata] = np.nan
    Z[Z < -100] = np.nan
    return Z, src.window_transform(win)


def slope_aspect(Z, res):
    """Pente (°) et azimut de la ligne de plus grande pente descendante (0 = nord, horaire)."""
    drow, dcol = np.gradient(Z, res)
    dz_e, dz_n = dcol, -drow
    slope = np.degrees(np.arctan(np.hypot(dz_e, dz_n)))
    az = np.degrees(np.arctan2(-dz_e, -dz_n)) % 360
    return slope, az


def group_pans(slope, az, res):
    """Regroupe les pixels de toit en pans : classes d'azimut, fusion des classes voisines."""
    nb = 360 // PAN_BIN
    idx = (np.floor((az + PAN_BIN / 2) / PAN_BIN) % nb).astype(int)
    area_px = res * res / np.cos(np.radians(slope))
    bins = {}
    for k in range(nb):
        m = idx == k
        if not m.any():
            continue
        a = float(area_px[m].sum())
        w = area_px[m]
        bins[k] = dict(
            area=a,
            sl=float((slope[m] * w).sum()),
            s=float((np.sin(np.radians(az[m])) * w).sum()),
            c=float((np.cos(np.radians(az[m])) * w).sum()),
        )
    parent = list(range(nb))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for k in bins:
        j = (k + 1) % nb
        if j in bins and bins[k]["area"] >= 1.5 and bins[j]["area"] >= 1.5:
            if abs(bins[k]["sl"] / bins[k]["area"] - bins[j]["sl"] / bins[j]["area"]) <= 6:
                parent[find(k)] = find(j)
    groups = {}
    for k, b in bins.items():
        g = groups.setdefault(find(k), dict(area=0.0, sl=0.0, s=0.0, c=0.0))
        for key in g:
            g[key] += b[key]
    pans = []
    for g in groups.values():
        if g["area"] < MIN_PAN_M2:
            continue
        pans.append(dict(
            area=g["area"], tilt=g["sl"] / g["area"],
            az=math.degrees(math.atan2(g["s"], g["c"])) % 360,
        ))
    return pans


def shade_loss(Z, T, geom, mask, ndir_step=4):
    """Perte annuelle indicative (0-1) due aux obstacles lointains dans le secteur sud."""
    res = abs(T.a)
    z0 = np.nanmedian(Z[mask])
    c = geom.representative_point()
    azs = np.radians(np.arange(110, 251, ndir_step))
    d = np.arange(2.0, SHADE_RADIUS, 1.0)
    A, D = np.meshgrid(azs, d)
    x, y = c.x + D * np.sin(A), c.y + D * np.cos(A)
    cols = np.floor((x - T.c) / T.a).astype(int)
    rows = np.floor((y - T.f) / T.e).astype(int)
    ok = (rows >= 0) & (rows < Z.shape[0]) & (cols >= 0) & (cols < Z.shape[1])
    rows, cols = np.clip(rows, 0, Z.shape[0] - 1), np.clip(cols, 0, Z.shape[1] - 1)
    own = features.geometry_mask([geom.buffer(1.0)], out_shape=Z.shape, transform=T, invert=True)
    zz = np.where(ok & ~own[rows, cols], Z[rows, cols], np.nan)
    ang = np.degrees(np.arctan2(zz - z0, D))
    ang = np.where(np.isfinite(ang), ang, -90.0).max(axis=0)  # obstacle max par direction
    # part d'énergie annuelle bloquée selon la hauteur angulaire de l'obstacle (empirique)
    share = np.interp(ang, [0, 10, 20, 30, 45, 60], [0, 0.01, 0.07, 0.20, 0.50, 0.80])
    w = np.clip(np.cos(azs - math.pi), 0.0, None)
    return float((share * w).sum() / w.sum() * 0.9)


def analyze_building(geom, src, interp, ymax, shade=True):
    pad = SHADE_RADIUS + 5 if shade else 3
    Z, T = read_window(src, geom, pad)
    res = abs(T.a)
    roof = geom.buffer(-ERODE)
    if roof.is_empty:
        return []
    mask = features.geometry_mask([roof], out_shape=Z.shape, transform=T, invert=True)
    if mask.sum() < 10 or np.isfinite(Z[mask]).mean() < 0.7:
        return []
    slope, az = slope_aspect(Z, res)
    pitched = mask & np.isfinite(slope) & (slope >= FLAT_SLOPE) & (slope <= MAX_SLOPE)
    flat = mask & np.isfinite(slope) & (slope < FLAT_SLOPE)
    pans = group_pans(slope[pitched], az[pitched], res) if pitched.any() else []
    for p in pans:
        p["usable"], p["flat"] = USABLE_RATIO, False
    flat_area = float(flat.sum() * res * res)
    if flat_area >= 15 and flat_area > 0.5 * sum(p["area"] for p in pans):
        pans.append(dict(area=flat_area, tilt=FLAT_TILT, az=180.0, usable=FLAT_USABLE, flat=True))
    loss = shade_loss(Z, T, geom, mask) if shade else 0.0
    out = []
    for p in pans:
        y = float(interp([[min(max(p["tilt"], 0), 90), p["az"]]])[0])
        y_net = y * (1 - loss)
        kwp = p["area"] * p["usable"] / M2_PER_KWP
        out.append(dict(
            az=round(p["az"], 1), tilt=round(p["tilt"], 1), area_m2=round(p["area"], 1),
            plat=p["flat"], shade_loss=round(loss, 3),
            yield_kwh_kwp=round(y_net, 0), ratio=round(y_net / ymax, 3),
            kwp=round(kwp, 2), kwh_an=round(kwp * y_net, 0),
        ))
    return out


# --------------------------------------------------------------------------- programme
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--mns", help="GeoTIFF du MNS LiDAR HD (Lambert-93). Sinon téléchargement WCS.")
    ap.add_argument("--out", default="resultats")
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--all-usages", action="store_true", help="inclut tous les usages de bâtiment")
    ap.add_argument("--no-shade", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="n'analyse que N bâtiments (test)")
    args = ap.parse_args()
    out, cache = Path(args.out), Path(args.cache)
    out.mkdir(parents=True, exist_ok=True)

    print("Contour communal et bâtiments BD TOPO...")
    commune = fetch_commune()
    cgeom = commune.geometry.iloc[0]
    blds = fetch_buildings(cgeom)
    if not args.all_usages and "usage_1" in blds:
        blds = blds[blds["usage_1"].fillna("").str.contains("sidentiel")]
    blds = blds[blds.geometry.area >= 30].reset_index(drop=True)
    if args.limit:
        blds = blds.head(args.limit)
    print(f"{len(blds)} bâtiments à analyser")

    mns = Path(args.mns) if args.mns else download_mns(
        tuple(cgeom.buffer(60).bounds), cache / "mns")
    c4326 = commune.to_crs("EPSG:4326").geometry.iloc[0].centroid
    print("Rendements PVGIS (grille inclinaison x azimut, mise en cache)...")
    interp, ymax = pvgis_interpolator(c4326.y, c4326.x, cache / "pvgis_grille.json")
    print(f"Rendement optimal de la grille : {ymax:.0f} kWh/kWc/an")

    rows = []
    with rasterio.open(mns) as src:
        for n, (i, b) in enumerate(blds.iterrows(), 1):
            for p in analyze_building(b.geometry, src, interp, ymax, shade=not args.no_shade):
                rows.append(dict(bat_id=b.get("cleabs", i), **p))
            if n % 200 == 0:
                print(f"  {n}/{len(blds)}")
    pans = pd.DataFrame(rows)
    if pans.empty:
        print("Aucun pan détecté : vérifie l'emprise et la résolution du MNS.")
        return
    pans.to_csv(out / "pans.csv", index=False)

    good = pans[pans.ratio >= GOOD_RATIO]
    agg = pans.sort_values("ratio", ascending=False).groupby("bat_id").first()[
        ["az", "tilt", "ratio", "yield_kwh_kwp", "shade_loss"]
    ].rename(columns={"az": "meilleur_az", "tilt": "meilleure_pente", "ratio": "meilleur_ratio"})
    agg["kwh_an_bons_pans"] = good.groupby("bat_id").kwh_an.sum()
    agg["kwh_an_bons_pans"] = agg["kwh_an_bons_pans"].fillna(0)
    key = "cleabs" if "cleabs" in blds else None
    blds["bat_id"] = blds[key] if key else blds.index
    res = blds.merge(agg, left_on="bat_id", right_index=True, how="inner")
    res = res.sort_values(["meilleur_ratio", "kwh_an_bons_pans"], ascending=False)
    res.to_file(out / "batiments.gpkg", driver="GPKG")
    res.drop(columns="geometry").to_csv(out / "batiments.csv", index=False)

    print("\nTop 20 (rendement relatif puis production potentielle) :")
    cols = ["bat_id", "meilleur_az", "meilleure_pente", "meilleur_ratio",
            "yield_kwh_kwp", "kwh_an_bons_pans"]
    print(res[cols].head(20).to_string(index=False))
    print(f"\nFichiers écrits dans {out}/ : pans.csv, batiments.csv, batiments.gpkg (ouvrir dans QGIS)")


if __name__ == "__main__":
    main()
