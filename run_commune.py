#!/usr/bin/env python3
"""
Analyse complète d'une commune puis publication dans le site (communes/<INSEE>.html + index.html).

  python run_commune.py "Boissy-Saint-Léger" --dep 94
  python run_commune.py --insee 94070
"""
import argparse
from pathlib import Path

import solaire
import viewer3d


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("commune", nargs="?", help="nom, code INSEE ou code postal")
    ap.add_argument("--insee")
    ap.add_argument("--dep", help="département, pour départager des homonymes")
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--site", default=".", help="racine du site (index.html, communes/)")
    ap.add_argument("--max-batiments", type=int, default=0)
    ap.add_argument("--all-usages", action="store_true")
    ap.add_argument("--no-shade", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    commune = solaire.resolve_commune(args.commune, args.insee, args.dep)
    out = Path("resultats") / commune["code"]
    solaire.analyse(commune, out, args.cache, all_usages=args.all_usages, shade=not args.no_shade,
                    limit=args.limit, max_batiments=args.max_batiments)
    viewer3d.build(out, site_dir=args.site)


if __name__ == "__main__":
    main()
