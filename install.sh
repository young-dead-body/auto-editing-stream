#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo 'Установите FFmpeg (ffmpeg и ffprobe) и повторите запуск.' >&2
  exit 1
fi
python_bin="${PYTHON:-python3}"
"$python_bin" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-desktop.txt
echo 'Установка завершена. Запуск: .venv/bin/python app.py'
