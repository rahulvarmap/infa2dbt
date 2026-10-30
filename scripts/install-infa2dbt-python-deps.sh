#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SITE_PACKAGES="$ROOT/.pythonlibs/lib/python3.11/site-packages"
mkdir -p "$SITE_PACKAGES"

uv pip install \
  --python python3 \
  --upgrade \
  --target "$SITE_PACKAGES" \
  --overrides "$ROOT/artifacts/infa2dbt/api/security-overrides.txt" \
  -r "$ROOT/artifacts/infa2dbt/api/requirements.txt"
