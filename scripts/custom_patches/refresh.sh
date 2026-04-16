#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
source_ref="${SOURCE_REF:-custom-patch}"
base_ref="${BASE_REF:-upstream/master}"
patch_dir="$repo_root/patches/custom"

mkdir -p "$patch_dir"

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/astrbot-patch-refresh.XXXXXX")"
worktree="$tmp_dir/worktree"
cleanup() {
  git -C "$repo_root" worktree remove --force "$worktree" >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

git -C "$repo_root" rev-parse --verify "$source_ref" >/dev/null
git -C "$repo_root" rev-parse --verify "$base_ref" >/dev/null
git -C "$repo_root" worktree add --detach "$worktree" "$base_ref" >/dev/null

apply_handoff_get_extra_compat() {
  local file="$worktree/astrbot/core/astr_agent_tool_exec.py"
  if grep -q 'selected_provider_id = event.get_extra("selected_provider")' "$file"; then
    perl -0pi -e 's/selected_provider_id = event\.get_extra\("selected_provider"\)/get_extra = getattr(event, "get_extra", None)\n            selected_provider_id = (\n                get_extra("selected_provider") if callable(get_extra) else None\n            )/' "$file"
  fi

  local test_file="$worktree/tests/test_tool_loop_agent_runner.py"
  if ! grep -q 'def get_provider_by_id(self, _provider_id: str):' "$test_file"; then
    perl -0pi -e 's/(    async def get_current_chat_provider_id\(self, _umo: str\) -> str:\n        return "provider-id"\n)/$1\n    def get_provider_by_id(self, _provider_id: str):\n        return MockProvider()\n/' "$test_file"
  fi
}

make_patch() {
  local patch_name="$1"
  shift
  for commit in "$@"; do
    if ! git -C "$repo_root" merge-base --is-ancestor "$commit" "$source_ref"; then
      echo "Commit $commit is not contained in $source_ref" >&2
      exit 1
    fi
    git -C "$worktree" cherry-pick --no-commit "$commit"
  done
  if [[ "$patch_name" == "0004-subagent-provider-fallback.patch" ]]; then
    apply_handoff_get_extra_compat
  fi
  git -C "$worktree" diff --binary --full-index HEAD > "$patch_dir/$patch_name"
  git -C "$worktree" add -A
  git -C "$worktree" commit -m "refresh: $patch_name" >/dev/null
}

rm -f "$patch_dir"/*.patch

make_patch 0001-telegram-inline-callbacks-and-markup.patch \
  89a787f2 f994f35d ab8960bc b776e668 1f53065a

make_patch 0002-gemini-provider-and-tool-streaming.patch \
  396f0eda 6e9e5f88 3e701e8b b2e0a653 99ce2ec2 612b160d

make_patch 0003-platform-fixes-and-shell-timeout.patch \
  9fc1ca64 f53f0a4b 06b8395c 20ddc863 f33ab45d

make_patch 0004-subagent-provider-fallback.patch \
  846e00dd 543a3c0b

cat > "$patch_dir/series" <<'SERIES'
0001-telegram-inline-callbacks-and-markup.patch
0002-gemini-provider-and-tool-streaming.patch
0003-platform-fixes-and-shell-timeout.patch
0004-subagent-provider-fallback.patch
SERIES

echo "Refreshed custom patch queue from $source_ref onto $base_ref."
