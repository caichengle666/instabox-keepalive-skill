# InstaBox 保活（instabox-keepalive-skill）

给 InstaBox 免费机器做保活的**纯脚本方案**：不经过 AI agent、不消耗 token，让机器绕开免费档的两个限制，近乎 24/7 在线。

本仓库是给人看的说明；给 AI agent 执行的完整规范在 [`SKILL.md`](SKILL.md) 里（方法、时间设定依据、已验证事实、排障铁律）。

## 要解决的问题：免费档的两个限制

| 限制 | 说明 |
|---|---|
| 闲置暂停 | 机器 30 分钟没有真实活动就被自动暂停，免费档关不掉（官方参数只对管理员开放） |
| 8 小时强制暂停 | 连续运行满 480 分钟被强制暂停，之后还有约 10 分钟冷却，冷却期内怎么拉都拉不起来 |

关键事实（均已实测）：

- 平台只认 **exec、文件操作、终端流量、访问公共端口** 为真实活动；查状态、调保活接口**不算**活动，靠轮询状态防不住闲置暂停。
- **手动** suspend 后没有冷却，约 10 秒就能 resume，且平台按 resume 时刻**重新计算** 8 小时运行起点；被**强制**暂停才有冷却。提前轮换就是利用这一点。

不同做法下，每次 8 小时周期的停机时间对比：

| 做法 | 停机 | 在线率 |
|---|---|---|
| 固定间隔巡检、睡了再救（旧办法） | 平均约 16 分钟、最坏约 21 分钟 | 约 96.7% |
| 只做冷却结束后精准唤醒 | 约 10.5–11.5 分钟 | 约 97.7% |
| **本方案：提前主动轮换** | **约 10 秒** | **约 99.9%** |

## 工作原理（三层）

1. **防睡**：每 10 分钟对每台运行中的机器 `exec` 一条轻命令，在 30 分钟闲置线之前刷一次真实活动，留一轮容错。
2. **提前轮换**：机器跑满 7 小时 50 分（470 分钟）时，脚本主动 `suspend → resume` 一次：没有冷却、约 10 秒、进程和内存都保留，8 小时计时从零开始，强制暂停永远轮不到。
3. **兜底**：万一还是被强制暂停，脚本读平台返回的 `cooldownUntil`（冷却结束时刻），提前 10 秒开始尝试唤醒，失败每 5 秒重试，2 分钟后改为每分钟。

调度方式：常驻 loop 不固定睡眠时长——每拍把所有机器里最早的到期时间写进 `next_wake`，loop 睡到那个点（最多 60 秒），多台机器各按各的节奏精确触发，没到期的账号不调 API。计时以平台的 `runStartedAt` 为准：你在别处（面板、别的脚本）手动轮换过，本地记录会自动跟上，不会多轮换一次。状态记在 `machines.json`（按账号分组、时间是带时区的可读格式，人能直接看）。

看门狗（可选层）：每次 exec 跑的是机器里的 `/data/watchdog.sh`，顺带检查指定的 systemd 服务、挂了就拉起；机器里没有这个文件时，exec 退回跑 `date`，只刷活跃。

自愈：`keepalive.sh` 负责看着 loop（进程在不在 + 心跳新不新鲜），死了就拉起来；再配一个每 300 秒的 hook 巡检，只有脚本自愈失败时才叫醒 agent。**正常运行时全程零 token。**

## 快速部署

前置条件：每个 InstaBox 账号在 Muse 的 Secure Vault 里注册一个 API 连接（`api_key`，放在 `X-API-Key` 请求头，`api_hosts` 为 `api.instabox.sh`），记下凭证名。**API Key 绝不写进任何文件、日志或聊天记录**，脚本通过保险库的 surrogate（替身凭证）机制调用。

1. 把 `scripts/` 里的 `ib_api.py`、`instabox_tick.py`、`loop.sh`、`keepalive.sh` 放进保活目录（默认 `~/workspace/instabox-keepalive`，两个 `.py` 必须同目录）；把 `instabox-loop-watch.sh` 放进 hooks 脚本目录并挂上 hook（poll 300 秒）。
2. 改 `loop.sh` 里的 `CREDS`：每个账号填一个凭证名，例如 `CREDS=(custom.instabox custom.instabox-another-account)`。
3. （可选）把 `watchdog.sh` 放进每台机器的 `/data/watchdog.sh`，`chmod +x`，并在文件里的 `SERVICES` 填要守护的 systemd 服务名（机器里需要免密 sudo）。
4. 用**绝对路径**启动：`bash ~/workspace/instabox-keepalive/keepalive.sh`（判活靠进程命令行里的绝对路径，所以必须这样启动）。
5. 启动后检查：`loop.pid` 对应的进程活着、心跳每分钟在刷新、`machines.json` 已生成且每台机器的 `run0`（运行起点）和 `next`（下次到期）合理。

## 日常使用

- 手动巡检 / 唤醒一个账号：`python3 instabox_tick.py <凭证名> --now`（退出码：0 正常，1 有机器出错，2 用法错误，3 凭证错误）。它和 loop 共用同一份状态文件，不会打架。
- 查健康：跑 `keepalive.sh`，退出码 0 是健康或已自愈，1 是自愈失败。
- 看日志：`loop.log` 平时应该是空的，只有轮换（带「7h50m 主动轮换」字样）、唤醒和错误才写行。如果出现非轮换的 resume，说明 exec 没被算作活动或 loop 断过，去查。

## 时间参数

| 参数 | 值 | 为什么 |
|---|---|---|
| exec 间隔 | 10 分钟 | 闲置线是 30 分钟，漏跑一轮还剩 10 分钟余量 |
| 主动轮换 | 跑满 470 分钟 | 离 480 分钟强制线留 10 分钟，轮换失败还能重试 |
| 冷却唤醒 | `cooldownUntil` − 10 秒 | 平台直接给冷却结束时刻，不用估算 |
| loop 睡眠 | 睡到最早到期，最多 60 秒 | 只是看表，没到期不调 API；平时 API 约每 10 分钟一次 |

## 文件说明

- `SKILL.md`：给 AI agent 的完整规范（方法、时间设定、部署步骤、已验证事实、排障铁律）。
- `skill.yaml`：skill 元数据。
- `scripts/ib_api.py`：API 调用（凭证走保险库 surrogate，生命周期调用自动带 Idempotency-Key）。
- `scripts/instabox_tick.py`：保活一拍：读状态文件，到期才调 API，由纯函数 `decide()` 决定 exec、轮换、唤醒还是等待。
- `scripts/loop.sh`：常驻 loop，给每个账号跑一拍，写心跳，再睡到 `next_wake`。
- `scripts/keepalive.sh`：自查自拉 loop。
- `scripts/instabox-loop-watch.sh`：hook 轮询脚本，只在自愈失败时 wake agent。
- `scripts/watchdog.sh`：放进机器 `/data/watchdog.sh` 的看门狗模板。

## 注意事项

- 不要用 `pkill -f` 之类按命令行模糊杀进程（会匹配到执行命令的 shell 自身）；只按 pid + `/proc/<pid>/cmdline` 核对后精确停。拉起 loop 时要关掉继承的 flock 锁句柄（`keepalive.sh` 已处理），否则锁会被残留子进程握住、自愈失效。细节见 `SKILL.md` 的 Pitfalls。
- 写操作（suspend / resume / start / stop / 创建 / 删除）都要带 `Idempotency-Key`，重试同一个 key 不会重复执行。
- 在机器上跑什么服务请自行遵守 InstaBox 的服务条款（例如其条款禁止 open proxy / 公共 VPN 出口）。
- 本方案只用平台公开 API 和免费档规则本身：防睡靠真实 exec，提前轮换用的是正常的手动暂停 / 恢复语义。
