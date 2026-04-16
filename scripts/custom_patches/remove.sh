#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
series_file="$repo_root/patches/custom/series"

if [[ ! -f "$series_file" ]]; then
  echo "Missing patch series: $series_file" >&2
  exit 1
fi

if ! git -C "$repo_root" diff --quiet || ! git -C "$repo_root" diff --cached --quiet; then
  echo "Tracked working tree changes exist. Commit or stash them before removing patches." >&2
  exit 1
fi

mapfile -t patches < <(grep -vE '^[[:space:]]*($|#)' "$series_file")

for ((idx=${#patches[@]} - 1; idx >= 0; idx--)); do
  patch_name="${patches[$idx]}"
  patch_path="$repo_root/patches/custom/$patch_name"
  if [[ ! -f "$patch_path" ]]; then
    echo "Missing patch: $patch_path" >&2
    exit 1
  fi
  echo "Reversing $patch_name"
  git -C "$repo_root" apply -R --3way --index "$patch_path"
done

if git -C "$repo_root" diff --cached --quiet; then
  echo "No patch changes were removed."
else
  git -C "$repo_root" commit -m "revert: remove custom patch queue changes"
fi
