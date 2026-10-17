import sys, math
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box
from scipy.interpolate import RegularGridInterpolator

import os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geopandas as gpd
import solaire as s

# MNS synthétique : toit à deux pans, faîtage est-ouest, pente 35 deg, 0.5 m de résolution
res = 0.5
x0, y0 = 650000.0, 6850000.0
W, H = 200, 200  # 100 m x 100 m
Z = np.full((H, W), 90.0)
bx0, by0, bx1, by1 = x0 + 40, y0 + 40, x0 + 60, y0 + 54  # 20 m x 14 m
ridge_y = (by0 + by1) / 2
for r in range(H):
    for c in range(W):
        x = x0 + (c + 0.5) * res
        y = y0 + (H - r - 0.5) * res
        if bx0 <= x <= bx1 and by0 <= y <= by1:
            Z[r, c] = 100 + 3 - abs(y - ridge_y) * math.tan(math.radians(35))
# arbre au sud du bâtiment
for r in range(H):
    for c in range(W):
        x = x0 + (c + 0.5) * res
        y = y0 + (H - r - 0.5) * res
        if 48 <= x - x0 <= 52 and 30 <= y - y0 <= 34:
            Z[r, c] = 112
path = os.path.join(tempfile.mkdtemp(), "mns_test.tif")
with rasterio.open(path, "w", driver="GTiff", height=H, width=W, count=1, dtype="float32",
                   crs="EPSG:2154", transform=from_origin(x0, y0 + H * res, res, res)) as d:
    d.write(Z.astype("float32"), 1)

# grille de rendement factice (cos de l'écart au sud/30 deg)
grid = np.zeros((len(s.TILTS), len(s.AZS)))
for i, t in enumerate(s.TILTS):
    for j, a in enumerate(s.AZS):
        grid[i, j] = 1250 * (0.8 + 0.2 * math.cos(math.radians(a - 180)) * math.sin(math.radians(min(t, 60) * 1.5))) \
            * (1 - 0.0006 * abs(t - 32))
interp = RegularGridInterpolator((s.TILTS, s.AZS), grid, bounds_error=False, fill_value=None)

geom = box(bx0, by0, bx1, by1)
H = np.array(s.FALLBACK_H, float)
Kd = np.array(s.FALLBACK_KD, float)
sun = s.make_sun_context(48.73, 2.57, H, Kd)
# le soleil de midi en juin est haut au sud, en décembre bas
mid = sun["az"] > math.radians(170)
assert math.degrees(sun["el"].max()) > 60 and math.degrees(sun["el"][sun["month"] == 11].max()) < 20


def run(allb, shade=True):
    ctx = dict(sun=sun, allb=allb)
    with rasterio.open(path) as src:
        return s.analyze_building(geom, src, interp, float(grid.max()), ctx, shade=shade)


nobuild = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:2154")          # seulement le bâtiment lui-même
neighbour = gpd.GeoDataFrame(geometry=[geom, box(x0 + 48, y0 + 30, x0 + 52, y0 + 34)], crs="EPSG:2154")
free = run(nobuild, shade=False)
tree = run(nobuild)      # le bloc au sud n'est pas dans la couche bâti : traité comme un arbre
house = run(neighbour)   # le même bloc déclaré comme bâtiment : opaque
for name, out in (("sans ombrage", free), ("arbre au sud", tree), ("bâti au sud", house)):
    print(name)
    for p in out:
        print("  ", p)
south = lambda out: max(out, key=lambda p: p["ratio"] if abs(p["az"] - 180) < 20 else 0)
assert south(free)["shade_loss"] == 0
t, h = south(tree), south(house)
assert t["shade_loss"] > 0 and t["shade_bati"] == 0 and t["shade_arbres"] > 0, t
assert h["shade_bati"] > 0 and h["shade_arbres"] == 0, h
assert h["shade_loss"] > t["shade_loss"], (h, t)   # un mur bloque plus qu'un arbre
assert south(tree)["ratio"] < south(free)["ratio"]
print("OK")
