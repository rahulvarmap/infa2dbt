#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SITE_PACKAGES="$ROOT/.pythonlibs/lib/python3.11/site-packages"
export PYTHONPATH="$SITE_PACKAGES${PYTHONPATH:+:$PYTHONPATH}"

cd "$ROOT"
exec python3 "$ROOT/artifacts/infa2dbt/api/serve.py"
