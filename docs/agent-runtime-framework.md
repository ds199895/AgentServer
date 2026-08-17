# AgentServer 任务与运行状态同步框架

Agent Runtime Protocol v1 把“终端是否存在”“Agent 是否在线”“任务执行到哪个阶段”拆成独立、
可恢复的事实。当前代码已经包含事件账本、状态机、受管终端身份、主动上报、被动观测、Lease、
命令、前端实时视图和 Codex Provider Adapter；不会读取或保存隐藏思考内容。

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

`scripts/agentserver_provider_hook.py` 接受单个 hook JSON；`--jsonl` 会逐行消费持续的 Provider
事件流。例如在受管 Terminal 内连接 Codex 非交互流：

```bash
codex exec --json "任务" \
  | ./.venv/bin/python scripts/agentserver_provider_hook.py --provider codex --jsonl
```

Codex Adapter 已映射 Session/Prompt、tool/span、阶段和 provider stop 事实。Provider 的 Stop 或
`turn.completed` 只进入 `finalizing`，不会擅自把 Run 判为成功；最终结果仍需可信 Reporter 或
AgentServer 控制面确认。内建 subagent 事件当前只作为 delegation observation 审计，不会伪造
一个共享父 Terminal Lease 的 Child Run。

Adapter 严格按事件类型重建 payload，只保留公开 machine code、阶段和数值进度；prompt、命令、
tool input/output、assistant 文本和 transcript 不会进入 Execution 事件。Claude/Kimi 目前只有
严格的 provider-neutral 归一化边界，原生事件映射仍待补充。

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

Bridge 目前没有设备级 enrollment、自动安装器或 Windows 安全 transport。启动时仍需显式提供
AgentServer URL、socket 地址、launch root PID，以及管理 API 为当前 Run 签发的 report/command
Token 文件；不要把 Token 放入 argv、`.env`、终端环境、仓库或日志。

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

本地样例见 [`.env.example`](../.env.example)，生产样例见
[`deploy/agentserver.env.example`](../deploy/agentserver.env.example)。

## 12. 验证与当前限制

```bash
./.venv/bin/python -m unittest discover -s tests -v
npm --prefix frontend test
npm --prefix frontend run build
./.venv/bin/python -m compileall -q app tests
bash -n scripts/*.sh
git diff --check
```

仍需明确保留的边界：

- Device Bridge 尚无设备长期凭据换取、安装/升级服务和真实 Windows 安全实现；当前生产链路仅
  支持 Linux，并要求外部启动器安全交付 per-Run Token 文件。
- 持久 tmux 的 `local-broker-path-compat` 以同一系统 UID 为信任边界；不受信任的本地 Agent 必须
  通过独立 UID/容器和稳定的系统级 Broker identity 隔离。
- Bridge 不内置具体 Agent 的 cancel/input 副作用；adapter 必须提供以 `command_id` 幂等的 handler，
  或显式处理 `uncertain` 恢复。
- Claude/Kimi 尚无经过真实 Provider 事件夹具验证的原生 Adapter；未知形状会失败关闭。
- Codex 内建 subagent 当前是审计 observation，不等同于跨设备 Child Run。跨设备委派必须先由
  AgentServer 控制面创建 Child Task / Assignment / Run。
- 外部 OTel/CloudEvents/A2A/MCP exporter、设备级 enrollment 和多 API worker 横向扩展尚未实现。
- 被动 observation 无法可靠区分隐藏的 thinking/coding；证据不足时系统会刻意显示 unknown，
  而不是猜测。
- Windows Terminal/探针与 Bridge 安全边界仍需真实 Windows 主机回归；真实 tmux 环境也应在发布
  前执行集成验收。
