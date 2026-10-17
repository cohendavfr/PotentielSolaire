# santeny-solaire

Classement des toits de Santeny (Val-de-Marne, INSEE 94069) selon leur potentiel photovoltaïque : orientation, pente, ombrage et production estimée.

## Principe

1. Contour communal (geo.api.gouv.fr) et bâtiments (BD TOPO, WFS Géoplateforme IGN).
2. Modèle numérique de surface LiDAR HD de l'IGN (MNS), téléchargé par tuiles ou fourni en GeoTIFF.
3. Pente et orientation de chaque pan de toit calculées sur le MNS.
4. Rendement (kWh/kWc/an) interpolé sur une grille PVGIS (mise en cache).
5. Perte d'ombrage lointain estimée dans le secteur sud.
6. Classement des bâtiments.

## Installation

```
pip install -r requirements.txt
```

## Utilisation

```
python santeny_solaire.py --limit 50          # test rapide
python santeny_solaire.py --out resultats     # run complet
python santeny_solaire.py --mns mns.tif       # avec un MNS local
```

Options : `--all-usages`, `--no-shade`, `--limit N`, `--cache DIR`.

Si le téléchargement WCS du MNS échoue, récupérer les dalles sur https://geoservices.ign.fr/lidarhd et passer le GeoTIFF avec `--mns`.

## Sorties

- `pans.csv` : un pan de toit par ligne (azimut, pente, surface, rendement, production).
- `batiments.csv` : classement par bâtiment.
- `batiments.gpkg` : couche à ouvrir dans QGIS.

## Test

`tests/test_synth.py` valide le calcul pente/orientation/ombrage sur un toit synthétique (sans réseau).

## Limites

Estimation indicative. Le MNS ne capte pas les obstacles proches (cheminées, velux). Charpente, PLU et abords protégés sont à vérifier. Le nom de la couche BD TOPO et l'identifiant WCS peuvent évoluer côté IGN (constantes en tête de script).
