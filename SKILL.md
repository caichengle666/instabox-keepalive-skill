# InstaBox 保活（零 token 纯脚本方案）

## Purpose

给 InstaBox 机器做保活，三层：

- 防睡：每 10 分钟对运行中的机器 exec 一条轻命令，制造平台认可的真实活跃，防 30 分钟闲置休眠。
- 避开 8 小时强制暂停：跑满 7h50m 主动 suspend→resume，平台按 resume 时刻重新计时，手动暂停没有冷却，停机约 10 秒，进程保留。设 `INSTABOX_ROTATE=restart` 可改用 stop→start 冷启（进程会丢，靠 systemd 自启加 watchdog.sh 拉回），一般用不上。
- 看门狗：每次 exec 跑的是机器里的 `/data/watchdog.sh`，检查服务、没起来就拉起，同时算活跃。
- 兜底：万一还是被强制暂停，按平台返回的 cooldownUntil 提前 10 秒开始唤醒，没成功每 5 秒再试，2 分钟后改每分钟。
- 计时以平台 runStartedAt 为准：别处轮换过，本地记录自动跟上，不会多轮换。
- 多台机器：每拍算出所有机器里最早的到期时间写进 next_wake，loop 睡到那一刻（最多 60 秒），各台按各自的点触发。

全程纯脚本，不经过 agent、不烧 token。agent 只在脚本本身挂了才被叫醒。

触发时机：用户说「InstaBox 保活」「帮我部署保活」「机器老休眠」「防睡」「8 小时到了能不能自己起来」这类需求，或者要在新设备上复刻同一套保活时，读本 skill。

## Setup wizard（首次部署：agent 一步步问用户，用户自己操作）

每一步只问一件事，等用户回答再往下走。全程不让用户在聊天里贴 Key。

1. 问账号：「你有几个 InstaBox 账号要保活？」每个账号定一个凭证名，默认 `custom.instabox`，第二个起用 `custom.instabox-2` 或用户自己起的名字。
2. 教用户拿 API Key（每个账号各做一次）：
   - 浏览器打开 https://instabox.sh ，用 GitHub 或 Google 登录（就是建机器的那个账号）。
   - 左侧栏点 **API keys** → **Create Key**，名字填 `muse-keepalive` 之类好认的。
   - 权限选 **Restricted**，勾 `machines:write`（已含 read，够 exec、暂停、恢复）；嫌麻烦选 **Full access** 也行。
   - 生成后 Key 以 `ibk_` 开头，**只显示一次**，先复制好别关页面。
   - 已装 instabox CLI 并登录过的，也可以 `instabox keys create --name muse-keepalive`。
3. 让用户自己把 Key 存进 Muse 的 Secure Vault（用 `custom-connector-setup` 那套流程弹安全表单给用户填）：类型 `api_key`，请求头 `X-API-Key`，`api_hosts` 为 `api.instabox.sh`，凭证名用第 1 步定的。用户若把 Key 贴进了聊天：提醒他去 API keys 页面撤销这把、重新生成，再走安全表单。
4. 验证每个凭证：跑 `python3 instabox_tick.py <凭证名> --now`。退出码 0 就是通了，把账号下的机器名念给用户确认。失败按报错处理：
   - `HTTP 401`：`invalid_key`/`malformed_key` 是 Key 填错，`revoked_key`/`expired_key` 是 Key 已失效，都请用户重新生成后再存一次；`missing_key` 是请求没带上凭证，查凭证名和 api_hosts。
   - `HTTP 403 missing_scope`：Key 没勾 `machines:write`，请用户重建一把。
   - 退出码 3：凭证名在 Vault 里找不到，核对拼写。
5. 把通过验证的凭证名写进 `$ROOT/creds`，每行一个（`#` 开头是注释）。loop 每轮重读，以后加减账号只改这个文件，不用重启。
6. 问看门狗（可选）：「机器里有没有要一直开着的服务？」有就教他写成 systemd unit 并 enable，把 `watchdog.sh` 放到 `/data/watchdog.sh`、`chmod +x`，`SERVICES` 填服务名；没有就跳过，exec 会退回跑 `date` 只刷活跃。
7. 按下面 Deploy 部署、启动、挂 hook，最后告诉用户：每台机器约每 7 小时 50 分自动轮换一次、每次停约 10 秒；出问题只有脚本自愈失败才会来找他。

## Idle rules（先搞懂平台怎么判闲置）

- 官方 OpenAPI 的闲置定义：状态读取和 keepalive 不记活跃，只有真实工作才算——exec（远程执行命令）、files（文件操作）、terminal（终端流量）、preview（访问公共端口页面）。
- 另有专用 activity lease（活动租约）：`PUT /v1/machines/{id}/activity-leases/{leaseID}`，TTL 30 到 3600 秒，到期前续租，同样能防闲置休眠。本方案没用它，用 exec 更简单。
- 免费档关不掉休眠：`autoSuspendAfterSeconds` 可选值只有 300 到 1800，没有 0；PATCH 设 0 返回 HTTP 200 但回读仍是 1800，被平台无声忽略。原因是 manualLifecycle（手动生命周期）为 false，那是网站方管理员给账号放行的标记，用户自己改不了，付费档才有生命周期控制权。这条已实测，不要再去试。
- 机器模板的 activity 探针只上报 agent 真在干活（防干活时被误判闲置），不会自动生成动静来保活；agent-browser 模板没带 activity，闲置判定只看终端与 CPU。
- 防闲置防不住 8 小时强制停：免费机 maxRunMinutes 480，连续跑满 8 小时强制暂停，默认冷却约 10 分钟，只有管理员会话能改。这一条靠 7h50m 主动轮换避开，唤醒兜底。

## Timing（时间怎么定的）

| 参数 | 值 | 理由 |
|---|---|---|
| loop 心跳 | 睡到最早到期，最多 60 秒 | 只是看表，没到期不调 API；平时 API 约每 10 分钟一次，只有冷却结束前后每 5 秒一次 |
| exec 间隔 | 10 分钟 | 闲置阈值 30 分钟，漏跑一轮还剩 10 分钟余量；超过 14 分钟就没有漏跑余量了 |
| 主动 suspend→resume | 运行满 470 分钟（7h50m） | 离 480 分钟强制暂停留 10 分钟，轮换失败还能每分钟重试 10 次 |
| 强制暂停后唤醒 | cooldownUntil − 10 秒 | 平台直接给冷却结束时刻；没成功每 5 秒再试 |

每 8 小时停机对比：旧 loop（660 秒固定间隔）被强制暂停后平均停约 16 分钟、最坏约 21 分钟，在线率约 96.7%；只做冷却后精准唤醒约 10.5 到 11.5 分钟，约 97.7%；主动 suspend→resume 约 10 秒，约 99.9%。

## Architecture

- `scripts/ib_api.py`：API 调用（凭证走 surrogate，生命周期调用自动带 Idempotency-Key）。
- `scripts/instabox_tick.py`：保活一拍。读时间记录文件 `machines.json`（按账号分组、按机器名记录，每台只留最近一次：本轮开始运行时间 run0、上次 exec、下次到期 next，时间都是带时区的可读格式；加文件锁，手动 --now 和 loop 同时跑不会互相覆盖），全部没到期直接退出；到期就列出全部机器，由纯函数 `decide()` 决定 exec、轮换、唤醒或等待。`--now` 立刻全量巡检，可当手动唤醒用。
- `scripts/watchdog.sh`：放进每台机器的 `/data/watchdog.sh`。外部 tick 每 10 分钟和每次开机后用 exec 以 sudo 调它，逐个检查 `SERVICES` 里的 systemd 服务，没起来就 start；没有这个文件时 exec 退回跑 `date`。
- `scripts/loop.sh`：常驻 loop，每轮重读 `$ROOT/creds`，给里面每个账号跑一拍，写心跳，再睡到 next_wake（最多 60 秒）；只有动作和错误才写日志。
- `scripts/keepalive.sh`：自查自拉。判断标准：pidfile 存在、`proc` cmdline 含 loop.sh 绝对路径、心跳 20 分钟内新鲜（loop 在每个账号前都写一次心跳）。
- `scripts/instabox-loop-watch.sh`：hook 轮询脚本，每 300 秒跑一次 keepalive.sh，只在自动恢复失败时 wake agent。
- 轮换为什么成立（2026-10-25 实测）：冷却 409 只在连续运行满 8 小时被强制暂停时出现，手动 suspend 后 10 秒就能 resume；一台机器被强制暂停、冷却结束被拉回后，runStartedAt 就刷新成拉回的时刻，平台按 resume 时刻重新计时。机器内 reboot 没用，平台视角 VM 没停过，runStartedAt 不刷新。
- 运行时长 run0：第一次见到机器时取平台的 `runStartedAt`；之后每次从暂停回到运行，取 `runStartedAt` 和当前时间中较晚的那个，防止平台没刷新计时导致反复轮换。
- 不要用 agent cron 做轮询：每 15 分钟跑一次 agent 费 token，已验证不可取。

## Deploy

- 前置：Setup wizard 第 1 到 5 步做完，每个账号的凭证已存进 Secure Vault、验证通过、写进 `$ROOT/creds`。
- Key 本身绝不进文件、聊天记录、环境变量或日志。调用一律走 authd surrogate 交换（`ib_api.py` 已封装）。
- 把 `ib_api.py`、`instabox_tick.py`、`loop.sh`、`keepalive.sh` 放进保活目录（默认 `$HOME/workspace/instabox-keepalive`，两个 py 必须同目录），hook 脚本放 hooks 脚本目录。旧的 `instabox_wake.py`、`exec_activity.py` 已并入 tick，删掉。
- 每台机器里：要常驻的程序都写成 systemd unit（`Restart=always`，`systemctl enable`），开机自启；再把 `watchdog.sh` 放到 `/data/watchdog.sh`，`chmod +x`，在 `SERVICES` 里写上这些服务名。root 盘和 `/data` 在 冷启后也保留，unit 和脚本不会丢。
- 账号写在 `$ROOT/creds`，每行一个凭证名（Setup wizard 第 5 步）。换目录时同时改 loop.sh、keepalive.sh 和 hook 脚本的 `ROOT`。
- 启动时必须用绝对路径跑 keepalive.sh：keepalive 靠 `proc` cmdline 里的绝对路径认进程。换版本时先杀旧 loop 再跑 keepalive.sh。
- 启动后检查：loop.pid 对应进程活着、heartbeat 每分钟刷新（单账号轮换时可能慢一两分钟）、生成了 machines.json，里面每台机器的 run0 和 next 看着合理。
- 挂 hook：poll 300 秒，健康时静默，只在自动恢复失败时 wake agent。

## Usage

- 手动巡检/唤醒一个账号：`python3 instabox_tick.py <凭证名> --now`。退出码：0 正常，1 有机器出错，2 用法错误，3 凭证错误。
- 查健康：跑 `keepalive.sh`，0 是健康或已成功拉起，1 是自动恢复失败。看 `loop.log`：平时应该是空的，只有轮换（带「7h50m 主动轮换」字样）、唤醒和错误行。
- 首次上线后核对一次：第一台机器轮换后，看平台 `runStartedAt` 是否刷新成轮换时间、服务是否被 watchdog 拉回（loop.log 里 exec 无报错）。刷新了，8 小时强制暂停应该再也不出现；没刷新，脚本会退回冷却结束前 10 秒唤醒，停机约 10 分钟。
- 上线前先手动演练一次看门狗：在机器里 `sudo systemctl stop <服务>`，再跑 `instabox_tick.py <凭证名> --now`（需 lastExec 已过 10 分钟，或删掉 machines.json 里那台机器的记录），确认服务被拉起。
- 验收防睡：loop.log 里不应出现非轮换的 resume；出现了说明 exec 没被算活跃或 loop 断过。

## Verified facts

- exec 防睡成立（2026-10-25）：10:17 把 loop 改成先 wake 后 exec，11:11 提前核查改版后 4 轮日志，idle resume 0 次，exec 16 次全成功（4 轮乘 4 台），四台 runStartedAt 全部停在改版前、没被刷新过。观察窗口约 54 分钟，跨了快两个闲置周期，按旧模式这个时段本该每台睡约一次。正式定案验收另有一次性任务在 11:30 复核。
- 改版前的旧模式是纯睡了再救：2026-10-24 夜里 200 行截断日志里有 13 次 resume，分摊 4 台平均每台约 3 次；周期约 30 分钟闲置睡、最多再等 15 分钟下一轮巡检被拉起。
- 模拟测试通过：suspend 一台机器后手动跑唤醒脚本，10 秒内 resume 回 running，退出码 0，最终 ready 为 True。
- all 模式实测抓到过一台之前没被 loop 管的休眠机器并唤醒，说明按单台加机器会漏，全量巡检才不会漏。
- 免费档限制：只能用 agent-browser 模板建机，universal 模板会被拒；闲置 30 分钟自动暂停；maxRunMinutes 480，连续跑 8 小时强制停。
- InstaBox API 的机器对象里没有 region 字段。机器出口 IP 反查为 AWS us-west-2（美国俄勒冈州），这是 IP 归属反查的结论，不是官方标注。
- 公共端口给的是 HTTPS URL，不是裸 TCP/UDP 端口。对外服务要套在 HTTPS 里，例如 sing-box 走 WS+TLS。

## Pitfalls

- 拉起 loop 时必须加 `9>&-` 关掉继承的 flock 锁 fd，否则 loop 活着时锁永不释放，loop 死后它残留的 sleep 子进程还会继续握着锁，导致下次 keepalive 拿不到锁直接失败。
- 判活必须用 `proc` cmdline 含 loop.sh 绝对路径加心跳新鲜度，不能只看 pidfile，否则会漏判成已死而拉起僵尸双实例。
- 绝不用 pkill 加 f 盲杀，只杀 cmdline 能对上的旧 loop。pgrep 加 f 会匹配到执行命令的 shell 自身，查存活时必须排除自匹配。
- 写操作（suspend、resume、start、stop、建机、删机）要带 Idempotency-Key（UUID），重试同一个 key 不会重复执行。exec 和读操作不用带。
- next_wake 绝不能留空或写过去的时间：读列表失败、凭证错误、账号下没机器时 tick 也写最晚 60 秒后的时间；loop 睡眠 5 到 60 秒，文件坏了按 60 秒睡。否则 loop 会每秒空转、反复打 API。
- 凭证 401 或 403 时先确认请求真的带上了 credential，不用 surrogate helper 的请求等于没带凭证，再怀疑 Key 本身。
- VM 重启或替换会杀掉 loop，hook 在 5 分钟内发现并重启；状态文件在磁盘上，重启后接着算。
- InstaBox 条款禁 open proxy 和 public VPN exit，跑这类服务有封号风险，部署前先确认用途合规。

## Files

- `scripts/ib_api.py`：API 调用。
- `scripts/instabox_tick.py`：保活一拍（exec、轮换、唤醒、冷却等待）。
- `scripts/loop.sh`：常驻 loop，睡到最早到期的机器，最多 60 秒一拍。
- `scripts/watchdog.sh`：机器内看门狗。
- `tests/test_tick.py`：离线单测，改 tick 逻辑后在仓库根目录跑 `python3 -m unittest discover -s tests`。
- `scripts/keepalive.sh`：自查自拉。
- `scripts/instabox-loop-watch.sh`：hook 轮询脚本。
