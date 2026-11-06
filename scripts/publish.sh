#!/usr/bin/env bash
set -euo pipefail

source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
remote="git@github.com:young-dead-body/auto-editing-stream.git"
ssh_key="${HOME}/.ssh/github_id25519"
if [[ ! -f "$ssh_key" ]]; then
  echo "Не найден SSH-ключ: $ssh_key" >&2
  exit 1
fi
export GIT_SSH_COMMAND="ssh -i \"$ssh_key\" -o IdentitiesOnly=yes"
temporary="$(mktemp -d)"
trap 'rm -rf "$temporary"' EXIT

git clone "$remote" "$temporary/repo"

files=(
  .dockerignore .gitattributes .gitignore Dockerfile PROMPTS.md README.md
  app.py compose.yaml highlights_to_fcp7xml.py render_preview.py
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
