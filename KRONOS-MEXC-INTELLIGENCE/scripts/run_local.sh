#!/usr/bin/env bash
set -euo pipefail
cp -n .env.example .env || true
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,inference]'
npm --prefix frontend install
npm --prefix frontend run build
python scripts/build_frontend.py
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000
