#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
series_file="$repo_root/patches/custom/series"

if [[ ! -f "$series_file" ]]; then
  echo "Missing patch series: $series_file" >&2
  exit 1
fi

if ! git -C "$repo_root" diff --quiet || ! git -C "$repo_root" diff --cached --quiet; then
  echo "Tracked working tree changes exist. Commit or stash them before applying patches." >&2
  exit 1
fi

commit_message() {
  case "$1" in
    0001-telegram-inline-callbacks-and-markup.patch)
      echo "feat: apply telegram inline callbacks and markup patch"
      ;;
    0002-gemini-provider-and-tool-streaming.patch)
      echo "fix: apply gemini provider and tool streaming patch"
      ;;
    0003-platform-fixes-and-shell-timeout.patch)
      echo "fix: apply platform fixes and shell timeout patch"
      ;;
    0004-subagent-provider-fallback.patch)
      echo "fix: apply subagent provider fallback patch"
      ;;
    *)
      echo "chore: apply custom patch ${1%.patch}"
      ;;
  esac
}

while IFS= read -r patch_name || [[ -n "$patch_name" ]]; do
  [[ -z "$patch_name" || "$patch_name" == \#* ]] && continue
  patch_path="$repo_root/patches/custom/$patch_name"
  if [[ ! -f "$patch_path" ]]; then
    echo "Missing patch: $patch_path" >&2
    exit 1
  fi
  echo "Applying $patch_name"
  git -C "$repo_root" apply --3way --index "$patch_path"
  git -C "$repo_root" commit -m "$(commit_message "$patch_name")"
done < "$series_file"
