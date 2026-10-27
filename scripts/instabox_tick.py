#!/usr/bin/env python3
"""InstaBox 保活一拍。loop 每 60 秒调一次，按到期时间决定这一拍做什么，没到期就不调 API。

时间设定（免费档：闲置 30 分钟休眠，连续跑 480 分钟强制暂停，之后冷却 10 分钟）：
- 每 10 分钟 exec 一次刷活跃：离 30 分钟闲置阈值还够漏跑一轮。
- 跑满 7h50m 主动 suspend→resume：平台按 resume 时刻重新记 runStartedAt，手动暂停没有冷却，停机约 10 秒，进程保留。
  INSTABOX_ROTATE=restart 改用 stop→start 冷启（进程会丢，靠 systemd 自启 + watchdog.sh 拉回），一般用不上。
- 每次 exec 跑的是机器里的 /data/watchdog.sh（检查并拉起服务），没有这个脚本就跑 date；exec 既刷活跃又当看门狗。
- 兜底：真被强制暂停了，按平台返回的 cooldownUntil 提前 10 秒开始唤醒，没成功就每 5 秒再试，2 分钟后改每分钟。
- 计时以平台 runStartedAt 为准：别处（手动、别的脚本）轮换过，本地记录跟着刷新，不会多轮换一次。
- 每拍结束写 next_wake（所有账号所有机器里最早的未来到期时间，最晚 60 秒后），loop 睡到那一刻，多台机器各按各的点精确触发。
  读列表失败、凭证错误、账号下没机器也照写，loop 不会空转狂打 API。

用法: instabox_tick.py <credential_name> [--now]   --now 忽略到期时间，立刻全量巡检。
退出码: 0 正常；1 有机器出错；3 凭证错误。
"""
import fcntl
import json
import os
import sys
import time
from datetime import datetime

from ib_api import ApiError, DynamicCredentialError, call

EXEC_EVERY = 10 * 60
ROTATE_AT = 470 * 60
WAKE_EARLY = 10        # 冷却结束前 10 秒开始唤醒
RETRY_FAST = 5         # 冷却窗口内失败后每 5 秒重试
ROTATE = os.environ.get("INSTABOX_ROTATE", "suspend")
EXEC_CMD = "if [ -x /data/watchdog.sh ]; then sudo /data/watchdog.sh; else date; fi"
TRANSIENT = {"creating", "starting", "stopping", "suspending", "resuming", "deleting"}
ROOT = os.path.expanduser(os.environ.get("INSTABOX_ROOT", "~/workspace/instabox-keepalive"))


def epoch(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def decide(m, st, now):
    """单台机器的决策（纯函数，只改 st）。返回 (动作 or None, 下次到期时间)。"""
    state = m.get("state")
    api_run0 = epoch(m.get("runStartedAt"))
    if state == "running":
        if st.get("state", "running") != "running":   # 刚被唤醒/轮换：先跑一次看门狗
            st.pop("rotating", None)
            st.pop("lastExec", None)
            st["run0"] = max(api_run0 or 0, now)        # 平台没刷新计时就按本地时间算
        # 平台的 runStartedAt 更新了（别处轮换过）就跟上；第一次见到也信平台
        st["run0"] = max(st.get("run0") or 0, api_run0 or 0) or now
        st["state"] = "running"
        if now - st["run0"] >= ROTATE_AT:
            return "rotate", now + 60
        if now - st.get("lastExec", 0) >= EXEC_EVERY:
            return "exec", min(now + EXEC_EVERY, st["run0"] + ROTATE_AT)
        return None, min(st.get("lastExec", 0) + EXEC_EVERY, st["run0"] + ROTATE_AT)
    if state in ("suspended", "stopped"):
        st["state"] = state
        action = "resume" if state == "suspended" else "start"
        cool = epoch(m.get("cooldownUntil"))
        if cool and now < cool - WAKE_EARLY:              # 强制暂停的冷却期：等到结束前 10 秒
            return None, cool - WAKE_EARLY
        return action, (now + RETRY_FAST if cool and now < cool + 120 else now + 60)
    if state in TRANSIENT:
        return None, now + 60
    return "error", now + EXEC_EVERY


def wait_state(mid, cred, want, limit=90):
    end = time.time() + limit
    while time.time() < end:
        time.sleep(3)
        try:
            if call("GET", f"/v1/machines/{mid}", cred).get("state") == want:
                return True
        except ApiError:              # 单次查询抖动不打断轮换，继续等到超时
            pass
    return False


def act(action, m, st, cred, now):
    mid, name = m["id"], m.get("name", m["id"])
    if action == "exec":
        r = call("POST", f"/v1/machines/{mid}/exec", cred, {"cmd": EXEC_CMD, "timeoutSeconds": 120}, timeout=150)
        if r.get("exitCode") != 0:
            raise ApiError(f"exec 退出码 {r.get('exitCode')}: {r.get('stderr')}")
        st["lastExec"] = now
        return None
    if action == "rotate":
        st["rotating"] = True
        down, paused = ("stop", "stopped") if ROTATE == "restart" else ("suspend", "suspended")
        call("POST", f"/v1/machines/{mid}/{down}", cred, {"timeoutSeconds": 60} if down == "stop" else None)
        if not wait_state(mid, cred, paused, limit=150):
            raise ApiError(f"主动 {down} 150 秒内没完成，下一拍继续")
        st["state"] = paused             # 下一拍看到 running 就按刚唤醒处理
        action = "start" if down == "stop" else "resume"
    if action in ("resume", "start"):
        call("POST", f"/v1/machines/{mid}/{action}", cred)
        return f"{name} 已发送 {action}" + ("（7h50m 主动轮换）" if st.get("rotating") else "")
    raise ApiError(f"状态 {m.get('state')} 无法自动处理，需人工")


TIME_KEYS = ("run0", "lastExec", "next")


def write_wake(book, now):
    """next_wake = 所有账号未来最早的到期点，最晚 now+60；绝不写空或过去的时间。"""
    nxt = [r["next"] for a in book.values() for r in a.values() if r.get("next", 0) > now]
    with open(os.path.join(ROOT, "next_wake"), "w") as f:
        f.write(str(int(min(nxt + [now + 60]))))


def load_book(path):
    """machines.json：按账号分组、按机器名记录，每台只留最近一次的时间（人能直接看）。"""
    try:
        with open(path) as f:
            book = json.load(f)
    except (OSError, ValueError):
        return {}
    for acct in book.values():
        for rec in acct.values():
            for k in TIME_KEYS:
                if k in rec:
                    rec[k] = epoch(rec[k]) or 0
    return book


def save_book(path, book):
    out = {a: {n: {k: (datetime.fromtimestamp(v).astimezone().isoformat(timespec="seconds")
                       if k in TIME_KEYS else v) for k, v in rec.items()}
               for n, rec in acct.items()} for a, acct in book.items()}
    with open(path + ".tmp", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)


def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print("用法: instabox_tick.py <credential_name> [--now]", file=sys.stderr)
        return 2
    cred, force = args[0], "--now" in args
    os.makedirs(ROOT, exist_ok=True)
    path = os.path.join(ROOT, "machines.json")
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)     # 手动 --now 和 loop 同时跑也不会互相覆盖
        book = load_book(path)
        acct = book.get(cred, {})
        now = time.time()
        if not force and acct and now < min(r.get("next", 0) for r in acct.values()):
            return 0
        try:
            machines = call("GET", "/v1/machines", cred).get("machines", [])
        except DynamicCredentialError as e:
            print(f"[{cred}] 凭证错误: {e}", file=sys.stderr)
            write_wake(book, now)
            return 3
        except Exception as e:
            print(f"{time.strftime('%F %T')} [{cred}] 读取机器列表失败: {e}", file=sys.stderr)
            write_wake(book, now)             # 记录不动，最晚 60 秒后重试
            return 1
        rc, fresh = 0, {}
        for m in machines:
            name = m.get("name") or m["id"]
            st = fresh[name] = acct.get(name, {}) if acct.get(name, {}).get("id") == m["id"] else {}
            st["id"] = m["id"]
            action, due = decide(m, st, now)
            if action:
                try:
                    msg = act(action, m, st, cred, now)
                    if msg:
                        print(f"{time.strftime('%F %T')} [{cred}] {msg}")
                except Exception as e:
                    print(f"{time.strftime('%F %T')} [{cred}] {name} {action} 失败: {e}", file=sys.stderr)
                    rc = 1
                    if action != "error" and due > now + RETRY_FAST:
                        due = now + 60
            st["next"] = max(min(due, now + EXEC_EVERY), now + RETRY_FAST)
        book[cred] = fresh
        save_book(path, book)
        write_wake(book, now)
    return rc


if __name__ == "__main__":
    sys.exit(main())
