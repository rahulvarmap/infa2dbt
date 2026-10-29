#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SITE_PACKAGES="$ROOT/.pythonlibs/lib/python3.11/site-packages"
mkdir -p "$SITE_PACKAGES"

python3 -m pip install \
  --disable-pip-version-check \
  --no-warn-script-location \
  --upgrade \
  --target "$SITE_PACKAGES" \
  -r "$ROOT/artifacts/infa2dbt/api/requirements.txt"
