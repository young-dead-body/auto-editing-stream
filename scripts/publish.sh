#!/usr/bin/env bash
set -euo pipefail

source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
remote="${1:-${KIPOFOX_GIT_REMOTE:-}}"
if [[ -z "$remote" ]]; then
  echo 'Укажите адрес репозитория первым аргументом или в KIPOFOX_GIT_REMOTE' >&2
  exit 1
fi
temporary="$(mktemp -d)"
trap 'rm -rf "$temporary"' EXIT

git clone "$remote" "$temporary/repo"

files=(
  .dockerignore .gitattributes .gitignore Dockerfile PROMPTS.md README.md
  app.py compose.yaml highlights_to_fcp7xml.py render_preview.py install.sh
  requirements-desktop.txt transcribe_stream.py
)
for file in "${files[@]}"; do
  cp "$source_root/$file" "$temporary/repo/$file"
done
for folder in .github examples packaging tests web scripts; do
  mkdir -p "$temporary/repo/$folder"
  cp -R "$source_root/$folder/." "$temporary/repo/$folder/"
done

git -C "$temporary/repo" add -- "${files[@]}" .github examples packaging tests web scripts
if git -C "$temporary/repo" diff --cached --quiet; then
  echo 'В репозитории уже актуальные файлы.'
  exit 0
fi

git -C "$temporary/repo" diff --cached --stat
git -C "$temporary/repo" commit -m 'Publish Kipofox Studio source'
git -C "$temporary/repo" push -u origin HEAD
