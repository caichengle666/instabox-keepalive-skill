#!/usr/bin/env bash
# instabox-loop-watch.sh — hook 轮询脚本：InstaBox 保活 loop 看门狗。
# keepalive.sh 自己负责检查 + 拉起（带锁、防误杀），健康时静默退出。
# 本脚本只在 keepalive.sh 返回非 0（自动恢复失败）时才 wake，平时零 token 消耗。
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

ROOT="$HOME/workspace/instabox-keepalive"

if bash "$ROOT/keepalive.sh" >/dev/null 2>&1; then
  silent "instabox loop healthy" '{}'
else
  wake "InstaBox 保活 loop 不健康且 keepalive.sh 自动恢复失败" '{"daemon":"instabox-loop","root":"~/workspace/instabox-keepalive"}'
fi
