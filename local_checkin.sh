#!/usr/bin/env bash
# 本機補跑 AnyRouter 多帳號簽到（GitHub Actions 失敗時的備援）
#
# 用法：本機 Git Bash 執行  ./local_checkin.sh
# 前置：repo 根目錄需有一張 .env（已被 .gitignore 排除，不會進 git）
#       .env 至少含 ANYROUTER_ACCOUNTS / PROVIDERS / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
#
# GitHub Actions 已每 6 小時自動跑（關機/當機也不中斷）。
# 這支只在看到 GitHub run 失敗（例如 CF challenge）時手動執行救回當輪。
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/Scripts/python.exe"
if [ ! -x "$PY" ]; then
  # 本機 Python 位置（依現況自動偵測）
  for cand in \
    "$LOCALAPPDATA/Programs/Python/Python312/python.exe" \
    "/d/programs/python/python.exe"; do
    if [ -x "$cand" ]; then PY="$cand"; break; fi
  done
fi

if [ ! -f .env ]; then
  echo "[ERR] 缺少 .env（含憑證）。請先把 ANYROUTER_ACCOUNTS / PROVIDERS / TELEGRAM_* 填進 repo 根的 .env"
  exit 1
fi

echo "[LOCAL] 本機補跑開始：$(date '+%Y-%m-%d %H:%M:%S')"
# Windows 本機 stdout 預設 cp950，強制 utf-8 避免框線字元（━）印不出而崩潰
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1
"$PY" checkin.py
