#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
canonical_root="$repo_root/skills"

mkdir -p "$canonical_root"
mkdir -p "$HOME/.codex/skills" "$HOME/.agents/skills" "$HOME/.claude/skills"

for skill_dir in "$canonical_root"/*; do
  [ -d "$skill_dir" ] || continue
  skill_name="$(basename "$skill_dir")"
  for target_root in "$HOME/.codex/skills" "$HOME/.agents/skills" "$HOME/.claude/skills"; do
    link_path="$target_root/$skill_name"
    if [ -L "$link_path" ]; then
      rm -f "$link_path"
    elif [ -e "$link_path" ]; then
      echo "Skip existing non-link path: $link_path"
      continue
    fi
    ln -s "$skill_dir" "$link_path"
    echo "Linked $link_path -> $skill_dir"
  done
done
