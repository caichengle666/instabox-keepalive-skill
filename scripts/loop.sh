#!/usr/bin/env bash
# loop.sh — InstaBox 保活常驻 loop，零 token。给每个账号跑一拍 instabox_tick.py，然后睡到最早到期的机器
# （最多 60 秒）。没到期的账号不调 API；平时约每 10 分钟一次，只在冷却结束前后变成每 5 秒。
# 部署时改 CREDS（每个账号一个凭证名）。必须用绝对路径启动，keepalive 靠 cmdline 认进程。
set -u
ROOT="$HOME/workspace/instabox-keepalive"
CREDS=(custom.instabox custom.instabox-another-account)
LOG="$ROOT/loop.log"
mkdir -p "$ROOT"
echo $$ > "$ROOT/loop.pid"

while true; do
  for c in "${CREDS[@]}"; do
    date +%s > "$ROOT/heartbeat"   # 每个账号前都写，轮换等待叠加 API 变慢也不会被误判成死
    INSTABOX_ROOT="$ROOT" python3 "$ROOT/instabox_tick.py" "$c" >> "$LOG" 2>&1
  done
  # 只在有动作或出错时才写日志；留最近 200 行
  [ "$(wc -l < "$LOG")" -gt 200 ] && tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  wake=$(cat "$ROOT/next_wake" 2>/dev/null); now=$(date +%s)
  s=$(( ${wake:-0} - now )); [ "$s" -gt 60 ] && s=60; [ "$s" -lt 1 ] && s=1
  sleep "$s"
done
