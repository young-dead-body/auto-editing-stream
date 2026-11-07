#!/usr/bin/env bash
set -euo pipefail

source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
remote="${1:-${PUBLISH_GIT_REMOTE:-}}"
if [[ -z "$remote" ]]; then
  echo 'Укажите адрес репозитория первым аргументом или в PUBLISH_GIT_REMOTE' >&2
  exit 1
fi
temporary="$(mktemp -d)"
trap 'rm -rf "$temporary"' EXIT

git clone "$remote" "$temporary/repo"

files=(
  .dockerignore .env.example .gitattributes .gitignore Dockerfile PROMPTS.md README.md
  app.py compose.yaml highlights_to_fcp7xml.py render_preview.py install.sh
  requirements-desktop.txt transcribe_stream.py analyze_highlights.py
  deep_analysis.py visual_analysis.py device_profile.py export_clips.py
)
for file in "${files[@]}"; do
  cp "$source_root/$file" "$temporary/repo/$file"
done
for folder in .github docs examples packaging tests web scripts; do
  mkdir -p "$temporary/repo/$folder"
  cp -R "$source_root/$folder/." "$temporary/repo/$folder/"
done

git -C "$temporary/repo" add -- "${files[@]}" .github docs examples packaging tests web scripts
if git -C "$temporary/repo" diff --cached --quiet; then
  echo 'В репозитории уже актуальные файлы.'
  exit 0
fi

git -C "$temporary/repo" diff --cached --stat
git -C "$temporary/repo" commit -m 'Publish Stream Editor source'
git -C "$temporary/repo" push -u origin HEAD
