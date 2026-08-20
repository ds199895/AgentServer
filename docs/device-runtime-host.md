# 多设备 Agent Runtime Host

本实现把“每个 Provider、每个项目都安装 Hook”改成“每台设备只安装并配对一个常驻 Host”。
AgentServer 通过统一的设备控制协议下发会话命令；设备上的 Adapter 再使用 Provider 原生协议。
首个完整 Adapter 是 `codex app-server`，原有 Hook、stream-json 和 PTY 观测继续作为 fallback。
每台设备仍需完成一次 bootstrap；“只配对一次”不表示可以跳过设备注册。

## 1. 从 t3code 借鉴了什么

t3code 的关键不是一套通用 Hook，而是把不同 Provider 的运行时差异留在 Adapter 边界内：

- Codex：`codex app-server` 的 stdio JSONL 双向 RPC；
- Claude：Agent SDK；
- Cursor/Grok：ACP；
- OpenCode：SDK 与事件订阅。

AgentServer 采用相同的“主动 Runtime Adapter”思想，但增加了多设备控制所需的 enrollment、长期设备
身份、generation fence、持久命令 ACK 和断线事件 spool。旧的
`app/execution/provider_adapters.py` 仍是只读 telemetry normalizer；它不会被扩写成进程/会话控制器。

## 2. 拓扑与边界

```text
浏览器（管理员 Session）
        │ 创建配对凭据 / session / turn / approval / interrupt
        ▼
AgentServer 中心控制面
  ├─ DeviceRuntimeStore：credential hash、Host generation、session、event
  ├─ ExecutionStore.CommandQueue：device-target command + stable ACK
  └─ /api/device-runtime/v1/*：仅接受 Device Bearer credential
        │ HTTPS 出站短轮询（默认 1 秒，失败时退避）；服务端不需要反向连接设备
        ▼
DeviceRuntimeHost（每台设备一个进程）
  ├─ 私有 credential file（0600）
  ├─ BridgeCommandJournal（cursor、ACK、uncertain）
  ├─ DeviceEventSpool（当前 generation 内 at-least-once）
  └─ RuntimeAdapter registry
       ├─ CodexRuntimeAdapter → codex app-server
       └─ 后续 Claude SDK / ACP / OpenCode SDK
```

以下事实相互独立，不能互相冒充：

- FRP 在线；
- SSH 可用；
- Device Runtime Host 在线；
- 某个 Provider session 正常；
- 某个既有 Execution Run 的 Agent Lease 正常。

Runtime session 固定在创建它的设备、Host generation 和 Provider resume cursor 上。设备离线时只会
变成 offline/lost，不会透明迁移；跨设备恢复必须显式创建新 session 并提供 Provider checkpoint。

## 3. 身份、fence 与持久化

### Enrollment

管理员为已登记的 Device 创建一个短时、单次使用的 enrollment token。Token 只返回一次，SQLite
只保存 SHA-256 hash；新 token 会使同设备尚未使用的旧 token 失效。Host 使用它换取高熵长期
credential，重新配对会原子撤销旧 credential。若成功换证的 HTTP 响应丢失，从首次成功消费起
固定 5 分钟内重试同一 token 会返回同一个派生 credential；这是响应恢复窗口，不是再次配对授权。

credential 不授予 Execution Run 上报权限，只能：

- heartbeat 与能力广告；
- 拉取本设备命令并 ACK；
- 上传本设备已分配 runtime session 的事件；
- 在当前 Host fence 内轮换 credential。

### Host generation

设备保存稳定 `instance_id`。Host 每次新启动，在单实例文件锁内原子增加持久 `generation`，并生成
新的 `runtime_session_id`/`boot_id`。heartbeat 接管成功后，旧进程即使仍持有 credential，也不能
继续拉取命令、ACK 或上传事件。命令 payload 同时携带：

```json
{
  "device_id": "device-1",
  "runtime_session_id": "boot-opaque",
  "runtime_generation": 7
}
```

服务端在读取、标记 delivered、ACK 和事件入口都重验 owner、device 与 generation；Host 在副作用
前再次验证 payload fence。设备 wall clock 不参与 deadline，Host 用认证响应中的 `server_time`、
RTT 和 monotonic anchor 计算。

### 崩溃恢复

`BridgeCommandJournal` 在 handler 开始前把命令记为 `executing`。若进程在副作用后、写 ACK 前
崩溃，重启会把它转成 `uncertain`，不会自动重放。只有明确的幂等操作可以恢复执行；ACK 响应丢失
时则复用同一个 `ack_id`。Adapter event 先写本地 SQLite spool，服务端按 `event_id` 与 producer
position 幂等；在当前 generation 内断网或响应丢失时可安全重传。Host 重启会分配新 generation，
服务端不接受旧 fence 的事件，所以启动时会把旧 generation spool 原子移入本地 bounded
dead-letter（保留原 envelope/fence、原因与隔离时间），再释放当前 FIFO；默认保留最新 10,000 条供
诊断，不承诺跨 generation at-least-once。

同一次 generation 切换也会原子隔离 journal 中属于旧 fence 的 `pending`/`executing`/
`uncertain`/`accepted` 工作，以及仍为 `pending`/`abandoned` 的 ACK，使它们退出派发、ACK 重放和
事件 causal barrier。已经结算的命令状态、ACK 历史和服务端 cursor 保持不变；隔离记录带
`stale_runtime_generation` 原因供诊断，不能以新 fence 伪装重放。

credential 轮换在本地私有文件中先持久化 `request_id` 和原 Host fence。如果服务端已换证但
响应丢失，下次 `rotate-credential` 会携带同一 `request_id` 与原 fence 幂等取回同一 replacement，
成功原子替换本地 credential 后才删除该恢复记录。enrollment 的幂等恢复窗口为 5 分钟；同一高熵
rotation `request_id` 可恢复到 replacement 过期或被管理员撤销为止，换一个 request id 会被拒绝。

事件响应按 `(event_id, producer_seq)` 逐条结算。accepted/duplicate 才从 live spool 删除；服务端
permanent NACK 会使对应原 envelope 在同一个 SQLite 事务中转入 durable dead-letter，retryable
NACK 或缺失结果则原样保留等待重传。未知引用、重复引用或畸形结果使整批不变，网络与 HTTP 失败
也不触发删除。服务端保留配额耗尽或 Session 生命周期/状态分歧时失败关闭，逐条返回 permanent
rejection，而不是把事件标成已接受或让 Host 静默丢弃。

事件 envelope 的 `device_id`、`runtime_session_id` 和 generation 必须与认证请求完全一致；跨
Session 的一次 delivery 在服务端共用一个 SQLite 事务，请求级 credential/fence 错误会让整批回滚，
而确定性的单事件错误才会返回 permanent rejection。`results` 是 v1 的唯一结算依据；
`accepted_through_seq`/`missing_ranges` 仅保留为兼容字段，Host 不用它们删除 spool。

命令 handler 可能先把 Provider lifecycle event 写入 spool、再生成带 Provider opaque ID 的 ACK。
因此 Host 在发事件前会先用稳定 `ack_id` 完成所有可重放 ACK；`executing`/`uncertain` 或仍未结算的
side-effect ACK 会形成显式 causal barrier，事件保持原样而不会越过它。取消会直接传播并释放锁，
不会被包装成普通轮询错误。

`session.stop` 的确定性命令入队和 Session 投影到 `stopping` 共用同一个 `BEGIN IMMEDIATE`：任一
失败都会整体回滚，并发重试只返回同一条命令。停止请求可能与已经在途的 Provider 事件交错，服务端
只在原 start/turn reservation、turn ID 和 interaction ID 仍匹配时吸收这些迟到事件，并始终保持
`stopping`，直到 `session.stopped`/`session.failed` 收敛；不匹配或终态后的事件会 permanent NACK。
stop 命令若过期、被拒绝、缺失或完成后仍无 lifecycle event，reconcile 会把 Session 失败关闭，
不会永久停在 `stopping`。

## 4. 控制协议

浏览器 Session API：

```text
POST   /api/devices/{device_id}/runtime/enrollment-tokens
GET    /api/devices/{device_id}/runtime
POST   /api/devices/{device_id}/runtime/probe
DELETE /api/devices/{device_id}/runtime/credential
GET    /api/devices/{device_id}/runtime/sessions
POST   /api/devices/{device_id}/runtime/sessions
GET    /api/runtime-sessions/{session_id}
DELETE /api/runtime-sessions/{session_id}
POST   /api/runtime-sessions/{session_id}/turns
POST   /api/runtime-sessions/{session_id}/interrupt
POST   /api/runtime-sessions/{session_id}/interactions/{interaction_id}/respond
GET    /api/runtime-sessions/{session_id}/events
```

设备 credential API：

```text
POST /api/device-runtime/v1/enroll
POST /api/device-runtime/v1/heartbeat
GET  /api/device-runtime/v1/commands
POST /api/device-runtime/v1/commands/{command_id}/ack
POST /api/device-runtime/v1/events:batch
POST /api/device-runtime/v1/credential:rotate
```

设备命令 allowlist：

```text
runtime.probe
session.start
session.turn
session.interrupt
session.respond
session.stop
```

heartbeat、ACK 和单事件最大 64 KiB；服务端事件批次最大 100 条/256 KiB，Host 按
100 条/224 KiB 拆批，为外层 JSON 留出空间。每个 owner/设备默认最多 8 个非终态 Runtime Session，
`DEVICE_RUNTIME_MAX_SESSIONS` 可设为 1–64。中心事件保留硬上限为每 Session 100,000 条或 64 MiB、
每设备 500,000 条或 256 MiB，任一先达即拒绝新事件；Host 本地 spool 最多 10,000 条，满载时
失败关闭而不删除已排队事件。Provider 最多 32 个、runtime/provider feature 列表最多 64 项；
能力广告不能携带环境变量、Token、prompt 或本地文件内容。

## 5. Runtime Adapter SPI

主动 Adapter 位于 `app/execution/runtime_adapters/`，核心操作为：

```python
probe()
start_session(RuntimeSessionSpec)
send_turn(session_id, RuntimeTurnInput)
interrupt_turn(session_id, turn_id)
respond_to_approval(session_id, interaction_id, decision)
respond_to_user_input(session_id, interaction_id, answers)
read_thread(session_id)
rollback_thread(session_id, num_turns)
stop_session(session_id)
events(session_id)
close()
```

Host 为每个 session 固定一个 Adapter 实例和 event pump。公共事件只包含生命周期 ID、machine code、
状态与审批所需的有限结构；文本 delta、reasoning、命令输出和 raw RPC 不上传。

每轮 Host 先 flush spool，再回收已经结束的 typed event pump。活跃 pump 异常或无终态提前 EOF 时，
必须先把合成的 `session.failed(event_pump_failed)` 持久写入 spool，随后才移除 Session handle 并关闭
Adapter；若 spool 仍满或 flush 失败，handle 保留并显式报告可重试 cycle error。若 Adapter 已经成功
写入 `session.stopped`/`session.failed`，随后 EOF 只做回收，不重复制造终态事件。取消跨越 SQLite
worker 写入时也会等待结果并记录 marker，避免下一轮重复入队。

## 6. Codex app-server 生命周期

`CodexRuntimeAdapter` 不使用 shell。Linux 常驻 Host 默认且强制以 `bubblewrap` 作为外层进程，
再直接启动：

```text
bwrap ... -- codex app-server
```

隔离会 unshare user/PID/IPC/UTS namespace（不 unshare network），将 `/` 只读挂载，创建独立
`/proc`、`/tmp`、`/dev`，只把解析后的 session workspace 与 `CODEX_HOME` 读写挂入。所有读写
挂载完成后，最后用 tmpfs 覆盖 Runtime Host state dir，因此 Provider 看不到同 UID Host 的
`device.credential`、`runtime.db` 和 journal。state dir、workspace、`CODEX_HOME` 任意两者存在包含
关系时拒绝启动，避免可写挂载抵消遮蔽边界。

Host 启动和 `runtime.probe` 都会实际通过该 sandbox 执行 `/bin/true`。Linux 缺少 `bwrap`、内核
禁止 user namespace、路径配置不安全或 preflight 非零退出时，Codex 会标记 unavailable，启动也
不会回退为裸进程。外层环境从白名单重建；显式的 `LD_*`、`PYTHON*`、`BASH_ENV`、
`NODE_OPTIONS` 等 loader/runtime 注入变量会被拒绝。网络仍可用，NVM/系统 Node 等依赖由只读根
挂载和清理后的绝对 `PATH` 提供。

这层 bubblewrap 边界用于隔离由 Host 启动的 Provider 子进程，不是对整台设备的通用保密
sandbox：根文件系统仍可读、网络仍可用，workspace 和 `CODEX_HOME` 中可读的秘密仍可被 Provider
访问。Host state 的 `0700`/`0600` 权限也无法防范 bubblewrap 之外的恶意同 UID 兄弟进程；如果设备上
运行不受信任的其他工作负载，应为 Runtime Host 使用独立 UID 或容器。

stdin/stdout 是逐行 JSON 对象；stderr 独立持续排空。握手顺序严格为：

```text
initialize
initialized (notification)
thread/start 或 thread/resume
turn/start
```

每个 session 有独立 subprocess、RPC pending map、interaction map 和 bounded event stream。reader loop
不会等待用户审批；server request 在受限并发 task 中处理，因此审批到来时不会阻塞同一条 RPC 管道。
畸形 JSON、超 8 MiB 行、EOF 和不可路由 envelope 对主动控制协议是 fatal，并会使所有 pending
request 失败。

权限映射：

| AgentServer mode | Codex approval | sandbox | reviewer |
|---|---|---|---|
| `approval-required` | `untrusted` | `read-only` | `user` |
| `workspace-write` | `on-request` | `workspace-write` | `user` |
| `auto` | `on-request` | `workspace-write` | `auto_review` |
| `full-access` | `never` | `danger-full-access` | `user` |

这些 mode 只改变 Codex 在外层边界内的审批与内建 sandbox 策略；CLI Host 始终保留 bubblewrap。
因此 UI 的 `full-access` 在宿主机持久路径中最多可写 session workspace 与 `CODEX_HOME`，不代表
对整台设备裸访问。

`thread/resume` 只有在明确的 missing-thread 错误时才回退到 `thread/start`；权限、timeout 或 transport
错误不会伪装成新会话。公共 approval decision 映射为 Codex 的 `accept`、`acceptForSession`、
`decline`、`cancel`。Provider RPC id 不外泄，AgentServer 使用独立 opaque `interaction_id`，响应只
能原子完成一次。

审批命令正文属于敏感信息，当前不会复制到公共 lifecycle event；远程 UI 只展示有限的 request
kind。需要查看完整命令时应进入受控终端，不能在缺少详情时盲目批准高权限操作。

## 7. 每台设备只配对一次

前提：Linux 设备能通过 HTTPS 出站访问 AgentServer，具备 `python3`、`curl` 或 `wget`，
并安装 `bubblewrap`、启用非特权 user namespace、安装且登录 Codex CLI 及其 Node.js 运行时；
`CODEX_HOME` 必须是现存目录。安装器支持 Linux user systemd；没有可用 user-systemd 时会写入
unit 并打印等价的前台启动命令。管理员可在“安装客户端”页注册设备并生成一次性 token；已有设备
也可在 Runtime 面板签发。管理页面为完整设备安装签发 30 分钟有效的单次 token；服务端 API 的默认值仍为
5 分钟：

```bash
umask 077
read -rsp 'Enrollment token: ' AGENTSERVER_ENROLLMENT_TOKEN
printf '\n'
printf '%s\n' "$AGENTSERVER_ENROLLMENT_TOKEN" > enrollment-token
unset AGENTSERVER_ENROLLMENT_TOKEN
```

### 一键接入新 Linux 设备

在“安装客户端”页填写设备 ID、唯一的 FRP 端口和 SSH 用户，点击“注册设备并生成配对凭据”。页面
以 `${DEVICE_ID}.ssh` 作为固定代理名创建中心端设备记录，签发 enrollment，并在校验返回设备身份后
显示 token 与一键命令。字段变化会立即清除页面内旧 token；token 不进入命令、Web Storage 或日志。
目标设备不需要 AgentServer checkout；以持有 Codex 登录态和工作区的普通用户运行页面给出的命令：

```bash
curl --fail --silent --show-error --proto '=https' --proto-redir '=https' \
  -o install-agentserver-device.sh https://agentserver.example.com/device-bootstrap/install.sh
bash install-agentserver-device.sh \
  --device-id device-001 \
  --base-url https://agentserver.example.com \
  --remote-port 20001 \
  --ssh-user operator \
  --runtime-user operator \
  --runtime-bundle-url https://agentserver.example.com
```

脚本依次完成：

1. 校验设备参数和目标普通用户，拒绝把 Runtime 安装到 root HOME；
2. 下载并校验当前发布的 Runtime bundle，拒绝摘要、清单、路径或权限异常的制品；
3. 以该用户检查 Python 依赖、Codex/Node 路径和真实 bubblewrap namespace；
4. 启用 linger/user-systemd，再以 root 安装 OpenSSH 与唯一的 FRP proxy；
5. 将 enrollment 暂存为目标用户所有、权限 `0600` 的临时文件；
6. 以清理过的用户环境配对并启动 `agentserver-runtime.service`，最后验证 service active。

FRP token 和 enrollment 默认用隐藏终端提示读取，不进入 argv、环境、unit 或日志。非交互自动化只
接受 `--frp-token-file` 和 `--enrollment-token-file`，文件权限必须恰好为 `0600`。NVM 未被标准
路径发现时显式传 `--codex-binary` 和 `--node-binary`。已有 SSH/FRP 时增加 `--runtime-only`；若
设备运行其他 frpc，在完整命令中增加 `--merge-existing /path/to/frpc.toml`。对统一安装器创建且
参数完全一致的 FRP 配置，普通重跑会复用安全的现有 token；参数不一致时失败关闭。
复用判定要求配置与安装器生成的规范模板完全一致，且配置目录、配置文件和 token 的 owner、权限与
链接状态通过安全检查；手工加入额外字段或注释后应使用 `--merge-existing`，不会被普通重跑覆盖。
对匹配的受管配置显式提供新的 FRP token 也会失败关闭；确认 frps 已同步新 token 后，完整安装器
使用 `--rotate-frp-token`（底层 `install_frpc_ssh.sh` 使用 `--rotate-token`）才能执行设备侧轮换。

总安装器在 FRP 前完成所有本地 Runtime preflight。若最终 enrollment 或 service 启动失败，已验证
可用的 SSH/FRP 会保留，并明确报告部分安装，不做跨两个服务的盲目回滚。控制面仍需等待 FRP 同步和
Runtime heartbeat；本地 service active 不能代替中心端在线检查。

### 手动配对与运行

```bash
./.venv/bin/python scripts/agentserver_runtime.py \
  --device-id device-001 \
  --base-url https://agentserver.example.com \
  enroll --enrollment-token-file ./enrollment-token

rm enrollment-token

./.venv/bin/python scripts/agentserver_runtime.py \
  --device-id device-001 \
  --base-url https://agentserver.example.com \
  run
```

只安装 Linux user-systemd Runtime：

```bash
scripts/install_agentserver_runtime.sh \
  --device-id device-001 \
  --base-url https://agentserver.example.com \
  --bubblewrap-binary /usr/bin/bwrap \
  --enrollment-token-file ./enrollment-token
```

该安装器现在是幂等的：发现同一 state dir 的现有 credential 时会保留它并更新 unit，不再默认换证；
显式重新配对必须同时提供新 token 和 `--reenroll`。state dir 会持久绑定首次安装的 device ID 与
AgentServer URL，传入不同身份会失败关闭。

`--bubblewrap-binary` 也可省略并由安装器从当前 `PATH` 解析，但安装器最终总会把验证过的绝对路径
显式固化进 systemd `ExecStart`。在消费单次 enrollment token 之前，它先以实际的 user/PID/IPC/UTS
namespace、只读根挂载和独立 `/proc`/`/tmp`/`/dev` 执行 `/bin/true`；preflight 失败则不发起 enrollment。

credential 保存在 `${XDG_STATE_HOME:-~/.local/state}/agentserver-runtime`，目录 `0700`、文件 `0600`；
不会出现在 argv、环境、unit 文件或日志中。Runtime bundle 版本目录与 state 分离，重复运行同一
版本不会重新 enrollment；新版本先独立校验并完成 preflight，再更新 unit，失败时保留旧版本。

## 8. Hook fallback 与扩展

不支持主动协议的 Provider 继续使用：

- `scripts/agentserver_provider_hook.py`；
- `scripts/agentserver_provider_exec.py` + stream-json；
- PTY/process observation。

它们不影响 Codex app-server session，也不共享设备 credential。新增 Claude SDK、ACP 或 OpenCode
时只需注册新的 `RuntimeAdapter` factory；设备控制 API、generation fence、journal 与 event spool
无需复制，已经 bootstrap 的设备和新项目也无需再安装对应 Hook。只有外部自行启动且仍无主动
Adapter 的 Provider 会继续使用这些 fallback。

## 9. 验证

```bash
./.venv/bin/python -m unittest tests.test_execution_device_runtime -v
./.venv/bin/python -m unittest tests.test_execution_device_runtime_api -v
./.venv/bin/python -m unittest tests.test_execution_device_runtime_e2e -v
./.venv/bin/python -m unittest tests.test_execution_runtime_host -v
./.venv/bin/python -m unittest tests.test_execution_codex_app_server -v
./.venv/bin/python -m unittest tests.test_install_agentserver_runtime -v
npm --prefix frontend test
npm --prefix frontend run build
```

常规 CI 使用 scripted app-server peer，不要求真实 Codex 登录。真实 smoke test 应作为有凭据、隔离
工作区中的可选验收，不应成为普通单元测试依赖。
