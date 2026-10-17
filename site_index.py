#!/usr/bin/env python3
"""Régénère index.html (choix de la ville) à partir de communes/*.json."""
import json
import os
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY", "cohendavfr/santeny-solaire")


def build_index(site_dir="."):
    site = Path(site_dir)
    communes = []
    for f in sorted((site / "communes").glob("*.json")):
        try:
            communes.append(json.loads(f.read_text()))
        except Exception:
            pass
    communes.sort(key=lambda c: c["nom"])
    tpl = Path(__file__).with_name("index_template.html").read_text()
    html = (tpl.replace("__COMMUNES__", json.dumps(communes, ensure_ascii=False))
               .replace("__REPO__", REPO))
    (site / "index.html").write_text(html)
    # ancien lien direct vers la maquette de Santeny
    if (site / "communes" / "94070.html").exists():
        (site / "santeny_3d.html").write_text(
            '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=communes/94070.html">'
            '<a href="communes/94070.html">Santeny</a>')
    return len(communes)


if __name__ == "__main__":
    print(build_index(), "commune(s)")
