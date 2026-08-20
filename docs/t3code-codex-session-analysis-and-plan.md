# t3code Codex 会话与消息 UI 分析及重构方案

> 状态：方案阶段（本次只新增文档，不修改现有 runtime 实现）
> 调研对象：`/home/hsy/Study/Agent/t3code`
> 目标项目：`/home/hsy/Study/Agent/AgentServer`

## 1. 结论先行

t3code 的核心体验不是“启动 Codex CLI 并转发 stdout”，而是：

```text
React UI
  ↕ typed command / thread subscription
Python API（本项目的宿主与鉴权）
  ↕ local bridge protocol
Node Codex bridge
  ↕ JSON-RPC over stdio
codex app-server
```

Codex app-server 的通知和请求先被转换为稳定的 provider/runtime 事件，再投影为两类 UI 数据：

1. `messages`：用户消息、assistant 流式文本和最终文本。
2. `activities`：turn、reasoning、plan、command、file change、MCP/dynamic tool、approval/user-input 等工作日志。

这两类数据必须独立建模。把 tool call 或 step 拼进 assistant 文本，会丢失生命周期、展开详情、失败状态、审批交互和滚动行为，无法像素级复刻 t3code。

本项目现有 `app/execution/*` + `DeviceRuntimeDialog` 是另一套面向“设备 runtime/执行观测”的模型，包含约 3 万行 runtime 相关代码和大量 fallback/observed/inferred 语义。新需求要求完全摒弃它，因此目标不是在旧模型上增加 Codex UI，而是删除旧 runtime 入口后建立新的 Codex session 子系统。

## 2. t3code 调研结果

### 2.1 会话建立、恢复和进程生命周期

关键文件：

- `apps/server/src/provider/Layers/CodexSessionRuntime.ts`
- `apps/server/src/provider/Layers/CodexAdapter.ts`
- `apps/server/src/provider/Layers/CodexProvider.ts`
- `apps/server/src/provider/Drivers/CodexDriver.ts`
- `apps/server/src/provider/Layers/codexLaunchArgs.ts`
- `packages/contracts/src/provider.ts`

建立流程：

1. `CodexDriver` 解析 binary、`CODEX_HOME`、cwd、模型、权限模式和 app-server 参数。
2. `CodexSessionRuntime` 启动 `codex app-server`，建立 JSON-RPC peer。
3. 发送 `initialize`，随后发送 `initialized` notification。
4. 没有 resume cursor 时调用 `thread/start`；有 cursor 时调用 `thread/resume`。
5. resume 出现明确的“thread 不存在/rollout 不存在”等可恢复错误时才新建 thread；这不是 UI fallback，而是 provider thread 恢复策略。
6. 保存 provider thread id 到 `resumeCursor`，session 状态变为 `ready`。
7. 每个 thread 只有一个长生命周期 session runtime；session 关闭时结束 pending approval/user-input、终止 scope、关闭事件流。

对应源码证据：`CodexSessionRuntime.ts:1743-1760`（initialize/open）、`451-505`（start/resume）、`1803-1849`（turn/start、interrupt）、`1897-1973`（请求响应、事件流、close）。

session 合同（`packages/contracts/src/provider.ts`）：

- `ProviderSessionStartInput`：thread、provider instance、cwd、model、approval/sandbox、runtime mode、resume cursor。
- `ProviderSendTurnInput`：thread、input、attachments、model selection、interaction mode。
- `ProviderTurnStartResult`：thread id、turn id、resume cursor。
- `ProviderRespondToRequestInput` / `ProviderRespondToUserInputInput`：审批和交互请求的闭环。
- `ProviderEvent`：统一的 `session | notification | request | error` envelope，附 `turnId/itemId/requestId/payload`。

### 2.2 Codex JSON-RPC 事件到稳定事件

关键文件：

- `apps/server/src/provider/Layers/CodexAdapter.ts`
- `packages/contracts/src/providerRuntime.ts`
- `apps/server/src/orchestration/Layers/ProviderRuntimeIngestion.ts`

Codex 原生事件按以下维度归一化：

| 原生信号 | 稳定事件/UI 意义 |
| --- | --- |
| `thread/started`, `thread/status/changed` | session/thread 状态和恢复游标 |
| `turn/started`, `turn/completed` | 一个用户请求的工作周期、计时和折叠边界 |
| `item/started`, `item/completed` | work-log 行生命周期 |
| `item/agentMessage/delta` | assistant 文本增量 |
| `item/reasoning/*Delta` | reasoning/思考 activity 或流式内容 |
| `item/plan/delta`, `turn/plan/updated` | plan/step 列表和步骤状态 |
| `item/commandExecution/*` | command execution、输出、退出状态 |
| `item/fileChange/*` | file change、diff/变更文件 |
| `item/mcpToolCall/*`, `item/tool/call` | MCP/dynamic tool call、输入输出详情 |
| `item/*/requestApproval` | approval request，阻塞 turn，等待用户决策 |
| `item/tool/requestUserInput` | 多问题/选项式用户输入，阻塞 turn |
| `serverRequest/resolved` | 请求完成，清理 pending 状态 |

`CodexAdapter` 的重要设计是保留原生 `payload` 作为 raw evidence，同时生成稳定字段：`itemType`、`title`、`detail`、`status`、`providerRefs`、`turnId/itemId/requestId`。UI 不依赖某个 Codex 版本的字段名。

`packages/contracts/src/providerRuntime.ts` 中的稳定枚举包括：

- item：`assistant_message`、`reasoning`、`plan`、`command_execution`、`file_change`、`mcp_tool_call`、`dynamic_tool_call`、`web_search`、`image_view` 等；
- request：command/file-read/file-change approval、patch approval、dynamic tool、user input、auth refresh；
- event：`session.*`、`thread.*`、`turn.*`、`item.*`、`content.delta`、`request.opened/resolved`、`user-input.*`、`tool.*`。

### 2.3 服务端持久化和实时同步

关键文件：

- `apps/server/src/orchestration/Layers/ProviderRuntimeIngestion.ts`
- `apps/server/src/orchestration/decider.ts`
- `apps/server/src/orchestration/projector.ts`
- `apps/server/src/persistence/Layers/ProjectionThreadMessages.ts`
- `apps/server/src/persistence/Layers/ProjectionThreadActivities.ts`
- `apps/server/src/orchestration/http.ts`
- `packages/client-runtime/src/state/threads.ts`

链路为：

```text
provider runtime event
  → ProviderRuntimeIngestion
  → orchestration command
  → decider event
  → message/activity projector
  → thread snapshot + websocket subscription
  → client thread reducer
```

assistant 增量不会直接把每个 token 写成一条消息。Ingestion 按 `messageId/turnId` 缓冲文本，批量 dispatch `thread.message.assistant.delta`，在 item/turn 完成时 dispatch `thread.message.assistant.complete`。这样能控制 SQLite 写放大和 React 重渲染。

activity 持久化字段为 `activityId/threadId/turnId/tone/kind/summary/payload/sequence/createdAt`。`ActivityPayloadProjection` 会对 command、MCP result、文件列表等做 UI 投影和摘要，原始完整 payload 仍留在持久化层。

客户端通过 thread snapshot + durable websocket subscription 收取替换快照/增量；thread state 维护 sequence cursor、缓存、加载更早 turns、重连和旧页合并，避免 stale snapshot 覆盖较新的 live data。

### 2.4 UI 消息、step 和 tool call 显示

关键文件：

- `apps/web/src/components/ChatView.tsx`
- `apps/web/src/components/chat/MessagesTimeline.tsx`
- `apps/web/src/components/chat/MessagesTimeline.logic.ts`
- `apps/web/src/session-logic.ts`
- `apps/web/src/components/chat/ChatComposer.tsx`

`session-logic.ts` 先把 activity 投影成 `WorkLogEntry`，再和 `ChatMessage`、`TurnPlanEntry`、`ProposedPlan` 合成 timeline entries。`MessagesTimeline` 再将其压成以下行类型：

- `message`：用户气泡、assistant markdown、复制、时间/耗时；
- `work`：一个或多个 work-log/tool 行；
- `work-toggle`：隐藏的历史 tool/log 数量；
- `turn-fold`：较早 turn 的折叠边界；
- `turn-plan` / `proposed-plan`：计划与 step；
- `working`：正在运行的 turn 和计时器。

默认只显示当前 work group 的一条 tool 行（`MAX_VISIBLE_WORK_LOG_ENTRIES = 1`）；其余以 `+N previous tool calls/log entries` 折叠。tool 行可点击展开，内容来自 `command/rawCommand/detail/changedFiles/toolData`，使用等宽字体和左侧竖线。

tool 行的像素级行为：

- 高度约 28px，左右 `px-0.5`，圆角 `rounded-md`，hover 使用淡 accent 背景；
- 左侧 20px 图标槽，Lucide 图标按 command/file/MCP/dynamic/web/image/request 分类；
- 标题 12px、单行截断；详情使用 secondary label；
- 右侧 16px 状态槽：完成 `Check`、失败 `X`、空/未决 `Minus`，全部带 tooltip；
- 可展开时右侧 `ChevronDown` 旋转 180°；详情 `max-h-64`、`font-mono`、`whitespace-pre-wrap`、可选择；
- assistant 使用 markdown，消息列最大宽度约 768px；
- 流式增长只在用户位于 live edge 时自动滚动，查看历史时不抢滚动位置；
- turn/work-group 折叠通过保持 anchor row 的 visible content position，防止展开后跳屏；
- 左侧 minimap 只在外侧 gutter 足够宽时启用，避免覆盖消息文本。

### 2.5 审批、用户输入和停止

pending request 是 session 级状态，不是普通消息：

1. provider 发起 request，adapter 生成稳定 `requestId` 并把 turn 置为 waiting。
2. ingestion 持久化 `request.opened` / `user-input.requested` activity。
3. UI 在 composer/banner 中展示批准按钮或问题选项。
4. 用户 dispatch response command；adapter 通过 Deferred/JSON-RPC response 唤醒 provider。
5. resolved activity 到达后移除 pending UI，turn 恢复 running 或完成。

停止操作必须调用 `turn/interrupt`，随后仍等待 `turn/completed`/session 状态事件；不能只在前端清掉 spinner。

## 3. 本项目现状与彻底删除范围

### 3.1 当前 runtime 的问题

本项目目前同时存在：

- `app/execution/runtime_adapters/codex.py`：Python 自建 JSON-RPC Codex adapter；
- `app/execution/device_runtime.py` + `runtime_host.py`：设备常驻 runtime host、租约、spool、命令 journal；
- `app/execution/service.py/store.py/projector.py/events.py/models.py`：Execution Run/Task/Assignment/Agent 的观测投影；
- `app/execution/bridge*.py`、`reporter.py`、`provider_hook.py`、`terminal_observer.py`：桥接、hook、被动观察和 reconcile；
- `frontend/src/execution-*.ts`、`useExecutionStream.ts`、`RunTimeline/RunTree/RunStatusBadge`：Execution UI；
- `frontend/src/components/DeviceRuntimeDialog.tsx`：另一套 RuntimeSession event → transcript 降维 UI；
- `frontend/src/DeviceWorld.tsx`、`TerminalPane.tsx`、`App.tsx`、`api.ts`：runtime 状态和入口耦合；
- `app/main.py`：runtime 初始化、后台 reconcile、device runtime router、artifact bootstrap 和 websocket endpoints。

当前 UI 的 `RuntimeEvent` 只有 `type/payload`，前端再用 `deriveRuntimeConversation` 和 `coalesceRuntimeEvents` 猜测 assistant 文本，无法稳定表达 t3code 的 `message/activity/request` 三类实体。

### 3.2 新需求下的删除规则

以下内容不能作为 fallback 保留：

- 旧 `RuntimeSession`、`RuntimeEvent`、`ExecutionRun`、`ExecutionTask` 等公开 API；
- `runtime.probe/session.start/session.turn/session.respond` 命令族；
- device runtime enrollment/heartbeat/lease/spool/bridge/reporter/hook；
- execution snapshot、evidence freshness、observed/inferred 状态机；
- `DeviceRuntimeDialog` 的 transcript 合并逻辑；
- terminal agent scan 对 AgentSession 的替代识别；
- “provider 不可用时退回 shell/旧 runtime”的任何分支。

设备、SSH/FRP、终端、workspace、preview 等与 AgentSession 无关的能力必须保留，但不能再向 Agent UI 提供第二套会话事实来源。

其中“在不同设备开终端”是明确的保留能力，不是迁移期间的临时兼容项：

- 本地设备继续创建 `TerminalSession(kind=local)`；
- 远程设备继续通过 FRP + SSH 创建 `TerminalSession(kind=ssh, device_id=...)`；
- 现有 xterm snapshot/input/resize/terminal websocket 继续工作；
- terminal cwd、workspace 浏览/读写、文件附件、preview 和服务端口探测继续绑定到该 terminal/device；
- TerminalSession 的 stdout/PTY 状态只进入终端 UI，不会被当作 Agent message/activity；
- AgentSession 可以从某个 TerminalSession 继承 `device_id + cwd`，也可以直接在设备 workspace 上创建；创建后两者通过引用关联，但互不替代。

删除旧 runtime 时，必须把当前 `TerminalExecutionLifecycle` 从 execution projection 中拆出来，改成独立的 `TerminalLifecycle`（requested/provisioning/connecting/ready/exited/unavailable）。否则“删除 execution”会误伤终端创建和远程终端恢复。

## 4. 推荐目标方案

本节使用 provider-agnostic 命名：`Agent Runtime`、`AgentSession`、`ProviderBridge`。前文的 Codex 仅代表调研得到的第一种 provider 实现，不会成为目标领域模型、API 路径或 UI 组件名称；后续其他 code agent 通过新增 bridge adapter 接入。

### 4.1 进程边界：Node Agent Runtime bridge

建议新增 `agent_bridge/`（Node + TypeScript），不是继续扩张 Python adapter：

- 直接复用/移植 t3code provider runtime 的 app-server 生命周期和请求关联逻辑；
- bridge 只处理一个 provider session：启动、resume、turn、interrupt、approval/user-input、raw event normalization；
- provider-specific 协议和字段只存在于 bridge adapter，不泄漏到 AgentSession/UI contract；
- Python FastAPI 保留鉴权、设备/终端、SQLite、HTTP/WebSocket 对外入口；
- bridge 与 Python 之间使用 versioned NDJSON 或 Unix socket JSON-RPC；不共享 Python Execution 模型；
- bridge 崩溃由 Python session supervisor 感知为 session error，不能悄悄切换旧 runtime。

原因：t3code 的参考实现依赖生成的 provider app-server schema/RPC client。把 provider 协议翻译进 Python 的收益低且容易重新制造协议分叉；独立 Node bridge 可以最大限度复用参考实现，同时为后续 provider 增加独立 adapter。

### 4.2 多设备执行平面与快速扩展

原项目的多设备能力不能只作为 UI 保留项；它必须进入 AgentSession 的一等路由字段。目标部署形态为：

```text
AgentServer（鉴权、设备目录、session projection、WS）
  ├─ local device: local Agent Runtime bridge
  ├─ device A: outbound Agent Runtime bridge
  └─ device B: outbound Agent Runtime bridge
       ↕ authenticated device channel
    provider process/app-server（运行在各自设备）
```

这里的 bridge agent 是新的、只负责 AgentSession 的轻量进程，不是旧 `app/execution/runtime_host.py` 的保留或改名。旧 device runtime 的 lease/spool/evidence/reporter/command journal 全部删除。

`AgentSession` 必须包含：

- `device_id`：目标设备，创建后不可隐式漂移；
- `executor_id` / `bridge_instance_id`：实际承载 bridge 的实例；
- `transport`：`local`、`ssh`、`outbound-agent`；
- `provider`、`cwd`、`platform`、`provider_version`、`capabilities`；
- `device_generation`：设备连接代次，用于拒绝旧 bridge 的迟到事件。

设备连接协议只做四件事：`hello/capabilities`、`session.open`、`session.command`、`session.event`。所有事件 envelope 同时带 `device_id/session_id/bridge_instance_id/sequence`。设备离线时 session 进入 `disconnected`/`error`，允许同一设备代次恢复后按 sequence 补发；绝不切换到本机或另一台设备作为 fallback。

现有 `DeviceStore`、FRP/SSH 探测、远程 terminal 创建逻辑可以作为“设备目录和传输层”保留，但不再负责 AgentSession 事实投影。新增设备不应要求修改 session orchestrator：

1. 设备注册提交统一 manifest（id、transport、地址/凭据引用、platform、workspace roots、provider binaries）。
2. 对应 bridge agent 启动后发送 capabilities handshake（provider 是否安装、版本、可用模型、sandbox/approval、tool 能力）。
3. server 根据通用 `DeviceConnector` 选择 local/SSH/outbound-agent connector；AgentSession 和 UI 只依赖统一 contract。
4. 只有新增传输方式（例如某种隧道或 Windows service）才新增一个 `DeviceConnector`，不修改 session/event/UI 层。

因此，Linux/macOS/Windows 或新远程节点的扩展点在 connector/agent 层，而不是复制一套 runtime session。设备 selector、设备状态、能力和 session 绑定都在创建 AgentSession 时明确显示。

### 4.3 房间小人中的 TerminalSession / AgentSession 区分

当前房间已经有 `CharSlot.kind = terminal | runtime`，但两类小人共用大部分 sprite、状态气泡和 execution projection，用户只能在 hover 按钮文字中间接看出差异。新实现应改成明确的 `terminal | agent`，并让类型在像素房间中可一眼识别。

每个房间角色的最小模型为：

```text
RoomCharacter
  sessionKind: terminal | agent
  sessionId, deviceId, label
  connectionState
  activityState       // terminal: shell/idle/disconnected; agent: turn/tool/waiting
  eventText           // terminal: connection/service; agent: step/tool summary
  target              // open terminal pane | open AgentSession pane
```

视觉规则：

- **TerminalSession**：默认坐在键盘前，角色上方显示 `>_`/terminal 小徽标；状态只来自 PTY/SSH（idle、connecting、exited、disconnected），气泡显示“终端已连接/SSH 重连”等；点击打开 TerminalPane。
- **AgentSession**：使用独立的 agent 小徽标和独立 accent，不再使用旧 Runtime 标识；状态来自 AgentSession/turn/activity（starting、ready、thinking、tool、waiting、completed、error），气泡显示当前 step/tool 摘要；点击打开 AgentSessionPane。
- 两类角色可以同时出现在同一设备房间，使用相同的 slot 排布，但 hover card 必须明确显示“终端”或“Agent”、session id、设备名和 cwd；选中环颜色和打开按钮目标也不同。
- Agent 的 approval/user-input waiting 必须在角色头顶保持 pinned 气泡；TerminalSession 不得显示“等待批准/调用工具”等 Agent 语义。
- 房间顶部 legend 同时展示“终端会话”和“Agent 会话”，不再使用“Runtime”；设备房间数量只按 device 计算，角色数量按两类 session 合并并提供 overflow 入口。

小人只是 session 的导航和状态摘要，不是新的事实来源：TerminalSession 的真相来自 terminal store/PTY channel，AgentSession 的真相来自 provider event projection。两者不能通过扫描终端 stdout 或旧 execution snapshot 互相推断。

### 4.4 新的领域模型

后端和前端共享一份小而稳定的 `agent_session_contract`：

```text
AgentSession
  id, provider_session_id, provider, cwd, model, state, active_turn_id,
  resume_cursor, pending_request_id, created_at, updated_at, last_error

AgentTurn
  id, session_id, state, started_at, completed_at, status, usage

AgentMessage
  id, session_id, turn_id?, role, text, streaming, created_at, updated_at

AgentActivity
  id, session_id, turn_id?, kind, tone, title, detail,
  item_type?, status?, command?, raw_command?, changed_files?, data?, sequence, created_at

AgentRequest
  id, session_id, turn_id?, kind, questions?, detail?, status, created_at, resolved_at
```

事件 envelope 必须包含 `schema_version/session_id/sequence/event_id/occurred_at`，并使用以下事件：

`session.started|ready|state_changed|closed|error`、`turn.started|completed|interrupted`、`message.delta|completed`、`activity.started|updated|completed`、`request.opened|resolved`、`plan.updated`、`thread.metadata.updated`。

SQLite 采用 append-only `agent_events` + projections（sessions/turns/messages/activities/requests）。sequence 按 session 单调递增；snapshot 与 websocket 都支持 `after_sequence`，断线重连先 snapshot 再补事件。

### 4.5 会话 API

建议替换旧 `/api/*/runtime/*` 为明确的 Agent Runtime API：

- `POST /api/agent/sessions`：创建 session，执行 provider initialize/start 或 resume；
- 创建 body 必须包含 `device_id` 和 `provider`；缺少设备或设备不具备 provider capability 时直接返回可操作错误；
- `GET /api/agent/sessions/{id}`：session projection；
- `GET /api/agent/sessions/{id}/snapshot?after_sequence=`：消息/activity/request 快照；
- `WS /ws/agent/sessions/{id}?after_sequence=`：实时事件；
- `POST /api/agent/sessions/{id}/turns`：发送 prompt/attachments/model/effort；
- `POST /api/agent/sessions/{id}/turns/{turnId}/interrupt`：中断；
- `POST /api/agent/sessions/{id}/requests/{requestId}/approval`：批准/拒绝；
- `POST /api/agent/sessions/{id}/requests/{requestId}/user-input`：回答问题；
- `DELETE /api/agent/sessions/{id}`：显式关闭。

所有 mutation 带 idempotency key；所有请求按 session owner/device/cwd 进行授权。不得返回 raw auth/token/environment。

### 4.6 前端组件结构

建议在 `frontend/src/agent/` 下建立：

- `agent-api.ts`：HTTP/WS contract；
- `agent-store.ts`：snapshot + sequence reducer + reconnect；
- `AgentSessionPane.tsx`：页面壳和 session header；
- `AgentMessagesTimeline.tsx`：复刻 t3code `MessagesTimeline` 行模型和滚动策略；
- `AgentMessageRow.tsx`：user/assistant markdown；
- `AgentActivityGroup.tsx` / `AgentActivityRow.tsx`：tool/step/reasoning/plan；
- `AgentRequestBanner.tsx`：approval/user-input；
- `AgentComposer.tsx`：输入、附件、model/permission、send/interrupt；
- `agent-timeline.ts`：activity → rows、折叠、耗时、失败状态；
- `AgentProviderPicker.tsx`：provider、设备、能力、连接状态和 cwd；
- `AgentSessionStatus.tsx`：显示 bridge instance/generation，区分设备离线和 session 错误。

终端相关组件继续保留并只做解耦：

- `TerminalGrid` / `TerminalPane` / `TerminalTabsBar`：多设备终端 tab/pane 和 xterm 交互；
- `TerminalSession` API/WS：本地与 SSH 终端的创建、输入、resize、snapshot、退出状态；
- `WorkspacePane` / `PreviewPane`：以 `terminal_id + device_id` 作为访问边界；
- `DeviceDashboard`：设备、FRP、SSH、终端和 Agent Runtime bridge 状态分栏显示；
- `DeviceWorld` / `pixel/scene.ts`：以 `sessionKind: terminal | agent` 生成角色，分别渲染类型徽标、状态气泡、hover card 和打开目标；不得再出现 `kind: runtime`。

终端 UI 不应复用 `AgentMessagesTimeline`；Agent UI 也不应从 xterm 内容推断 turn/tool 状态。

可直接复制 t3code 的算法/样式基础：

- `MessagesTimeline.logic.ts` 的 row derivation、live-follow、minimap、折叠 anchor；
- `MessagesTimeline.tsx` 的 `WorkGroupSection`、`PlainWorkEntryRow`、`buildToolCallExpandedBody`、图标映射；
- `session-logic.ts` 中 `WorkLogEntry`、tool lifecycle status、failure/success/neutral 判定；
- `ActivityPayloadProjection.ts` 的 command/MCP/file 输出 slimming。

不可直接复制的部分：

- t3code 的 Effect connection runtime、environment registry、multi-provider orchestration；
- 与本项目设备/终端无关的 desktop/mobile 路由；
- 任何会引用旧 `app/execution` 或保留旧 runtime fallback 的适配代码。

当前 frontend 已经有 Tailwind 4、Lucide、markdown/xterm 依赖，视觉基础足以复刻；应把现有 `index.css` 的深色 token 对齐到 timeline，而不是引入另一套 UI 框架。

## 5. 实施顺序

### Phase 0：冻结合同与基线

- 固定首个 provider bridge 的 app-server 版本和支持的 RPC 方法集合；
- 固定 provider adapter interface，确保后续 provider 不改变 AgentSession/UI contract；
- 写 `docs` 中的 event schema 和状态机；
- 为旧 runtime 的所有导出建立删除清单，禁止新代码引用；
- 录制一组真实事件 fixture：纯文本、command、file change、tool、plan、approval、user-input、interrupt、resume。

### Phase 1：删除旧 runtime

- 从 `app/main.py` 移除 execution/device-runtime 初始化、reconcile、router、bootstrap artifact 和 runtime websocket；
- 删除 `app/execution/` 中 runtime/bridge/reporter/observer/provider hook 全部模块及其测试；
- 从 `frontend/src` 删除 execution state/stream、DeviceRuntimeDialog、RunTimeline/RunTree/RunStatusBadge、runtime API 类型和 DeviceWorld runtime 入口；
- 清理数据库迁移和配置项（不做兼容 fallback；开发数据库需要显式重建/迁移策略）；
- 保留 terminal、workspace、preview 代码，但移除其中对 execution state 的引用；
- 将 `TerminalExecutionLifecycle` 重构为独立 `TerminalLifecycle`，保留本地/SSH 创建、xterm websocket、快照、输入、resize、退出和设备删除清理；
- 保留 `DeviceStore`、FRP monitor、SSH probe、remote shell command 和设备级 terminal listing；删除的只是 runtime/execution 观测链，不是 terminal transport。

### Phase 2：Agent Runtime bridge 和事件存储

- 复制 t3code provider runtime 的核心并替换为本项目的最小 Node bridge；
- 实现 initialize/start/resume/send/interrupt/approval/user-input/close；
- 实现 provider raw notification → stable event mapper；
- 定义并实现 `DeviceConnector` 和 bridge `hello/capabilities/session.open/session.command/session.event`；
- local、SSH 启动、outbound-agent 三种连接模式共用同一 session contract；
- Python 侧实现 bridge supervisor、append-only event store、projection 和 snapshot/WS；
- 用 fixture 和 fake app-server 做端到端测试，不依赖本机登录态。

### Phase 3：消息与 activity UI

- 先实现 snapshot 渲染，再接实时 WS；
- 按 t3code row model 复刻消息、work group、tool call 展开、status icon、plan step、turn fold；
- 加入 live-edge scroll、older history、断线重连、pending request banner；
- 用固定 viewport 和 screenshot 对照 t3code（desktop 1440×900、窄屏 390×844）。

### Phase 4：接入现有设备/终端壳

- 从 provider + 设备 selector + terminal/workspace 选择 `device_id/cwd` 创建 AgentSession；
- session pane 与现有 TerminalPane/Preview 解耦，通过 session id 绑定；
- 保持现有“每台设备开多个终端”的 workflow：设备 dashboard → 创建本地/SSH terminal → TerminalGrid/TerminalPane → xterm websocket；
- 不把终端 stdout、agent scan、execution observation 混入 Agent transcript；其中 agent scan 只能作为终端标题/提示信息，不能作为 AgentSession 状态来源；
- 设备断线只显示连接错误，恢复后按 `device_generation + sequence` resume，不切换旧 runtime 或其他设备；
- 以“新增一个 manifest + 部署通用 bridge agent”验证快速接入新设备，不修改 session/UI 代码。

### Phase 5：验收和清理

- `rg -i 'runtime|execution|RuntimeSession|ExecutionRun'` 只允许出现在明确的 Agent Runtime/terminal 基础设施文档/变量中；
- 删除旧依赖、migration、artifact 和环境变量；
- 完成真实 provider 登录态下的 smoke test，再运行 focused unit/integration tests；
- 逐项核对 t3code 像素基线和交互行为。

## 6. 验收标准

### 会话正确性

- 新建 session 完成 provider `initialize → initialized → start`，UI 显示 ready；
- 刷新/重连使用 provider session/thread id resume，消息不重复、不丢失；
- resume 失败只按 provider 明确定义的 recoverable session 错误新建 session；其他错误显示 error 并停止；
- 一次 turn 的所有 delta、activity、request 都能按 sequence 重放；
- interrupt、approval、user-input 都能完成 provider RPC 闭环。

### UI 正确性

- assistant 文本按 markdown 流式显示，完成后可复制；
- command/file/tool/dynamic tool 分别显示正确图标、标题、摘要和完成/失败状态；
- tool call 可展开原始命令、输出摘要、tool 数据、changed files；
- 多个 tool 默认折叠为 `+N previous tool calls`，展开不跳屏；
- plan step 显示 pending/in progress/completed；
- 用户阅读历史时不会被新 token 强制滚到底部；
- pending approval/user-input 可操作，turn 状态显示 waiting；
- desktop/mobile viewport 下没有文本溢出或按钮遮挡。

### 清理正确性

- 不存在旧 runtime endpoint、旧 websocket、旧 RuntimeSession/Execution snapshot API；
- 不存在旧 runtime fallback；Agent Runtime bridge 不可用时明确报错；
- 每个 AgentSession 的事件都能追溯到唯一 `provider + device_id + bridge_instance_id + device_generation`；
- 新增 provider 或设备只需要 provider adapter/通用 manifest/connector，不需要复制 session/UI 实现；
- 不同设备仍可以创建和恢复多个 TerminalSession，terminal websocket、workspace、preview 不依赖 execution projection；
- 数据库和前端 bundle 中不再包含旧 runtime 代码路径。

## 7. 复制和许可边界

t3code 根目录为 MIT 许可（见 `/home/hsy/Study/Agent/t3code/LICENSE`）。可以在本项目中复制其 provider bridge/UI 实现，但应：

- 保留 MIT copyright/license notice；
- 在复制文件头注明来源和本项目改动；
- 优先复制小而稳定的 provider runtime 协议处理、timeline 算法和 work-log UI 组件；
- 不复制整个 t3code monorepo 或引入其多端连接 runtime；
- 将复制代码和本项目特有代码分开，便于后续同步 provider app-server 协议变化。

## 8. 开始编码前需要确认的唯一架构决策

本方案默认采用 **Node bridge + Python host + React UI**。这是最小风险的落地方式：协议实现最大程度复用 t3code，现有 FastAPI/设备/终端边界不被整体推翻，同时能真正删除旧 runtime。

如果决定把整个后端迁移到 TypeScript/Effect，则可以省掉 bridge，但会把鉴权、设备、终端、workspace、preview 一并迁移，范围显著扩大，不符合“先复刻 provider session/UI”的最小可交付路径。

## 9. 本次实现状态（2026-08）

已落地的目标边界如下：

- `app/agent_runtime/` 提供 provider-neutral `AgentSession`、`AgentTurn`、`AgentMessage`、`AgentActivity`、`AgentRequest`、append-only SQLite event store、snapshot 和 sequence replay。
- `ProviderBridge`/`ProviderBridgeRegistry` 是唯一 provider 扩展点；首个 app-server provider bridge 位于 `app/agent_runtime/providers/codex.py`，只负责把 provider RPC/event 映射成通用事件。
- 新 API 为 `/api/agent/sessions`、`/api/agent/sessions/{id}/turns`、`/api/agent/sessions/{id}/requests/respond` 和 `/ws/agent/sessions/{id}`。
- 旧浏览器 RuntimeSession API、旧 runtime session websocket、旧 RuntimeSession dialog 已删除；未匹配的 `/api/*` 不再回退到 SPA index，而是返回 404。
- 前端 `frontend/src/agent/` 实现 snapshot/WS session pane、message timeline、activity/tool rows、pending request banner 和 composer；房间角色使用 `terminal | agent`，两者打开目标和状态来源分离。
- TerminalManager、SSH/FRP、xterm websocket、workspace、preview 保持原路径，且相关回归测试通过。

验证结果：前端 `npm --prefix frontend run build` 通过；新增 Agent Runtime/API 测试 7 项通过；终端、workspace、preview 串行 focused tests 通过；全量旧测试仅剩 2 项针对已删除 RuntimeSession API 的预期失败，未恢复这些 fallback。
