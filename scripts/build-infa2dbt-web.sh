#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

pnpm --filter @workspace/infa2dbt run build
bash "$ROOT/scripts/install-infa2dbt-python-deps.sh"
