# Toits solaires 3D

Classement des toits d'une commune française selon leur potentiel photovoltaïque (orientation, pente, ombrage des arbres et du bâti, production estimée), avec une maquette 3D interactive.

Site : https://cohendavfr.github.io/santeny-solaire/ (Santeny, Val-de-Marne, est déjà analysée).

## Saisir une adresse

La page `adresse.html` accepte n'importe quelle adresse de France métropolitaine (Base Adresse Nationale). Le calcul se fait **dans le navigateur** : le moteur Python (`solaire_core.py`, `solaire_address.py`) tourne via Pyodide (numpy + shapely), après téléchargement du bâtiment (BD TOPO), du relevé LiDAR HD et de l'orthophoto. Compter une dizaine de secondes. On obtient les pans de la toiture du bâtiment (tableau et plan 3D), l'ombrage des arbres et du bâti voisin et la production. En cliquant sur un bâtiment voisin, on peut lancer son analyse.

PVGIS n'étant pas accessible depuis un navigateur (pas d'en-têtes CORS), les rendements viennent d'une grille précalculée de la France (`data/pvgis_fr.json`, pas de 0,5°, interpolée), générée par `python tools/build_pvgis_grid.py` (environ 40 minutes, à relancer rarement).

## Bilan économique

Dans la fiche d'un bâtiment (page adresse ou page d'une ville), le tableau des pans donne le **nombre de panneaux possibles** par pan (modules de 430 Wc, environ 1,13 x 1,72 m, entiers, sur 60 % du pan ou 45 % d'un toit plat). On coche les pans à équiper (par défaut ceux à 85 % ou plus du rendement optimal) et le bilan se recalcule : puissance, production, part autoconsommée, **investissement estimé** et **gain annuel** (économie sur la facture plus vente du surplus), temps de retour.

Hypothèses par défaut (octobre 2026), toutes modifiables dans la fiche et à vérifier :
- prix du kWh évité : 0,2001 € TTC, tarif réglementé Base 6 kVA (CRE, hausse du 1er août 2026) ;
- surplus injecté (≤ 9 kWc) : 1,1 c€/kWh HT, aucune prime à l'autoconsommation (arrêté du 1er juin 2026, JO du 4 juin 2026) ; des sources donnent encore 4 c€ ;
- coût TTC de l'installation posée : 3 300 € + 1 350 €/kWc (soit environ 7 400 € pour 3 kWc, 11 400 € pour 6 kWc, 15 500 € pour 9 kWc), TVA 5,5 % jusqu'à 9 kWc, majoration TVA 20 % au-delà ;
- consommation du foyer : 5 000 kWh/an, autoconsommation estimée par `0,38 x (consommation / production)^0,7` (sans batterie).

Hors batterie, dégradation des modules, maintenance et remplacement de l'onduleur. Ces valeurs sont des ordres de grandeur, pas un devis.

## Choisir une ville

- **Sur le site** : la page d'accueil permet de chercher n'importe quelle commune de France métropolitaine. Si elle est déjà analysée, la maquette s'ouvre. Sinon, la page indique comment lancer l'analyse.
- **Depuis GitHub** : onglet *Actions* > *Analyser une ville* > *Run workflow*, puis saisir un nom, un code INSEE ou un code postal. Le résultat est publié automatiquement (10 à 40 minutes selon la taille de la commune).
- **En local** :

```
pip install -r requirements.txt
python run_commune.py Santeny
python run_commune.py "Boissy-Saint-Léger" --dep 94
python run_commune.py --insee 94070
```

Les homonymes sont départagés avec `--dep` ou `--insee`. L'analyse seule (sans maquette) se lance avec `python solaire.py <commune>`.

## Méthode

1. Contour communal (geo.api.gouv.fr) et bâtiments (BD TOPO, WFS Géoplateforme IGN).
2. Modèle numérique de surface LiDAR HD de l'IGN (MNS, 0,5 m), téléchargé par tuiles WMS. Il contient les toits, les arbres et le bâti voisin.
3. Pente, orientation et position de chaque pan de toit, calculées sur le MNS.
4. Rendement (kWh/kWc/an) interpolé sur une grille PVGIS (inclinaison x azimut, pertes système 14 % par défaut, option `--loss`).
5. **Pans de toiture** : les pixels du toit sont regroupés par orientation (classes de 20°), séparés en morceaux connexes, puis découpés en facettes planes (plan ajusté par moindres carrés, écart toléré 0,35 m). Chaque pan reçoit un polygone, un plan 3D, une pente et une orientation (celles du plan). Les pans sont ensuite étendus à toute l'emprise du bâtiment pour la maquette.
6. **Ombrage par pan** :
   - profil d'horizon complet (72 azimuts, jusqu'à 150 m) depuis le centre du pan, séparant le bâti (BD TOPO, opaque) du reste (arbres) ;
   - course du soleil sur 12 jours types, toutes les 15 minutes, pondérée par le rayonnement direct mensuel PVGIS (ciel clair recalé) ;
   - les arbres laissent passer une partie du soleil, davantage hors feuillage (55 % de novembre à mars, 15 % de mai à septembre) ;
   - le rayonnement diffus (part mensuelle PVGIS) est réduit selon la part de ciel masquée.
7. Production : surface équipable (60 % d'un pan, 45 % d'un toit plat) à 5 m² par kWc. Seuls les pans à 85 % ou plus du rendement optimal comptent dans la production potentielle.

## Sorties de l'analyse (`resultats/<INSEE>/`)

- `pans.csv` : un pan de toit par ligne (azimut, pente, surface, ombrage bâti / arbres, rendement, production).
- `pans_geo.json` : géométrie des pans (polygones et plans 3D), utilisée par la maquette.
- `batiments.csv` : classement par bâtiment.
- `batiments.gpkg` : couche à ouvrir dans QGIS.
- `meta.json` : synthèse de la commune.

## Maquette 3D

`viewer3d.py` écrit `communes/<INSEE>.html` : page (three.js, `assets/viewer.js`) avec bâtiments en 3D, toitures détaillées pan par pan, sur l'orthophoto IGN, noms de rues, adresses (Base Adresse Nationale), recherche d'adresse, couleur selon le rendement ou la production, fiche par bâtiment, navigation adaptée au pavé tactile. `site_index.py` régénère `index.html`.

## Test

- `python tests/test_core.py` : pans, plans 3D, couverture de l'emprise, ombrage (arbre contre bâti), toit plat, projection Lambert-93, sur une scène synthétique, sans réseau.
- `python tests/test_address.py` : analyse d'une adresse de bout en bout (réseau requis, nécessite `cache/94070/` ou une grille de rendement).

## Limites

Estimation indicative.
- L'ombrage ne distingue pas les essences : tout obstacle non bâti est traité comme un arbre feuillu moyen. Un conifère ou un relief proche sont sous-estimés ou surestimés selon le cas.
- Le MNS ne capte pas les obstacles proches du toit (cheminées, velux, lucarnes) ni les arbres plantés ou coupés depuis le survol.
- Le rendement affiché est celui du **meilleur pan** de chaque bâtiment, relatif au meilleur point de la grille PVGIS.
- Charpente, PLU et abords protégés sont à vérifier. Le nom de la couche BD TOPO et celui de la couche WMS peuvent évoluer côté IGN (constantes en tête de `solaire.py`).
- Couverture LiDAR HD et BD TOPO : France métropolitaine uniquement.
