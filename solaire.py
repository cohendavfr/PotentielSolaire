#!/usr/bin/env python3
"""
Classement des toits d'une commune française pour le photovoltaïque.

Principe
  1. Contour de la commune (geo.api.gouv.fr) et bâtiments (BD TOPO, WFS Géoplateforme IGN).
  2. Modèle numérique de surface LiDAR HD (MNS) : GeoTIFF local (--mns) ou téléchargement par
     tuiles via le WMS de la Géoplateforme. Le MNS contient les toits, les arbres et le bâti voisin.
  3. Par bâtiment : pente, orientation et position de chaque pan de toit, calculées sur le MNS.
  4. Rendement (kWh/kWc/an) interpolé sur une grille de calculs PVGIS (cachée sur disque).
  5. Ombrage par pan : profil d'horizon complet (72 azimuts, jusqu'à 150 m) séparant bâti et
     végétation, croisé avec la course du soleil (12 jours types x 24 h), pondérée par le
     rayonnement direct mensuel de PVGIS. Les arbres laissent passer une partie du soleil hors
     feuillage. Le rayonnement diffus est réduit selon la part de ciel masquée.
  6. Sorties : pans.csv, batiments.gpkg, batiments.csv, meta.json + top 20 dans la console.

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
from rasterio import features, windows
from rasterio.merge import merge
from scipy.interpolate import RegularGridInterpolator

# --------------------------------------------------------------------------- constantes
CRS = "EPSG:2154"  # Lambert-93
GEO_API = "https://geo.api.gouv.fr/communes"
WFS_URL = "https://data.geopf.fr/wfs/ows"
WMS_URL = "https://data.geopf.fr/wms-r/wms"
MNS_LAYER = "IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"
MNS_RES = 0.5  # résolution demandée (m)
PVGIS_URL = "https://re.jrc.ec.europa.eu/api/v5_3/PVcalc"
PVGIS_MR_URL = "https://re.jrc.ec.europa.eu/api/v5_3/MRcalc"

FLAT_SLOPE = 8.0       # en dessous : toit considéré plat (°)
MAX_SLOPE = 60.0       # au dessus : façade / bruit
ERODE = 0.6            # recul par rapport à l'emprise pour éviter murs et débords (m)
MIN_PAN_M2 = 6.0       # pan minimal retenu
USABLE_RATIO = 0.6     # part du pan réellement équipable (rives, cheminées, velux)
FLAT_USABLE = 0.45     # idem toit plat (espacement des rangées)
FLAT_TILT = 15.0       # inclinaison supposée des structures sur toit plat
M2_PER_KWP = 5.0       # surface de panneaux par kWc
PAN_BIN = 20           # largeur des classes d'azimut pour regrouper les pans (°)
GOOD_RATIO = 0.85      # un pan est "bon" s'il fait au moins 85 % du rendement optimal
LOSS_DEFAULT = 14.0    # pertes système PVGIS (%) : câbles, onduleur, salissures...

# ombrage
HORIZON_RADIUS = 150.0  # distance de recherche des obstacles (m)
HORIZON_AZ = np.arange(0, 360, 5)  # azimuts du profil d'horizon (°, 0 = nord)
# Part du rayonnement direct qui traverse un obstacle non bâti (arbre), par mois.
# Hors feuillage (nov-mars) les branches laissent passer plus de soleil.
TREE_T_DIRECT = np.array([.55, .55, .50, .35, .15, .15, .15, .15, .15, .35, .55, .55])
TREE_T_DIFFUSE = 0.40   # idem pour le rayonnement diffus, moyenne annuelle
MID_MONTH_DOY = [15, 46, 74, 105, 135, 166, 196, 227, 258, 288, 319, 349]
# valeurs de repli si PVGIS ne répond pas (climat tempéré du nord de la France)
FALLBACK_H = [28, 50, 94, 146, 164, 210, 178, 160, 110, 66, 33, 22]
FALLBACK_KD = [.70, .55, .51, .42, .48, .38, .40, .42, .47, .55, .68, .72]

TILTS = list(range(0, 91, 10))
AZS = list(range(0, 361, 30))  # azimut boussole : 0 = nord, 180 = sud


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
def pvgis_interpolator(lat, lon, cache_file, loss=LOSS_DEFAULT):
    """Grille rendement (kWh/kWc/an) en fonction de (inclinaison, azimut boussole)."""
    if cache_file.exists():
        grid = np.array(json.loads(cache_file.read_text()))
    else:
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
    interp = RegularGridInterpolator((TILTS, AZS), grid, bounds_error=False, fill_value=None)
    return interp, float(grid.max())


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


# --------------------------------------------------------------------------- soleil
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
            a, e = solar_position(lat, lon, doy, h)
            if e > math.radians(3):
                air = 1.0 / (math.sin(e) + 0.50572 * (math.degrees(e) + 6.07995) ** -1.6364)
                dni = 1353.0 * 0.7 ** (air ** 0.678)           # ciel clair (Meinel)
                pts.append((a, e, dni * step_h))
        if not pts:
            continue
        horiz = sum(d * math.sin(e) for _, e, d in pts)        # énergie sur le plan horizontal
        scale = H[m] * (1 - Kd[m]) / horiz                      # recalage sur le direct PVGIS du mois
        for a, e, d in pts:
            az.append(a); el.append(e); mo.append(m); w.append(d * scale)
    sky_el = np.arange(1.5, 90, 3.0)
    return dict(
        az=np.array(az), el=np.array(el), month=np.array(mo), w=np.array(w),
        diffuse=float((H * Kd).sum()),
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


def shade_losses(Hb, Hv, tilt, az, ctx):
    """(perte totale, perte due au seul bâti) en fraction de la production annuelle du pan."""
    s = ctx["sun"]
    bt, a = math.radians(tilt), math.radians(az)
    cosinc = math.cos(bt) * np.sin(s["el"]) + math.sin(bt) * np.cos(s["el"]) * np.cos(s["az"] - a)
    wd = s["w"] * np.clip(cosinc, 0, None)
    saz = np.degrees(s["az"])
    el = np.degrees(s["el"])
    hb, hv = _circ(Hb, saz), _circ(Hv, saz)
    tm = TREE_T_DIRECT[s["month"]]
    t_tot = np.where(el < hb, 0.0, np.where(el < hv, tm, 1.0))
    t_bat = np.where(el < hb, 0.0, 1.0)
    # rayonnement diffus : isotrope, réduit selon la part de ciel masquée vue par le pan
    EL = s["sky_el"][None, :]
    AZ = s["sky_az"][:, None]
    g = np.clip(math.cos(bt) * np.sin(EL) + math.sin(bt) * np.cos(EL) * np.cos(AZ - a), 0, None) * np.cos(EL)
    elg = s["sky_el_deg"][None, :]
    d_tot = np.where(elg < Hb[:, None], 0.0, np.where(elg < Hv[:, None], TREE_T_DIFFUSE, 1.0))
    d_bat = np.where(elg < Hb[:, None], 0.0, 1.0)
    gs = g.sum()
    dif_tot = float((g * d_tot).sum() / gs) if gs > 0 else 1.0
    dif_bat = float((g * d_bat).sum() / gs) if gs > 0 else 1.0
    D = s["diffuse"] * (1 + math.cos(bt)) / 2
    den = wd.sum() + D
    if den <= 0:
        return 0.0, 0.0
    return (float(1 - ((wd * t_tot).sum() + D * dif_tot) / den),
            float(1 - ((wd * t_bat).sum() + D * dif_bat) / den))


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


def group_pans(slope, az, res, x, y, z):
    """Regroupe les pixels de toit en pans : classes d'azimut, fusion des classes voisines."""
    nb = 360 // PAN_BIN
    idx = (np.floor((az + PAN_BIN / 2) / PAN_BIN) % nb).astype(int)
    area_px = res * res / np.cos(np.radians(slope))
    bins = {}
    for k in range(nb):
        m = idx == k
        if not m.any():
            continue
        w = area_px[m]
        bins[k] = dict(
            area=float(w.sum()),
            sl=float((slope[m] * w).sum()),
            s=float((np.sin(np.radians(az[m])) * w).sum()),
            c=float((np.cos(np.radians(az[m])) * w).sum()),
            x=float((x[m] * w).sum()), y=float((y[m] * w).sum()), z=float((z[m] * w).sum()),
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
        g = groups.setdefault(find(k), {key: 0.0 for key in b})
        for key in g:
            g[key] += b[key]
    pans = []
    for g in groups.values():
        if g["area"] < MIN_PAN_M2:
            continue
        pans.append(dict(
            area=g["area"], tilt=g["sl"] / g["area"],
            az=math.degrees(math.atan2(g["s"], g["c"])) % 360,
            x=g["x"] / g["area"], y=g["y"] / g["area"], z=g["z"] / g["area"],
        ))
    return pans


def building_mask(Z, T, allb, geom):
    """Rasterise le bâti voisin (obstacles « bâtiment ») et l'emprise du bâtiment étudié."""
    minx, miny = T.c, T.f + T.e * Z.shape[0]
    maxx, maxy = T.c + T.a * Z.shape[1], T.f
    ids = allb.sindex.query(shapely.box(minx, miny, maxx, maxy))
    shapes = [g.buffer(0.8) for g in allb.geometry.iloc[ids]]
    if shapes:
        bmask = features.geometry_mask(shapes, out_shape=Z.shape, transform=T, invert=True)
    else:
        bmask = np.zeros(Z.shape, bool)
    own = features.geometry_mask([geom.buffer(1.0)], out_shape=Z.shape, transform=T, invert=True)
    return bmask, own


def analyze_building(geom, src, interp, ymax, ctx=None, shade=True):
    """Pans d'un bâtiment. ctx = dict(sun=..., allb=GeoDataFrame du bâti alentour)."""
    Z, T = read_window(src, geom, 3)
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

    def coords(m):
        r, c = np.nonzero(m)
        return T.c + (c + 0.5) * T.a, T.f + (r + 0.5) * T.e, Z[m]

    pans = []
    if pitched.any():
        x, y, z = coords(pitched)
        pans = group_pans(slope[pitched], az[pitched], res, x, y, z)
    for p in pans:
        p["usable"], p["flat"] = USABLE_RATIO, False
    flat_area = float(flat.sum() * res * res)
    if flat_area >= 15 and flat_area > 0.5 * sum(p["area"] for p in pans):
        x, y, z = coords(flat)
        pans.append(dict(area=flat_area, tilt=FLAT_TILT, az=180.0, usable=FLAT_USABLE, flat=True,
                         x=float(np.nanmean(x)), y=float(np.nanmean(y)), z=float(np.nanmean(z))))
    if not pans:
        return []

    if shade and ctx is not None:
        Zb, Tb = read_window(src, geom, HORIZON_RADIUS + 5)
        bmask, own = building_mask(Zb, Tb, ctx["allb"], geom)
    out = []
    for p in pans:
        loss = loss_b = 0.0
        if shade and ctx is not None:
            Hb, Hv = horizon_profile(Zb, Tb, bmask, own, p["x"], p["y"], p["z"])
            loss, loss_b = shade_losses(Hb, Hv, min(max(p["tilt"], 0), 90), p["az"], ctx)
        y = float(interp([[min(max(p["tilt"], 0), 90), p["az"]]])[0])
        y_net = y * (1 - loss)
        kwp = p["area"] * p["usable"] / M2_PER_KWP
        out.append(dict(
            az=round(p["az"], 1), tilt=round(p["tilt"], 1), area_m2=round(p["area"], 1),
            plat=p["flat"], shade_loss=round(loss, 3), shade_bati=round(loss_b, 3),
            shade_arbres=round(max(loss - loss_b, 0.0), 3),
            yield_kwh_kwp=round(y_net, 0), ratio=round(y_net / ymax, 3),
            kwp=round(kwp, 2), kwh_an=round(kwp * y_net, 0),
        ))
    return out


# --------------------------------------------------------------------------- programme
def analyse(commune, out, cache, mns=None, all_usages=False, shade=True, limit=0,
            loss=LOSS_DEFAULT, max_batiments=0):
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
    print("Rendements PVGIS (grille inclinaison x azimut, mise en cache)...")
    interp, ymax = pvgis_interpolator(lat, lon, cache / f"pvgis_grille_l{loss:g}.json", loss)
    print(f"Rendement optimal de la grille : {ymax:.0f} kWh/kWc/an")
    H, Kd = pvgis_climate(lat, lon, cache / "pvgis_climat.json")
    ctx = dict(sun=make_sun_context(lat, lon, H, Kd), allb=allb) if shade else None
    if shade:
        print(f"Part de rayonnement diffus : {(H * Kd).sum() / H.sum():.0%} (PVGIS)")

    rows = []
    with rasterio.open(mns) as src:
        for n, (i, b) in enumerate(blds.iterrows(), 1):
            for p in analyze_building(b.geometry, src, interp, ymax, ctx, shade=shade):
                rows.append(dict(bat_id=b.get("cleabs", i), **p))
            if n % 100 == 0:
                print(f"  {n}/{len(blds)}", flush=True)
    pans = pd.DataFrame(rows)
    if pans.empty:
        raise SystemExit("Aucun pan détecté : le LiDAR HD couvre-t-il cette commune ?")
    pans.to_csv(out / "pans.csv", index=False)

    good = pans[pans.ratio >= GOOD_RATIO]
    agg = pans.sort_values("ratio", ascending=False).groupby("bat_id").first()[
        ["az", "tilt", "ratio", "yield_kwh_kwp", "shade_loss", "shade_bati", "shade_arbres"]
    ].rename(columns={"az": "meilleur_az", "tilt": "meilleure_pente", "ratio": "meilleur_ratio"})
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
        lat=round(lat, 5), lon=round(lon, 5), batiments=int(len(res)),
        ratio_median=round(float(res.meilleur_ratio.median()), 3),
        ombrage_moyen=round(float(res.shade_loss.mean()), 3),
        ombrage_arbres_moyen=round(float(res.shade_arbres.mean()), 3),
        kwc_bons_pans=round(float(res.kwp_bons_pans.sum()), 0),
        gwh_an_bons_pans=round(float(res.kwh_an_bons_pans.sum()) / 1e6, 2),
        rendement_optimal=round(ymax, 0), pertes_systeme=loss,
        ombrage="complet" if shade else "aucun",
        date=datetime.date.today().isoformat(),
    )
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))

    print("\nTop 20 (rendement relatif puis production potentielle) :")
    cols = ["bat_id", "meilleur_az", "meilleure_pente", "meilleur_ratio",
            "yield_kwh_kwp", "shade_loss", "kwh_an_bons_pans"]
    print(res[cols].head(20).to_string(index=False))
    print(f"\nFichiers écrits dans {out}/ : pans.csv, batiments.csv, batiments.gpkg (QGIS), meta.json")
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
    ap.add_argument("--limit", type=int, default=0, help="n'analyse que N bâtiments (test)")
    ap.add_argument("--max-batiments", type=int, default=0, help="refuse les communes plus grosses")
    args = ap.parse_args(argv)
    commune = resolve_commune(args.commune, args.insee, args.dep)
    out = args.out or str(Path("resultats") / commune["code"])
    return analyse(commune, out, args.cache, mns=args.mns, all_usages=args.all_usages,
                   shade=not args.no_shade, limit=args.limit, loss=args.loss,
                   max_batiments=args.max_batiments)


if __name__ == "__main__":
    main()
