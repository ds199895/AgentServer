# AgentServer 任务与运行状态同步框架

Agent Runtime Protocol v1 把“终端是否存在”“Agent 是否在线”“任务执行到哪个阶段”拆成独立、
可恢复的事实。当前代码已经包含事件账本、状态机、受管终端身份、主动上报、被动观测、Lease、
命令、前端实时视图、常驻多设备 Runtime Host 和 Codex app-server Adapter；不会读取或保存隐藏
思考内容。

[![Agent Runtime v1 架构图](../deploy/agent-runtime-framework.visual-check.1440x900.light.png)](../deploy/agent-runtime-framework.html)

点击图片可打开由 Archify 生成的交互式架构图，切换主题、追踪数据流并导出图片。

## 1. 实现状态

| 阶段 | 状态 | 当前实现 |
|---|---|---|
| Phase 0 | 已完成 | 修复进程探测线程边界、远端归属、空扫描清理和 `agent_hint` 语义 |
| Phase 1 | 已完成 | SQLite 不可变事件、双幂等、CAS Projection、Outbox、原子 snapshot/live |
| Phase 2 | 已完成 | 受管 Terminal 身份、启动生命周期、Task / Assignment / Run 原子绑定和恢复归并 |
| Phase 3 | 核心完成 | 本地 Broker、Reporter CLI/API/spool、短期 Token、Linux Device Bridge 与自动轮换 |
| Phase 4 | 已完成 | 进程、PTY、cwd 等被动 observation，以及字段级 authority / freshness 归并 |
| Phase 5 | 核心完成 | cancel/input/retry、Lease fence、命令 ACK journal、父子 Run DAG 与预算/深度限制 |
| Phase 6 | 已完成 | 浏览器状态、时间线、Run 树、断线 resync 和 projection 驱动的像素动画 |
| Phase 7 | 部分完成 | CloudEvents、OTel、A2A、MCP 的隐私安全映射；尚未启用外部 exporter |
| Phase 8 | 核心完成 | 设备 enrollment、长期凭据、Host generation fence、Runtime session 与 Codex app-server 原生控制 |

v1 以单个 AgentServer API 进程和 SQLite 为部署单元。启动时会锁定 `DATA_DIR`；第二个 API
worker 会失败关闭，避免重复接管 PTY、探针、reconcile 和本地控制 socket。

## 2. 统一实体与状态

| 实体 | 含义 | 稳定身份 |
|---|---|---|
| Device | 运行终端和 Agent 的设备 | `device_id` |
| Terminal | 一次受管终端启动 | `terminal_id + launch_id` |
| AgentInstance | 一次 Agent 进程实例 | `agent_instance_id` + process fingerprint |
| Task | 用户目标 | `task_id` |
| Assignment | Task 向 Terminal/Agent 的一次委派 | `assignment_id` |
| Run | 一次具体执行尝试；retry 会创建新 Run | `run_id` |
| Span | Run 内的工具调用或阶段 | `span_id` |
| Artifact | Run 产生的文件元数据 | Artifact event + `run_id/span_id` |

PID 不能单独充当身份。被动进程证据还包含启动时间、boot ID、PGID、TTY、父子关系和
`launch_id`，以防 PID 重用或同类 Agent 串线。

```text
Terminal: requested → provisioning → connecting → ready → exited | failed

Agent:    discovered | starting → online → stopping → exited
                              └→ unreachable → recovered | lost

Run:      pending → starting → running → succeeded | failed | cancelled | lost

Activity: idle | thinking | planning | coding | tooling | testing |
          reviewing | waiting | finalizing | unknown
```

`waiting` 必须带 `wait_reason`，例如 `user_input`、`approval`、`tool`、`child_run`、
`network` 或 `rate_limit`。Run 到达终态后不再显示活动阶段；证据过期但进程仍在时显示
`unknown/stale`。

## 3. 五个原子能力

1. **AppendEvent**：以 `event_id` 和命名空间化的 `(producer_id, epoch, seq)` 双重幂等追加。
2. **CompareAndSetTransition**：projected event 携带 `expected_revision`；冲突返回当前状态。
3. **Lease**：Terminal Assignment 和 AgentInstance 通过 acquire/renew/release 与 fencing 防止旧
   Run 在终端被重新分配后继续写状态或执行命令。
4. **LinkEntities**：Task、Assignment、Run、Agent、Terminal 和父子关系在同一 SQLite 事务内
   校验并提交，约束单父、无环、最大深度、并发数、期限和预算。
5. **SnapshotAndSubscribe**：在同一 cursor 边界返回完整快照与后续事件；溢出或旧 cursor
   显式返回 `resync_required`。

管理端创建 Task/Terminal 的实体与首事件也是原子操作；Assignment 会在 `BEGIN IMMEDIATE`
事务内重验 revision、lifecycle、launch 和 Lease 后一次提交。若 API 启动了新 Terminal、随后
Assignment 失败，会按 `owner_id + terminal_id + launch_id` 精确补偿；reconciler 也会收敛超时
的启动孤儿。

事件 envelope 使用 `agentserver.event/1`：

```json
{
  "schema": "agentserver.event/1",
  "event_id": "opaque-id",
  "type": "run.activity.changed",
  "scope": {
    "owner_id": "owner-1",
    "device_id": "device-1",
    "terminal_id": "terminal-1",
    "launch_id": "launch-1",
    "agent_instance_id": "agent-1",
    "task_id": "task-1",
    "assignment_id": "assignment-1",
    "run_id": "run-1",
    "parent_run_id": null,
    "span_id": null
  },
  "producer": {
    "id": "adapter-1",
    "epoch": "boot-42",
    "seq": 103,
    "adapter": "codex",
    "version": "1",
    "mode": "adapter"
  },
  "expected_revision": 12,
  "occurred_at": 1786968000.0,
  "evidence": {"confidence": 1.0, "valid_for_ms": 15000},
  "payload": {"activity": "coding", "summary": "更新状态投影"}
}
```

服务端追加 `recorded_at`、`global_sequence` 和 `stream_version`。一致性仅依赖服务端顺序；
`occurred_at` 只用于展示。SQLite 表通过 trigger 禁止事件 UPDATE/DELETE，事务提交后才发布 live
事件。Reporter 传输为 at-least-once，ACK 返回连续 cursor 和 `missing_ranges`；spool 满载不会
删除已经分配 sequence 的生命周期事件。

## 4. 受管终端与动态上下文

AgentServer 创建 local、tmux 或 SSH Terminal 时，只注入非秘密静态身份：

```text
AGENTSERVER_MANAGED=1
AGENTSERVER_PROTOCOL_VERSION=1
AGENTSERVER_ORIGIN=agentserver
AGENTSERVER_OWNER_ID=...
AGENTSERVER_DEVICE_ID=...
AGENTSERVER_TERMINAL_ID=...
AGENTSERVER_LAUNCH_ID=...
AGENTSERVER_CONTROL_SOCKET=...  # 控制通道真实可达时才有
AGENTSERVER_CONTROL_TRANSPORT=local-broker | local-broker-path-compat | device-bridge
AGENTSERVER_CONTROL_SERVER_PID=...         # direct local-broker
AGENTSERVER_CONTROL_SERVER_START_TIME=...  # direct local-broker
```

`AGENTSERVER_BASE_URL` 也是允许的发现值。启动器会清理继承环境中的 Token、Task 正文和动态
Task / Assignment / Run / Agent ID；SSH POSIX、PowerShell 和 cmd 都使用同一 allowlist。

动态上下文通过本地 `context` action 或 `GET /api/runtime/v1/context` 获取：

```json
{
  "schema": "agentserver.context/1",
  "terminal_id": "terminal-1",
  "launch_id": "launch-1",
  "context_revision": 42,
  "assignment": {
    "task_id": "task-1",
    "assignment_id": "assignment-1",
    "status": "accepted"
  },
  "active_run_id": "run-1",
  "server_time": 1786968000.0,
  "terminal_lease": {
    "id": "lease-1",
    "revision": 3,
    "expires_at": 1786968030.0
  },
  "control_available": true
}
```

没有活跃委派时，`assignment` 和 `active_run_id` 为 `null`。Reporter 不能自行挑选 Run；服务端
把事件绑定到当前 canonical scope，并重新校验 Terminal Assignment Lease。

## 5. 主动上报

### 通用 Reporter CLI

在已经分配 Task 的受管 Terminal 中运行：

```bash
./.venv/bin/python scripts/agentserver_report.py attach --kind codex
./.venv/bin/python scripts/agentserver_report.py phase coding
./.venv/bin/python scripts/agentserver_report.py progress --current 2 --total 5
./.venv/bin/python scripts/agentserver_report.py wait --reason approval
./.venv/bin/python scripts/agentserver_report.py span start --id tool-1 --name tests
./.venv/bin/python scripts/agentserver_report.py span end --id tool-1 --status ok
./.venv/bin/python scripts/agentserver_report.py artifact dist/report.json --kind report
./.venv/bin/python scripts/agentserver_report.py complete
```

失败使用 `fail --code <machine-code> --summary <公开摘要>`。本地 Broker/Provider Adapter 会按
event type 重建 payload 并丢弃自由文本 summary；只有显式签名的 active HTTP Reporter 才能持久
化公开摘要。`phase waiting` 会被拒绝，必须用 `wait --reason ...`。Artifact 是显式功能，只接受
workspace-relative path、受限 kind/media type，不会自动上传文件内容。

### Provider Hook

`scripts/agentserver_provider_hook.py` 接受 Provider 传入的单个 hook JSON；
`scripts/agentserver_provider_exec.py` 则托管非交互 JSONL 命令，同时原样转发 stdout 并保留
Provider 的退出码。例如在受管 Terminal 内运行 Codex：

```bash
./.venv/bin/python scripts/agentserver_provider_exec.py --provider codex -- \
  codex exec --json "任务"
```

不要把 Provider 直接管道到 `provider_hook.py --jsonl`：普通 shell 默认返回最后一个进程的退出码，
还会让 observer 吞掉 Provider stdout。`provider_exec.py` 对每行遥测独立失败开放，超限或畸形行也
会继续排空和透传；到达 EOF 时会关闭未完成 Span 并进入非权威的 `finalizing`。

Codex Adapter 已映射 Session/Prompt、tool/span、阶段和 provider stop 事实。Provider 的 Stop 或
`turn.completed` 只进入 `finalizing`，不会擅自把 Run 判为成功；最终结果仍需可信 Reporter 或
AgentServer 控制面确认。内建 subagent 事件当前只作为 delegation observation 审计，不会伪造
一个共享父 Terminal Lease 的 Child Run。

Kimi Code 通过 `~/.kimi-code/config.toml` 的 `[[hooks]]` 接入同一个 Provider Hook（hook 命令的
工作目录是 Kimi 会话的项目目录，下面假定在该仓库内运行）：

```toml
[[hooks]]
event = "SessionStart"
command = "./.venv/bin/python scripts/agentserver_provider_hook.py --provider kimi"

[[hooks]]
event = "TurnStarted"
command = "./.venv/bin/python scripts/agentserver_provider_hook.py --provider kimi"

[[hooks]]
event = "PermissionRequest"
command = "./.venv/bin/python scripts/agentserver_provider_hook.py --provider kimi"

[[hooks]]
event = "PermissionResult"
command = "./.venv/bin/python scripts/agentserver_provider_hook.py --provider kimi"

[[hooks]]
event = "PreToolUse"
command = "./.venv/bin/python scripts/agentserver_provider_hook.py --provider kimi"

[[hooks]]
event = "PostToolUse"
command = "./.venv/bin/python scripts/agentserver_provider_hook.py --provider kimi"

[[hooks]]
event = "PostToolUseFailure"
command = "./.venv/bin/python scripts/agentserver_provider_hook.py --provider kimi"

[[hooks]]
event = "Stop"
command = "./.venv/bin/python scripts/agentserver_provider_hook.py --provider kimi"

[[hooks]]
event = "StopFailure"
command = "./.venv/bin/python scripts/agentserver_provider_hook.py --provider kimi"

[[hooks]]
event = "Interrupt"
command = "./.venv/bin/python scripts/agentserver_provider_hook.py --provider kimi"

[[hooks]]
event = "SessionEnd"
command = "./.venv/bin/python scripts/agentserver_provider_hook.py --provider kimi"
```

需要委派和 compact 观测时，再按同一形式加入 `SubagentStart`、`SubagentStop`、`TaskStarted`、
`PreCompact`、`PostCompact`；也可加入 `UserPromptSubmit`。`UserPromptQueued`、
`SessionHeartbeat`、`Notification` 会被 Adapter 接收但不产生运行事件。Kimi 的退出码语义是 `2`
表示阻断主流程，因此 Provider Hook 出错时一律以 `1` 失败开放（fail-open），遥测故障永远不会
阻塞工具调用。

非交互流同样支持 `kimi -p --output-format stream-json`：

```bash
./.venv/bin/python scripts/agentserver_provider_exec.py --provider kimi -- \
  kimi -p "任务" --output-format stream-json
```

Kimi Adapter 映射 Session/Turn、tool/span（`tool_call_id` 只做哈希传输引用）、Permission
等待、Subagent/Task 委派观测、Compact 阶段和 Stop/Interrupt 边界；`Stop` 与
`session.resume_hint` 等 meta 行同样不会擅自判定 Run 结果。只有稳定 `agent_id`/`task_id` 的
委派才产生可关联的 `child_run.requested`；仅有可重复 `agent_name` 时只记录不关联的
`child_run.observed`。

Claude Code 可在项目 `.claude/settings.json`（或对应的用户级 settings）中配置相同 Hook。下面是
最小配置；`PostToolUseFailure`、`PermissionRequest`、`SubagentStart`、`SubagentStop`、
`PreCompact`、`PostCompact`、`StopFailure`、`SessionEnd` 可按同一结构加入：

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "./.venv/bin/python scripts/agentserver_provider_hook.py --provider claude"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "./.venv/bin/python scripts/agentserver_provider_hook.py --provider claude"}]}],
    "PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "./.venv/bin/python scripts/agentserver_provider_hook.py --provider claude"}]}],
    "PostToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "./.venv/bin/python scripts/agentserver_provider_hook.py --provider claude"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "./.venv/bin/python scripts/agentserver_provider_hook.py --provider claude"}]}]
  }
}
```

Claude 非交互 stream-json 使用同一个透明 wrapper：

```bash
./.venv/bin/python scripts/agentserver_provider_exec.py --provider claude -- \
  claude -p --verbose --output-format stream-json "任务"
```

Claude Adapter 映射 Messages API 的 `system`/`assistant`/`user`/`result` 行、tool use/result Span、
compact 边界和原生 Hook；最终 `result` 同样只产生 `finalizing`，不越权决定 AgentServer Run 结果。

Adapter 严格按事件类型重建 payload，只保留公开 machine code、阶段和数值进度；prompt、命令、
tool input/output、assistant 文本和 transcript 不会进入 Execution 事件。

## 6. 本地 Broker 与远端 Bridge

`ExecutionControlBroker` 随 FastAPI lifespan 启停，默认地址为
`DATA_DIR/control/agentserver.sock`。目录权限为 `0700`、socket 为 `0600`、单条请求上限
64 KiB。Linux 服务端每次请求都使用 `SO_PEERCRED` 的 PID/UID，并核对 `/proc` 启动时间、
祖先链、session/TTY 和 launch binding；direct Terminal 的客户端还会在发送 payload 前反向校验
Broker PID 与启动时间，路径被替换时不会把运行数据交给伪 listener。

持久 tmux pane 会跨 AgentServer 进程重启，无法安全保留某一次 Web 进程的 PID，因此明确使用
`local-broker-path-compat`：服务端仍校验调用者 lineage，但客户端只信任 owner-UID socket path。
它适合当前“Terminal 与 AgentServer 同属一个受信系统账号”的部署，不是恶意同 UID 代码的隔离
边界。若要运行不受信任的本地工作负载，应把 Terminal 与服务进程拆到不同 UID/容器，并由独立
常驻 Broker 或系统级 socket activation 提供稳定身份。

非 Linux 平台无法提供当前所需的 peer-PID 证明时会失败关闭。Windows 后续需要 owner-only
Named Pipe DACL、客户端 PID/creation time 和祖先校验，不能仅依赖同一登录用户。

`scripts/agentserver_bridge.py` 是 Linux per-Run Bridge 原语，具备：

- 仅允许 HTTPS；测试和单机开发可使用 loopback HTTP，所有客户端禁用环境代理继承；
- `0600` Token 文件热加载、临近过期自动轮换与原子持久化；
- SQLite Reporter spool、断线重传、heartbeat 和结构化 health；
- 命令 cursor、稳定 `ack_id`、ACK 重试和 durable journal；
- 执行前再次校验 Run/Assignment/Terminal/launch/Lease fence；
- handler 副作用后状态不确定时进入 `uncertain`，默认不自动重放。只有显式声明 handler 幂等，
  或用相同 `command_id` 选择 `retry_idempotent` 恢复，才会重试。

Bridge 仍是 per-Run 原语，不复用设备长期凭据。启动时需显式提供 AgentServer URL、socket 地址、
launch root PID，以及管理 API 为当前 Run 签发的 report/command Token 文件；不要把 Token 放入
argv、`.env`、终端环境、仓库或日志。

### 多设备 Runtime Host

受 t3code 的 provider driver / adapter 边界启发，AgentServer 另提供每台设备一个、只出站连接的
`DeviceRuntimeHost`。每台设备仅需安装并配对这一个 Host，不再为每个项目或 Provider 复制设备
credential 和控制协议。浏览器和中心服务只发送统一的 session/turn/interrupt/respond/stop 命令；
真正访问工作区、Provider 登录态和启动 CLI 子进程的是目标设备上的 Host。Provider 差异收敛在
`RuntimeAdapter` registry，一个 Host 可注册多个 Provider adapter；目前首个内建实现是 Codex
`app-server` 双向 JSON-RPC adapter。增加 Provider 时不需要修改设备命令队列或浏览器协议。

这里的“一次”是每台设备各做一次 bootstrap，并不是免除设备注册。设备配对完成后，受控 Runtime
才不需要为每个 Provider 或项目重复配置 Hook；外部自行启动且没有主动 Adapter 的进程仍走 fallback。

这条链路不要求给每个 Codex 项目配置 Hook。它只覆盖由 AgentServer 创建的原生 Runtime Session；
用户在外部终端自行启动的 Claude/Kimi/Codex 不会被 daemon 注入或劫持，仍使用前面的 Hook、
JSONL wrapper 或被动观测。

首次接入一台 Linux 设备：

1. 在“安装客户端”页填写设备 ID、FRP 端口与 SSH 用户，注册中心端设备记录并生成一次性
   enrollment token。代理名固定为 `${DEVICE_ID}.ssh`；页面只在内存中显示 token，且不会把它拼入命令。
2. 目标设备需能通过 HTTPS 出站访问 AgentServer，具备 `python3`、`curl` 或 `wget`，并已安装
   `bubblewrap`、启用非特权 user namespace，以及安装和登录 `codex` 及其 Node.js 运行时；
   `CODEX_HOME` 必须是现存目录。
   把 token 写入权限为 `0600` 的临时文件，不要放进命令行或环境变量。Codex Provider 的
   bubblewrap preflight 失败时会报告 unavailable，且不会回退为未隔离进程。安装器会在 enrollment
   前用实际 namespace/mount 参数运行 `/bin/true`，并把解析后的绝对路径固化为 unit 的
   `--bubblewrap-binary`，避免 service 启动时重新依赖可变的 `PATH`。
3. 目标设备不需要 AgentServer checkout；以拥有 Codex 登录态的普通用户下载并执行 bootstrap：

   ```bash
   curl --fail --silent --show-error --proto '=https' --proto-redir '=https' \
     -o install-agentserver-device.sh https://agentserver.example/device-bootstrap/install.sh
   bash install-agentserver-device.sh \
     --device-id DEVICE_ID \
     --base-url https://agentserver.example \
     --remote-port 20001 \
     --ssh-user operator \
     --runtime-user operator \
     --runtime-bundle-url https://agentserver.example
   ```

完整安装器隐藏读取 FRP/enrollment token，先以 Runtime 用户做 fail-fast preflight，再安装系统级
SSH/FRP、启用 linger，最后写入并验证该用户的 `agentserver-runtime.service`。已有隧道使用
`--runtime-only`；已有其他 frpc 使用 `--merge-existing /path/to/frpc.toml`；非交互场景使用两个
mode-0600 token 文件。普通重跑复用匹配的受管 FRP token 并保留 Runtime credential，显式
`--reenroll` 才换证。匹配的受管 FRP token 只有在显式传入 `--rotate-frp-token` 时才会替换；没有
user-systemd 时完整安装会失败；底层 Runtime 安装器仍可单独写 unit 并
打印前台命令。长期 credential、Host generation、ACK journal 和事件 spool 仅保存在 owner-private
state 目录；服务端数据库只保存 credential hash。

运行边界如下：

- heartbeat 报告 Provider capability 和平台状态，在线判断只采用服务端时间；FRP/SSH 在线与
  Runtime 在线是三个独立信号；
- Host 以 HTTPS 短轮询拉取命令（默认 1 秒一次），错误时指数退避；当前没有服务端
  挂起的长轮询或设备入站连接；
- 所有命令绑定 owner、device、`runtime_session_id` 和单调 generation；新 Host 接管后旧命令、
  旧 Session 和迟到事件全部失败关闭；
- 命令 ACK 和事件使用本地 SQLite 持久重试；非幂等命令若在副作用后崩溃会进入 `uncertain`，
  不自动重复执行。新 generation 会把旧 fence 的活跃 journal 工作和未结算 ACK 原子标为
  `quarantined`，使其退出重放和 causal barrier，同时保留 settled history 与 server cursor。事件仅在
  当前 generation 内 at-least-once；Host 重启会生成新 fence，并把已不可能通过旧 fence 的 spool
  原子移入 bounded durable dead-letter。dead-letter 保留原 envelope/fence、原因与隔离时间并按
  最旧优先裁剪，不把旧事件伪装成已送达，也不静默丢弃；
- 服务端按 `(event_id, producer_seq)` 逐条返回 accepted、duplicate 或 rejected。permanent NACK
  在 Host 端原子转入 durable dead-letter；retryable NACK 与缺失结果保留在 live spool。未知、重复或
  畸形结果使整批 settlement 失败且本地不变；网络/HTTP 失败同样不删除事件。保留配额耗尽或 Session
  生命周期/状态分歧不会降级成 accepted，而是失败关闭并给出逐事件 permanent rejection。跨 Session
  delivery 共用一个服务端事务，envelope fence 必须与请求 fence 完全一致；Host 以逐项 `results` 为
  唯一结算依据，并在事件前先结算 causal command ACK，uncertain side effect 会显式阻塞 spool；
- stop 命令入队和 Session 的 `stopping` 投影在同一 SQLite 事务内提交。只吸收仍与原
  start/turn/interaction identity 匹配的在途生命周期事件，且不退出 `stopping`；stop 过期、拒绝、
  缺失或完成后没有终态事件时会 reconcile 到 `failed`，不会留下永久悬挂的 Session；
- typed Adapter 的 event pump 异常或提前 EOF 会先 durable spool `session.failed`，再移除 handle 和
  关闭 Adapter；spool 满时先 flush，仍失败则保留 handle 并显式重试。已写入 Provider 终态后 EOF
  只回收，不重复合成失败事件；
- enrollment 逻辑上只消费一次；若成功响应丢失，从首次消费起固定 5 分钟内重试
  同一 token 会取回同一 credential。轮换在 Host 端持久化 `request_id + 原 fence`，响应丢失后
  使用同一 request 幂等恢复，直到 replacement 过期或被管理员撤销；轮换没有固定 5 分钟恢复
  窗口。长期 credential 也可从管理端撤销，删除设备会先撤销凭据；
- Codex 子进程只读挂载主机根目录，只对 workspace 与 `CODEX_HOME` 开放写入，并在最后用 tmpfs
  遮蔽 Host state dir；网络保持可用，但 Provider 无法读取同 UID Host 的 credential/SQLite。UI
  的 `full-access` 只关闭边界内的 Codex 审批，不会取消这层挂载隔离或授予整机写权限。该边界
  仍可读根文件系统并访问网络；Host 文件的 `0700`/`0600` 也不防范 bubblewrap 之外的恶意
  同 UID 进程，这类工作负载必须用独立 UID 或容器隔离。

完整协议、Codex RPC 映射、安装命令与恢复语义见
[`device-runtime-host.md`](device-runtime-host.md)。

## 7. Token、命令与委派边界

浏览器 Session、设备身份和 Runtime Token 是不同权限：

- report Token 仅授予 `context/report/heartbeat`；Bridge Adapter Token 额外绑定准确的
  `adapter` producer mode，客户端不能靠 envelope 自报提升权限；
- command Token 仅授予 `context/heartbeat/commands/ack`；
- Token 绑定 owner、Run、Terminal、launch、device、AgentInstance 和 capability，并由独立 HMAC
  密钥及 SQLite allowlist 双重验证；
- 默认 15 分钟有效，只能在寿命最后 20%（5–300 秒窗口）内轮换；同一父 Token 并发刷新只生成
  一个 replacement，旧 Token 保留到自然过期；
- Run 终态立即撤销 command/ACK 权限；report Token 暂留至过期，仅允许最终事件的精确幂等重放。

cancel/input 命令携带 Run、Assignment、Terminal、launch 和 Lease generation。服务端在 delivery、
Bridge 暴露、handler 执行和 ACK 前重新校验 fence；Bridge 用认证响应中的 `server_time`、保守 RTT
与 monotonic anchor 判断 deadline，不信任设备 wall clock。终态或 Terminal 被重新分配后，旧
Token 和已下载命令都不能继续执行。ACK body 最大 64 KiB，且只应包含 machine-readable 状态。

父子 Run 关系只能由控制面的原子 Assignment 创建。Reporter 的 `child_run.linked` 只能确认一个
已经存在、declared parent 一致的 canonical relation；不能绕过深度、并发、期限和预算限制动态
挂接任意 Run。

## 8. 被动观测与权威归并

没有主动 Adapter 时，TerminalManager 仍采集进程树、PTY 特征、cwd 和退出事实：

- 本地用进程祖先、TTY 和 fingerprint 绑定；远端只接受 PID、唯一 banner 或唯一候选等明确证据；
- 多个同类 Agent 无法区分时保留为设备级 `unattributed_observations`；
- `agent_hint` 只是启动意图；探针 `None` 表示未知并保留旧证据，成功空列表才清空；
- 稳定进程扫描会续证；进程退出可结束精确 AgentInstance，但绝不推导 Run 成功。

字段级 authority 从高到低为 `control`、`active`、`adapter`、`system`、`process`、
`pty/heuristic`。evidence 包含 `source`、`confidence`、`recorded_at`、`expires_at`、`fresh`、
`authority` 和 `global_sequence`。默认 process TTL 为 15 秒、PTY TTL 为 5 秒；过期后显示 stale，
不会继续冒充当前事实。

## 9. API 与前端契约

管理 API 使用浏览器 Session：

```text
POST /api/tasks
POST /api/tasks/{task_id}/assignments
GET  /api/tasks/{task_id}
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/events
POST /api/runs/{run_id}/cancel | retry | input
POST /api/runs/{run_id}/reporter-token
POST /api/runs/{run_id}/bridge-tokens
GET  /api/execution/snapshot
WS   /ws/execution?after_sequence=<cursor>
```

Runtime API 只接受相应 capability 的 Bearer Token：

```text
GET  /api/runtime/v1/context
POST /api/runtime/v1/events:batch
POST /api/runtime/v1/heartbeat
POST /api/runtime/v1/token:refresh
GET  /api/runtime/v1/commands
POST /api/runtime/v1/commands/{command_id}/ack
```

设备 Host 使用独立长期 credential 与 `/api/device-runtime/v1/*`；浏览器侧另有配对、Runtime
session、turn、interrupt 和 interaction API。Device credential 与 Reporter Token 双向不可混用，
详细端点见 [`device-runtime-host.md`](device-runtime-host.md#4-控制协议)。

快照包含 tasks、assignments、runs、agents、terminals、`terminal_bindings`、relations 和未归属
observation。前端严格通过 `terminal_bindings[].active_run_id` 选择 Run，并以
`(revision, view_sequence)` 合并 evidence；不会拿不同 Run 的 revision 比“最近”。时间线在 Run
完成并解绑后仍锁定原 `run_id`，可显示最终结果。WebSocket 断线使用 cursor 恢复；队列溢出或
cursor 失效会触发 single-flight snapshot resync。Execution API 不可用时，终端主体继续工作并
显示 degraded 状态。

## 10. 标准映射与隐私

`app.execution.interop` 提供 CloudEvents 1.0、OpenTelemetry、A2A 和 MCP 映射。隐私模式要求
调用方显式提供至少 32-byte 的持久 `pseudonym_key` 和稳定 `sink_id`，只保留公开枚举/数值，
对 owner、scope、event、producer 等 ID 按 protocol、sink、tenant 和 entity kind 分域生成 HMAC
pseudonym；默认也不外发内部 global sequence。完整 payload 是需要调用方先完成 tenant ACL 的
显式 opt-in。

这些函数是映射库，不是已启用的网络 exporter。正式外发前仍需配置独立、持久的 export key、
明确 sink 授权、重试/背压策略和敏感字段验收。内部 Execution Event、状态机、权限与 cursor
仍是权威来源，CloudEvents/OTel/A2A/MCP 不替代它们。

## 11. 配置

所有值都有默认值；Runtime Token 本身不是配置项。

| 环境变量 | 默认值 | 作用 |
|---|---:|---|
| `AGENTSERVER_CONTROL_SOCKET` | `DATA_DIR/control/agentserver.sock` | 本地 Broker 路径 |
| `AGENTSERVER_REMOTE_CONTROL_SOCKET` | 空 | 已部署 Bridge 时注入远端 Terminal 的设备本地路径 |
| `AGENTSERVER_LOCAL_DEVICE_ID` | `agentserver-local` | 本地 observation 的 Device ID |
| `AGENT_SCAN_INTERVAL` | `10` 秒 | Agent 进程树校准周期 |
| `EXECUTION_RECONCILE_INTERVAL` | `5` 秒 | Lease/退出/孤儿归并周期 |
| `TERMINAL_PTY_READY_GRACE` | `0.25` 秒 | PTY 首次输出前的 ready 宽限 |
| `TERMINAL_LAUNCH_ORPHAN_TIMEOUT` | `30` 秒 | 未完成启动的归并阈值 |
| `AGENT_PROCESS_OBSERVATION_TTL_MS` | `15000` | process evidence TTL |
| `AGENT_PTY_OBSERVATION_TTL_MS` | `5000` | PTY evidence TTL |
| `AGENT_LEASE_TTL` | `30` 秒 | Agent/Assignment Lease TTL |
| `AGENT_LOST_GRACE` | `90` 秒 | unreachable 到 lost 的宽限 |
| `REPORTER_TOKEN_TTL` | `900` 秒 | 新签发 Runtime Token 寿命 |
| `EXECUTION_SUBSCRIPTION_QUEUE_SIZE` | `1024` | 每个 live subscription 队列上限 |
| `OBSERVATION_INGEST_QUEUE_SIZE` | `1024` | 被动 observation 入口队列 |
| `EXECUTION_MAX_PARENT_DEPTH` | `16` | 父子 Run 最大深度 |
| `EXECUTION_MAX_CHILD_RUNS` | `8` | 每个父 Run 默认最大 Child Run 数 |
| `DEVICE_RUNTIME_OFFLINE_AFTER` | `30` 秒 | 独立判断常驻 Device Runtime Host offline 的服务端 TTL |
| `DEVICE_RUNTIME_MAX_SESSIONS` | `8` | 每 owner/设备的非终态 Runtime Session 上限，允许 1–64 |

Device Runtime 还有固定边界：单事件 64 KiB，批次 100 条/256 KiB（Host 以 224 KiB
字节预算分批），每 Session 最多保留 100,000 条或 64 MiB，每设备最多 500,000 条或
256 MiB，Host 本地 spool 最多 10,000 条。保留量任一达限会拒绝新事件，不会沉默删除旧事件。

本地样例见 [`.env.example`](../.env.example)，生产样例见
[`deploy/agentserver.env.example`](../deploy/agentserver.env.example)。

## 12. 验证与当前限制

```bash
./.venv/bin/python -m unittest \
  tests.test_execution_device_runtime_api \
  tests.test_install_agentserver_runtime -v
./.venv/bin/python -m unittest discover -s tests -v
npm --prefix frontend test
npm --prefix frontend run build
./.venv/bin/python -m compileall -q app tests
bash -n scripts/*.sh
git diff --check
```

仍需明确保留的边界：

- Device Runtime Host 已有一次性 enrollment、长期凭据和 Linux user-systemd 安装器，但还没有
  Windows service 安装器和自动升级通道；per-Run Device Bridge 仍要求安全交付短期 Token 文件。
- 持久 tmux 的 `local-broker-path-compat` 以同一系统 UID 为信任边界；不受信任的本地 Agent 必须
  通过独立 UID/容器和稳定的系统级 Broker identity 隔离。
- Legacy Bridge 不内置具体 Agent 的 cancel/input 副作用；常驻 Host 的 Codex Adapter 使用原生
  turn/interrupt/interaction RPC。所有非幂等 handler 仍必须显式处理 `uncertain` 恢复。
- Hook/stream-json Adapter 对未知 telemetry 单行失败开放；主动 app-server 协议对畸形或不可路由
  JSON 失败关闭，避免在控制通道损坏时猜测语义。
- Codex 内建 subagent 当前是审计 observation，不等同于跨设备 Child Run。跨设备委派必须先由
  AgentServer 控制面创建 Child Task / Assignment / Run。
- Runtime Adapter 当前只内建 Codex app-server；Claude、Kimi、Cursor/OpenCode 等仍走既有
  Hook/JSONL 路径，直到各自有稳定、可双向控制的 adapter。
- 外部 OTel/CloudEvents/A2A/MCP exporter 和多 API worker 横向扩展尚未实现。
- 被动 observation 无法可靠区分隐藏的 thinking/coding；证据不足时系统会刻意显示 unknown，
  而不是猜测。
- Windows Terminal/探针与 Bridge 安全边界仍需真实 Windows 主机回归；真实 tmux 环境也应在发布
  前执行集成验收。
