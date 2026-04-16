#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
series_file="$repo_root/patches/custom/series"

if [[ ! -f "$series_file" ]]; then
  echo "Missing patch series: $series_file" >&2
  exit 1
fi

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/astrbot-patch-check.XXXXXX")"
worktree="$tmp_dir/worktree"
cleanup() {
  git -C "$repo_root" worktree remove --force "$worktree" >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

git -C "$repo_root" worktree add --detach "$worktree" HEAD >/dev/null

while IFS= read -r patch_name || [[ -n "$patch_name" ]]; do
  [[ -z "$patch_name" || "$patch_name" == \#* ]] && continue
  patch_path="$repo_root/patches/custom/$patch_name"
  if [[ ! -f "$patch_path" ]]; then
    echo "Missing patch: $patch_path" >&2
    exit 1
  fi
  echo "Checking $patch_name"
  git -C "$worktree" apply --3way --index "$patch_path"
  git -C "$worktree" commit -m "check: $patch_name" >/dev/null
done < "$series_file"

echo "Patch queue applies cleanly."
