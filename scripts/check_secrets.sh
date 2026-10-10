#!/usr/bin/env bash
# 追跡対象ファイルに秘密情報らしき文字列が含まれていないか検査する（certificate C8）
set -euo pipefail
pattern='(sk-ant-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)'
if git ls-files -z | xargs -0 grep -IEn "$pattern" -- 2>/dev/null; then
  echo "秘密情報らしき文字列が見つかりました" >&2
  exit 1
fi
echo "check-secrets: ok"
