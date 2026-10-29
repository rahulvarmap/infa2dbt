#!/bin/bash
set -e
pnpm install --frozen-lockfile
bash scripts/install-infa2dbt-python-deps.sh
