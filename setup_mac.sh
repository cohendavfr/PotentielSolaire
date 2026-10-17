#!/bin/bash
# Installation de l'environnement sur macOS (Apple Silicon ou Intel)
set -e
cd "$(dirname "$0")"
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew est requis : https://brew.sh"; exit 1
fi
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
brew list gdal >/dev/null 2>&1 || brew install gdal
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
echo
echo "Environnement prêt. Pour l'utiliser :"
echo "  cd $(pwd) && source .venv/bin/activate"
echo "  python tests/test_synth.py      # test hors réseau"
echo "  python santeny_solaire.py --limit 50"
