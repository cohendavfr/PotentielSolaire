#!/usr/bin/env python3
"""
Précalcule les rendements PVGIS sur une grille couvrant la France métropolitaine, pour que la page
« adresse » (qui tourne dans le navigateur, où PVGIS est inaccessible faute d'en-têtes CORS) puisse
estimer le rendement de n'importe quel toit par interpolation.

  python tools/build_pvgis_grid.py            # écrit data/pvgis_fr.json (reprend si interrompu)
  python tools/build_pvgis_grid.py --fine     # ajoute la grille fine (pas de 0,1°, un seul rendement)

La grille grossière donne la forme du rendement selon l'inclinaison et l'azimut ; sa résolution (0,5°)
est trop faible près des côtes (les noeuds en mer n'ont pas de données) et en montagne. La grille fine
corrige le niveau : rendement d'un plan de référence (30° plein sud) tous les 0,1°, appliqué en facteur.

Pour chaque noeud (pas de 0,5°) : rendement annuel d'1 kWc (pertes 14 %) pour 10 inclinaisons x 12
azimuts, plus l'irradiation horizontale mensuelle et la part de diffus (MRcalc).
Environ 45 000 appels PVGIS : compter une à deux heures.
"""
import concurrent.futures as cf
import json
import math
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import solaire as s  # noqa: E402

LAT0, LON0, STEP = 41.0, -5.5, 0.5
NLAT, NLON = 22, 32  # 41.0..51.5 et -5.5..10.0
LOSS = 14
OUT = ROOT / "data" / "pvgis_fr.json"
PART = ROOT / "data" / "pvgis_fr.partial.json"
sess = requests.Session()


def land_nodes():
    """Noeuds nécessaires : coins des mailles contenant au moins un point de terre (geo.api.gouv.fr)."""
    pts = [(LAT0 + 0.2 * i, LON0 + 0.2 * j) for i in range(int(10.5 / 0.2) + 1) for j in range(int(15.5 / 0.2) + 1)]

    def is_land(p):
        for k in range(3):
            try:
                r = sess.get("https://geo.api.gouv.fr/communes", params=dict(lat=p[0], lon=p[1], fields="code"), timeout=30)
                if r.status_code == 200:
                    d = r.json()
                    return bool(d) and not d[0]["code"].startswith(("97", "98"))
            except Exception:
                time.sleep(1)
        return False

    with cf.ThreadPoolExecutor(8) as ex:
        land = [p for p, ok in zip(pts, ex.map(is_land, pts)) if ok]
    nodes = set()
    for lat, lon in land:
        i, j = int((lat - LAT0) // STEP), int((lon - LON0) // STEP)
        for di in (0, 1):
            for dj in (0, 1):
                if 0 <= i + di < NLAT and 0 <= j + dj < NLON:
                    nodes.add((i + di, j + dj))
    return sorted(nodes)


def get(url, params):
    for k in range(6):
        try:
            r = sess.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 400:   # « location over the sea » : noeud sans donnée
                return None
        except Exception:
            pass
        time.sleep(1.5 * (k + 1))
    raise RuntimeError(f"échec {url} {params}")


def node(ij):
    i, j = ij
    lat, lon = LAT0 + STEP * i, LON0 + STEP * j
    grid = []
    for t in s.TILTS:
        row = []
        for a in s.AZS[:-1]:
            d = get(s.PVGIS_URL, dict(lat=lat, lon=lon, peakpower=1, loss=LOSS, angle=t, aspect=a - 180, outputformat="json"))
            if d is None:
                return f"{i},{j}", None
            row.append(round(d["outputs"]["totals"]["fixed"]["E_y"]))
        grid.append(row)
    mr = get(s.PVGIS_MR_URL, dict(lat=lat, lon=lon, horirrad=1, d2g=1, outputformat="json", startyear=2015, endyear=2020))
    if mr is None:
        return f"{i},{j}", None
    H, K = [0.0] * 12, [0.0] * 12
    n = [0] * 12
    for m in mr["outputs"]["monthly"]:
        k = m["month"] - 1
        H[k] += m["H(h)_m"]; K[k] += m["Kd"]; n[k] += 1
    H = [round(h / c, 1) for h, c in zip(H, n)]
    K = [round(k / c, 3) for k, c in zip(K, n)]
    return f"{i},{j}", dict(g=grid, H=H, Kd=K)


FINE = 0.1
REF_TILT, REF_AZ = 30, 180


def fine_point(ij):
    i, j = ij
    d = get(s.PVGIS_URL, dict(lat=LAT0 + FINE * i, lon=LON0 + FINE * j, peakpower=1, loss=LOSS,
                              angle=REF_TILT, aspect=REF_AZ - 180, outputformat="json"))
    return None if d is None else round(d["outputs"]["totals"]["fixed"]["E_y"])


def fine_stage():
    data = json.loads(OUT.read_text())
    coarse = {tuple(map(int, k.split(","))) for k in data["nodes"]}
    k = round(STEP / FINE)
    nlat, nlon = int(round((NLAT - 1) * k)) + 1, int(round((NLON - 1) * k)) + 1
    todo = []
    for i in range(nlat):
        for j in range(nlon):
            ci, cj = i // k, j // k          # maille grossière contenant le point
            if any((ci + di, cj + dj) in coarse for di in (0, 1) for dj in (0, 1)):
                todo.append((i, j))
    part = ROOT / "data" / "pvgis_fine.partial.json"
    vals = {tuple(map(int, kk.split(","))): v for kk, v in json.loads(part.read_text()).items()} if part.exists() else {}
    todo = [p for p in todo if p not in vals]
    print(f"{len(todo)} points fins à calculer", flush=True)
    t0 = time.time()
    with cf.ThreadPoolExecutor(8) as ex:
        for n, (p, v) in enumerate(zip(todo, ex.map(fine_point, todo)), 1):
            vals[p] = v
            if n % 200 == 0:
                part.write_text(json.dumps({f"{a},{b}": x for (a, b), x in vals.items()}))
                print(f"  {n}/{len(todo)}  {time.time() - t0:.0f}s", flush=True)
    grid = [[-1] * nlon for _ in range(nlat)]
    for (i, j), v in vals.items():
        if v is not None:
            grid[i][j] = v
    data["fine"] = dict(step=FINE, ref=[REF_TILT, REF_AZ], grid=grid)
    OUT.write_text(json.dumps(data, separators=(",", ":")))
    part.unlink(missing_ok=True)
    print(f"écrit {OUT} ({OUT.stat().st_size / 1e3:.0f} ko)")


def main():
    if "--fine" in sys.argv:
        return fine_stage()
    done = json.loads(PART.read_text()) if PART.exists() else {}
    nodes = land_nodes()
    todo = [n for n in nodes if f"{n[0]},{n[1]}" not in done]
    print(f"{len(nodes)} noeuds, {len(todo)} à calculer", flush=True)
    t0 = time.time()
    with cf.ThreadPoolExecutor(6) as ex:
        for k, (key, val) in enumerate(ex.map(node, todo), 1):
            done[key] = val
            if k % 5 == 0:
                PART.write_text(json.dumps(done))
                print(f"  {k}/{len(todo)}  {time.time() - t0:.0f}s", flush=True)
    out = dict(lat0=LAT0, lon0=LON0, step=STEP, nlat=NLAT, nlon=NLON, loss=LOSS,
               tilts=s.TILTS, azs=s.AZS[:-1], nodes={k: v for k, v in done.items() if v})
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    PART.unlink(missing_ok=True)
    print(f"écrit {OUT} ({OUT.stat().st_size / 1e3:.0f} ko)")


if __name__ == "__main__":
    main()
