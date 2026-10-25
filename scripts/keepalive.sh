#!/usr/bin/env bash
# keepalive.sh — loop 自查自拉起。
# 退出 0：loop 健康，或已成功拉起；退出 1：自动恢复失败（由 hook 叫醒 agent）。
# 判断标准：pidfile 存在、/proc/<pid>/cmdline 含 loop.sh 绝对路径、心跳 20 分钟内新鲜。
# 拉起段用 flock 加锁，防重叠轮询拉起双实例。绝不用 pkill -f 盲杀。
set -u
ROOT="$HOME/workspace/instabox-keepalive"
PIDFILE="$ROOT/loop.pid"
HB="$ROOT/heartbeat"
LOCK="$ROOT/keepalive.lock"

alive() {
  local pid last now
  [ -f "$PIDFILE" ] || return 1
  pid=$(cat "$PIDFILE")
  [ -n "$pid" ] || return 1
  [ -d "/proc/$pid" ] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q "instabox-keepalive/loop.sh" || return 1
  [ -f "$HB" ] || return 1
  last=$(cat "$HB")
  now=$(date +%s)
  [ $((now - last)) -lt 1200 ] || return 1
  return 0
}

if alive; then exit 0; fi

exec 9>"$LOCK"
if ! flock -n 9; then
  sleep 2
  alive && exit 0 || exit 1
fi

# 锁内再确认一次，避免两个 keepalive 同时判定死亡
if alive; then exit 0; fi

# 只杀 cmdline 能对上的旧 loop，绝不盲杀
if [ -f "$PIDFILE" ]; then
  pid=$(cat "$PIDFILE")
  if [ -d "/proc/$pid" ] && tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q "instabox-keepalive/loop.sh"; then
    kill "$pid" 2>/dev/null || true
    sleep 2
  fi
fi

# 关键：9>&- 关掉 loop 继承的锁 fd，否则 loop 活着时锁永不释放，
# loop 死后它残留的 sleep 子进程还会继续握着锁，导致下次 keepalive 拿不到锁。
setsid nohup bash "$ROOT/loop.sh" 9>&- > "$ROOT/loop.out" 2>&1 < /dev/null &
sleep 5
alive && exit 0 || exit 1
