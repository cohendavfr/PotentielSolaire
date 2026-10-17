import sys, math
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box
from scipy.interpolate import RegularGridInterpolator

import os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import santeny_solaire as s

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
with rasterio.open(path) as src:
    for shade in (False, True):
        out = s.analyze_building(geom, src, interp, float(grid.max()), shade=shade)
        print("ombrage" if shade else "sans ombrage")
        for p in out:
            print("  ", p)
