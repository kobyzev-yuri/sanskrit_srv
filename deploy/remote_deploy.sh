#!/usr/bin/env bash
# Runs on the VPS after code sync. Does not touch .env / data / storage.
set -euo pipefail

APP_ROOT=/opt/sanskrit_srv
cd "$APP_ROOT"

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r backend/requirements.txt

# PDF export is HTML → docx → LibreOffice. The API cgroup is 400MB, so
# soffice is started later as its own unit. Noto Serif Devanagari is the face
# the docx names for complex script.
if ! command -v soffice >/dev/null 2>&1 || ! fc-list 2>/dev/null | grep -q "Noto Serif Devanagari"; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends libreoffice-writer fonts-noto-core
  fc-cache -f >/dev/null 2>&1 || true
fi

mkdir -p data storage
if [[ ! -f .env ]]; then
  echo "WARN: $APP_ROOT/.env missing — copy secrets via scp before serving traffic" >&2
fi

install -m 644 deploy/sanskrit-srv.service /etc/systemd/system/sanskrit-srv.service
install -m 644 deploy/sanskrit-worker.service /etc/systemd/system/sanskrit-worker.service
systemctl daemon-reload
systemctl enable sanskrit-srv sanskrit-worker
systemctl restart sanskrit-srv
systemctl restart sanskrit-worker
sleep 2
systemctl --no-pager --full status sanskrit-srv || true
systemctl --no-pager --full status sanskrit-worker || true
curl -fsS http://127.0.0.1:8000/health || true
echo
echo "Deploy done."
