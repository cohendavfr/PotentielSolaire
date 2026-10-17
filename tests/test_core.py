"""Test hors réseau de solaire_core sur une scène synthétique (toit à deux pans, arbre, voisin)."""
import math
import os
import sys

import numpy as np
from shapely.geometry import box
from shapely.ops import unary_union

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import solaire_core as c

res = 0.5
x0, y0 = 650000.0, 6850000.0
W = H = 700                                  # 350 m x 350 m
T = c.Tf(x0, res, y0 + H * res, -res)
bx0, by0, bx1, by1 = x0 + 170, y0 + 170, x0 + 190, y0 + 184   # maison 20 m x 14 m
ridge_y = (by0 + by1) / 2
Z = np.full((H, W), 90.0)
X = T.c + (np.arange(W) + 0.5) * res
Y = T.f + (np.arange(H) + 0.5) * T.e
XX, YY = np.meshgrid(X, Y)
inside = (XX >= bx0) & (XX <= bx1) & (YY >= by0) & (YY <= by1)
Z[inside] = 103 - np.abs(YY - ridge_y)[inside] * math.tan(math.radians(35))
tree = (XX >= x0 + 178) & (XX <= x0 + 182) & (YY >= y0 + 152) & (YY <= y0 + 156)   # au sud
Z[tree] = 112
geom = box(bx0, by0, bx1, by1)

grid = np.zeros((len(c.TILTS), len(c.AZS)))
for i, t in enumerate(c.TILTS):
    for j, a in enumerate(c.AZS):
        grid[i, j] = 1250 * (0.8 + 0.2 * math.cos(math.radians(a - 180)) * math.sin(math.radians(min(t, 60) * 1.5))) \
            * (1 - 0.0006 * abs(t - 32))
ym = c.YieldModel(grid)
assert abs(ym(30, 180) - grid[3, 6]) < 1e-6 and abs(ym(35, 195) - ym(35, 195 - 360)) < 1e-6

H_, Kd = np.array([28, 50, 94, 146, 164, 210, 178, 160, 110, 66, 33, 22.0]), np.array([.7, .55, .51, .42, .48, .38, .4, .42, .47, .55, .68, .72])
sun = c.make_sun_context(48.73, 2.57, H_, Kd)
assert math.degrees(sun["el"].max()) > 60 and math.degrees(sun["el"][sun["month"] == 11].max()) < 20


def run(nearby, shade=True):
    return c.analyze_building(geom, Z, T, nearby, ym, sun, shade=shade)


free = run([], shade=False)
tree_case = run([])                                  # le bloc n'est pas du bâti : arbre
house_case = run([box(x0 + 178, y0 + 152, x0 + 182, y0 + 156)])   # même bloc déclaré bâtiment
for name, r in (("sans ombrage", free), ("arbre au sud", tree_case), ("bâti au sud", house_case)):
    print(name, [(p["pan"], p["az"], p["tilt"], p["area_m2"], p["shade_loss"], p["ratio"]) for p in r["pans"]])

# deux pans distincts, nord et sud, pente 35°
assert len(free["pans"]) == 2, free["pans"]
azs = sorted(p["az"] % 360 for p in free["pans"])
assert min(azs[0], 360 - azs[0]) < 8 and abs(azs[1] - 180) < 8 or min(azs[1], 360 - azs[1]) < 8 and abs(azs[0] - 180) < 8, azs
assert abs(azs[1] - 180) < 8, azs
assert all(abs(p["tilt"] - 35) < 2.5 for p in free["pans"]), [p["tilt"] for p in free["pans"]]
full = 0.5 * 20 * 14 / math.cos(math.radians(35))      # avant recul de 0,6 m sur le pourtour
assert 0.8 * full < free["pans"][0]["area_m2"] < full
# les polygones des pans recouvrent toute l'emprise, sans chevauchement
shapes = []
from shapely.geometry import Polygon
for p in free["pans"]:
    shapes.append(unary_union([Polygon(ext, holes) for ext, holes in p["polys"]]))
assert abs(unary_union(shapes).area - geom.area) < 0.02 * geom.area
assert shapes[0].intersection(shapes[1]).area < 0.03 * geom.area
# plan : altitude du faîtage au-dessus du sol (13 m environ), et sol à 90 m
assert abs(free["zg"] - 90.0) < 0.5, free["zg"]
for p in free["pans"]:
    a, b, cc = p["plane"]
    ref = free["ref"]
    zr = cc + a * (p["x"] - ref[0]) + b * (p["y"] - ref[1])
    assert abs(zr - (p["z"] - free["zg"])) < 0.6, (zr, p["z"])

south = lambda r: min(r["pans"], key=lambda p: abs(p["az"] - 180))
assert south(free)["shade_loss"] == 0
t, h = south(tree_case), south(house_case)
assert t["shade_loss"] > 0 and t["shade_bati"] == 0 and t["shade_arbres"] > 0, t
assert h["shade_bati"] > 0 and h["shade_arbres"] == 0, h
assert h["shade_loss"] > t["shade_loss"], (h, t)
assert t["ratio"] < south(free)["ratio"]
for p_ in free["pans"]:
    assert p_["panneaux"] == int(p_["area"] * c.USABLE_RATIO / c.M2_PER_KWP / c.MODULE_KWP + 1e-9) and p_["panneaux"] > 10, p_
    assert abs(p_["kwp"] - p_["panneaux"] * c.MODULE_KWP) < 0.01
assert free["pans"][0]["pan"] == 1 and free["pans"][0]["ratio"] >= free["pans"][1]["ratio"]

# toit plat : un seul pan, plan horizontal
Zf = np.full((H, W), 90.0)
Zf[inside] = 96.0
fl = c.analyze_building(geom, Zf, T, [], ym, sun, shade=False)
assert len(fl["pans"]) == 1 and fl["pans"][0]["flat"] and fl["pans"][0]["plane"][:2] == (0.0, 0.0), fl
assert abs(fl["pans"][0]["plane"][2] - 6.0) < 0.5

# Lambert-93 : Santeny (BAN 94070 : lon 2.56972, lat 48.717265 -> x 668344.48, y 6846434.14)
x, y = c.lambert93(2.56972, 48.717265)
assert abs(x - 668344.48) < 2 and abs(y - 6846434.14) < 2, (x, y)
print("OK")
