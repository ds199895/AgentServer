# AgentServer

AgentServer 是一个集中式 FRP 设备管理与 Web SSH 终端。每台受管设备只需运行 SSH
Server 和一个 frpc 服务；设备列表、在线/离线记录、SSH 探测、登录鉴权和浏览器终端都
运行在 frps 所在服务器。

项目同时保留原有的本地 PTY 模式，方便在单机上运行 Codex 或普通 Shell。生产集中部署
可以通过 `ENABLE_LOCAL_TERMINALS=0` 禁用本地 PTY。

## 功能

- 从 frps Dashboard 自动发现 TCP 代理，并持久保存设备记录。
- 分别展示“FRP 隧道在线”和“SSH 服务可用”，避免把端口在线误判成终端可用。
- 保存最后在线时间、frpc 版本、代理名称、远端端口和检测错误。
- 手动注册、编辑、检测和删除设备。
- 可为每台设备选择系统默认、PowerShell 或 CMD 作为 SSH 登录后的远程 Shell。
- 通过服务器系统 SSH 客户端建立 PTY，再经 WebSocket 连接 xterm.js。
- SSH 使用密钥、`BatchMode`、Keepalive 和独立 `known_hosts`，首次连接默认 TOFU 固定主机密钥。
- PBKDF2 密码、HttpOnly 签名 Cookie，并要求显式配置初始管理员密码。
- 支持多个并行 SSH/本地终端和断线后输出回放。
- 生产环境使用独立 tmux 服务托管终端，并在 SQLite 保存会话元数据；部署重启后原标签可自动恢复。
- 每个终端绑定独立的只读工作区：本地终端使用受根目录约束的文件系统访问，SSH 终端通过同一设备身份使用 SFTP。
- 浏览器内置文件浏览与 Artifact 面板，可预览图片、受限文本和 PDF；HTML、SVG 与未知格式只提供下载。
- 持久化结构化 Artifact 事件，并通过 WebSocket 实时推送 Agent 新建或更新的文件。
- `read-image` 将验证后的图片保存为 SHA-256 内容寻址附件，返回可供模型消费和会话回放的结构化内容块。
- 提供 systemd 服务、frpc/frps 示例和服务器安装脚本。

## Roadmap

### 已完成

- 集中管理 FRP 设备，自动同步在线状态并检测 SSH 可用性。
- 在浏览器中创建、切换和恢复多个 SSH/本地终端，支持断线重连与历史输出回放。
- 使用独立 tmux 服务持久托管终端任务，Web 服务更新不会终止正在运行的任务。
- 提供像素风房间 Canvas，将设备和终端会话映射为可交互的小人，并可从场景直接跳转终端。
- 通过 SSH 临时隧道和隔离子域名预览设备本地开发服务，支持 HTTP、WebSocket 与 HMR。
- 自动从终端输出识别 Vite、Next.js、Storybook 等本地 Web 服务，主动探活并提供一键预览；服务停止后自动关闭关联预览隧道。
- 提供设备端一键安装脚本、登录鉴权、HTTPS 部署支持，以及桌面端和移动端基础适配。

### TODO

1. 添加终端 Agent 识别，自动区分 Codex、Kimi、Claude 等编码 Agent 和普通 Shell。
2. 丰富 Agent 状态模型，统一管理任务进度、等待输入、提示、异常和告警，并提供聚合视图。
3. 适配思考、编码、读取、等待、报错、完成等多状态动画，让场景更有游戏性和真实感。
4. 建立 Tool、Agent 状态/行为与场景物件的交互映射，例如读取文件时从书柜取书、搜索时查看地图、执行命令时操作工作台。

### 有趣的延伸

- 增加 Agent 事件时间线、任务回放和关键节点快照，方便复盘长任务与定位异常。
- 展示多 Agent 协作关系，让委派、并行执行、交接和结果汇总在房间中可视化。
- 支持自定义房间、工位、角色外观和场景主题，使不同设备与项目拥有独立空间。
- 加入任务里程碑、成就、连续稳定运行和空间成长等轻量游戏机制。
- 提供插件式场景行为 API，让新的 Agent、Tool 和自动化工作流可以自行注册状态与动画。

## 架构

工作区、事件和模型内容之间采用可替换的 provider/adapter 边界，思路借鉴
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的“Everything is a
Plugin”，但不引入其运行时依赖：AgentServer 仍以现有 FastAPI、TerminalManager 和 React
界面为宿主。

```text
Device A: sshd + frpc ─┐
Device B: sshd + frpc ─┼──> frps ──> AgentServer ──> Browser/xterm.js
Device C: sshd + frpc ─┘       │           │
                               │           ├── TerminalManager ── PTY/tmux/SSH
                               │           ├── WorkspaceService ─ Local/SFTP ─ Browser file pane
                               │           ├── ArtifactEventStore ──────────── Browser event pane
                               │           └── AttachmentStore (SHA-256) ───── read-image/model blocks
                               └── local-only Dashboard API
```

FRP 只负责 TCP 传输。每台设备仍需启用 OpenSSH Server，并将 AgentServer 的 SSH 公钥
加入目标账户的 `authorized_keys`。

### 工作区、Artifact 与图片附件

终端会话除了 PTY 数据通道，还保存一个归属于“登录用户 + 终端 ID”的 `WorkspaceBinding`。
本地终端默认把 `TERMINAL_CWD` 作为根目录；创建终端时可通过 `workspace_root` 选择其下的
子目录。SSH 终端默认根为远端登录目录 `.`，也可指定远端目录，并通过与终端相同的设备、
私钥和 `known_hosts` 建立按需 SFTP 连接。工作区接口只读，不会把服务器本地路径误当作
SSH 终端的远端路径。远端自定义 `workspace_root` 只约束文件面板，不会暗中向已经启动的
交互 shell 注入 `cd`；没有可靠 cwd 协议时，API 会把远端 `current_path` 保持为空。

当前认证和设备管理仍采用**单管理员控制面**：`owner` 绑定用于阻止终端、文件授权和附件在
会话之间串用，并为将来扩展多租户保留边界，但设备与预览资源尚未宣称支持互不信任的多用户
租户。若需要开放给多个独立用户，必须先把设备和预览的所有权也纳入授权链路。

文件读取分为两步：先把根目录相对路径解析为短时、不可猜测的 `FileGrant`，再携带该授权
及 `terminal_id` 读取内容。授权绑定用户、终端、工作区实例和文件版本；过期、文件变化或
重新绑定都会失效。路径拒绝绝对地址、`..` 和符号链接，读取有字节/条目上限并支持 HTTP
`Range`。服务端依据实际内容嗅探类型，响应始终带 `nosniff`；HTML、SVG 和未知内容不会
内联到主站上下文。

本地提供器用设备号、inode、纳秒级 mtime/ctime 和大小复核版本。SFTP v3 通常只暴露秒级
mtime 和大小，因此远端授权还会保存并在每次读取时复核一个有界内容探针；它是短时、
version-checked 的授权，不是完整文件快照。若远端文件在同一秒被等长改写、且探针覆盖部分
保持不变，协议元数据无法证明后半段未变化；对必须严格不可变的图片回放，应使用
`read-image` 生成 SHA-256 附件。

`ArtifactEvent` 是独立于 xterm 字节流的版本化事件，持久保存于 SQLite，并通过
`/ws/events/{terminal_id}` 先发送快照、再发送实时增量。带登录 Cookie 的 Agent 适配器可
调用 HTTP API；运行在 PTY 内、没有 HTTP 凭据的 Agent 可以输出以下纯文本标记。它可穿过
生产环境的 tmux；结尾的回车与擦行序列只用于避免在终端中留下长标记：

```text
__AGENTSERVER_ARTIFACT__:<无填充的 base64url(JSON)>:AGENTSERVER_END__ CR ESC [ 2 K
```

JSON 至少包含根目录相对的 `path`，并可包含 `type`、`name`、`media_type`、`size`、
`kind` 和 `version`。这条消息只登记元数据，不绕过文件网关；用户打开文件时仍会生成新的
短时授权。生产 `tmux` 后端通过 `pipe-pane` 把应用原始 pane 输出送入每会话独立的私有
FIFO，机器解析不依赖 attach 客户端的屏幕 redraw；即使标记在一次刷新前被擦除也不会漏掉，
普通 shell 输出仍只向浏览器广播一次。`direct` 后端直接解析 PTY 字节流。两种后端都兼容
`ESC ] 633 ; artifact ; <base64url> BEL` OSC 形式；终端来源事件先进入有界队列，每个终端
最多接收每秒 20 条，再由后台线程写入 SQLite，避免文件事件阻塞 PTY 数据通道。

工作区文件引用表示“这个路径当前的内容”，适合快速浏览；`read-image` 则读取并验证真实
图片格式、字节数和像素数，把 PNG/JPEG/GIF/WebP 保存到 `DATA_DIR/attachments`，生成
`sha256:<digest>` 的不可变 `AttachmentRef`，随后才提交带附件引用的 Artifact 事件。附件
只有被同一用户、同一终端的持久事件引用时才能读取，因此会话回放不依赖原文件仍然存在，
也不会把短时文件授权或图片 base64 写入事件日志。`POST .../read-image` 同时返回显式标记为
`openai-responses` 的 `model_content`：其中的 `input_image` 是只存在于本次鉴权响应中的 data
URL，可由 OpenAI Responses 兼容适配器直接作为输入内容提交。AgentServer 不会向任意外部
CLI 自动注入工具；Codex、Claude 等独立 Agent 仍需一个适配器来调用此端点并把返回内容交给
各自模型 API。

## 目录

```text
app/auth.py                 用户、密码与 Cookie 签名
app/artifacts.py            Artifact 事件日志、内容寻址图片附件与 read-image 内容块
app/devices.py              设备库、FRP 同步和 SSH Banner 探测
app/terminal.py             本地/SSH PTY 生命周期与 WebSocket 广播
app/main.py                 FastAPI API、生命周期和 SSH 启动参数
app/workspace.py            根目录约束的 Local/SFTP 文件提供器与短时文件授权
frontend/                   React、TypeScript、Tailwind CSS、shadcn/ui、xterm.js 源码
web_dist/                   已构建的生产前端，服务器无需 Node.js
deploy/                     systemd 与生产环境示例
frpc.example.toml           每台设备的 SSH 穿透模板
frps.example.toml           frps 安全配置模板
scripts/install_server.sh   Ubuntu 服务器安装脚本
scripts/build_release.sh    从干净 commit 构建不可变发布制品
scripts/deploy_release.sh   原子切换、smoke 与失败回滚
```

生产环境中的终端分为两层：`agentserver-tmux.service` 持有真正的 shell、SSH 和 Codex
进程，`agentserver.service` 只负责 WebSocket attach。更新代码并重启 Web 服务只会断开
短暂的浏览器连接，不会终止 tmux 中的任务；浏览器重连后会使用 SQLite 中保存的终端 ID
恢复原标签与滚动历史。显式点击关闭终端时，才会删除数据库记录并执行
`tmux kill-session`。

## 本地开发

需要 Python 3.10+ 和 Node.js 20.19+。

```bash
cp .env.example .env
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
npm --prefix frontend install
npm --prefix frontend run build
bash scripts/start.sh
```

复制 `.env.example` 后必须设置至少 8 位的 `ADMIN_PASSWORD`，再启动服务。默认访问地址为
`http://127.0.0.1:18088`，初始管理员用户名默认为 `admin`。

## 集中服务器部署

推荐安装路径 `/opt/agentserver`，状态目录 `/var/lib/agentserver`，配置目录
`/etc/agentserver`。

```bash
git clone https://github.com/ds199895/AgentServer.git /opt/agentserver
cd /opt/agentserver
bash scripts/install_server.sh
```

可以先自动生成生产配置和随机初始密码，再运行安装脚本：

```bash
bash scripts/configure_server.sh
bash scripts/install_server.sh
```

初始登录信息只写入服务器的 `/root/agentserver-initial-credentials.txt`（权限 `0600`）。
也可以跳过自动配置，让安装脚本生成环境模板，再手动填写所有 `CHANGE_ME`。

生产服务监听 `0.0.0.0:18100`。云安全组放行 18100 后可直接访问：

```text
http://101.43.103.46:18100
```

直接 HTTP 不会加密登录凭据或终端数据。正式使用应配置 HTTPS；在 HTTPS 完成前，也可以
不开放公网端口并使用 SSH 隧道：

```bash
ssh -L 18100:127.0.0.1:18100 root@101.43.103.46
```

然后打开 `http://127.0.0.1:18100`。如需公网访问，应在前方增加 HTTPS 反向代理、MFA
和访问来源限制，不要直接开放明文 HTTP。

安装脚本会生成：

```text
/var/lib/agentserver/ssh/id_ed25519
/var/lib/agentserver/ssh/id_ed25519.pub
/var/lib/agentserver/ssh/known_hosts
/var/lib/agentserver/tmux/agentserver.sock
```

安装脚本会安装 tmux，并启用两个服务：

```bash
systemctl status agentserver-tmux.service
systemctl status agentserver.service
```

日常部署不要直接复制源码或 `web_dist`。应从干净的 Git commit 构建完整制品，再通过原子
发布脚本切换版本；该过程只重启 `agentserver.service`，不要重启 `agentserver-tmux.service`。
宿主机完整
重启后 tmux 进程无法继续存在；此时保存的标签会显示为已退出，而不是伪装成仍在运行。

```bash
# 开发机或 CI：工作区必须完全干净
scripts/build_release.sh
scp dist/agentserver-<sha>.tar.gz* root@server:/tmp/

# 服务器：校验、安装依赖、原子切换 current，失败自动回滚
sudo /opt/agentserver/current/scripts/deploy_release.sh /tmp/agentserver-<sha>.tar.gz
```

每个制品同时携带后端 `BUILD_SHA`、前端编译版本和 `web_dist/build.json`。生产启动时三者
不一致会拒绝启动；部署后 smoke 会检查版本、静态资源、登录和终端 API 契约。发布目录位于
`/opt/agentserver/releases/<sha>`，`/opt/agentserver/current` 始终原子指向当前版本。

把 `.pub` 内容加入每台设备目标 SSH 用户的 `~/.ssh/authorized_keys`。

## 设备端 frpc

登录管理平台后打开“安装客户端”页面，填写唯一设备 ID、远端端口和 SSH 用户，即可下载：

- Linux/macOS Shell 自动安装器；
- Windows PowerShell 自动安装器；
- 手动 frpc 配置模板；
- AgentServer SSH 公钥。

安装器会识别操作系统和 CPU 架构、验证官方 SHA-256、配置 SSH 授权密钥并注册开机服务。
FRP token 在运行时隐藏输入，不包含在网页或下载文件中。
如果设备已经运行其他 frpc，使用 `--merge-existing /path/to/frpc.toml`。安装器会备份
原配置、保留现有 token 与 proxy、追加 SSH proxy、校验完整配置，并把原进程迁移为
systemd/launchd 常驻服务。旧版 frpc 会原子升级到 0.69.0 并保留二进制备份，继续保持
每台设备只有一个 frpc。脚本也能识别并修复上一次创建但启动失败的 systemd 单元。
Shell 安装器支持 `--dry-run`：无需 root，只执行平台识别、下载、SHA-256 校验、配置生成
和 `frpc verify`，不会修改系统或重启已有服务。

将 `frpc.example.toml` 安装为 `/etc/frp/frpc.toml`，将 FRP token 放在权限为 `0600`
的 `/etc/frp/token`，并创建：

```dotenv
DEVICE_ID=device-001
FRP_SSH_REMOTE_PORT=20001
```

保存为 `/etc/frp/device.env`。然后安装 `deploy/frpc.service`：

```bash
install -m 0644 deploy/frpc.service /etc/systemd/system/frpc.service
systemctl daemon-reload
systemctl enable --now frpc
```

每台设备必须使用唯一 `DEVICE_ID` 和唯一 `FRP_SSH_REMOTE_PORT`。代理最终名称为
`{DEVICE_ID}.ssh`，例如 `device-001.ssh`。

macOS 需要启用“系统设置 → 通用 → 共享 → 远程登录”；Windows 需要启用 OpenSSH
Server。Linux 安装器会启用已有的 `ssh`/`sshd`；Ubuntu、Debian、Fedora、RHEL 等系统
尚未安装 OpenSSH Server 时，还会通过 `apt-get`、`dnf` 或 `yum` 自动安装。

## 设备开发服务预览

AgentServer 可以通过设备现有的 SSH 入口建立临时本地转发，预览仅监听在设备
`127.0.0.1` 上的 Vite、Next.js、Storybook 等开发服务。登录后可从设备列表或终端设备组
点击“预览”，手动填写端口；预览面板支持刷新、响应式宽度、全屏、新窗口打开和停止隧道。

终端启动开发服务并输出 `http://localhost:<port>`、`http://127.0.0.1:<port>` 或明确的
`listening/running/ready ... port <port>` 提示后，AgentServer 会自动识别端口，并通过设备
现有 SSH 入口主动检查它是否为可访问的 HTTP(S) 服务。终端右下角会显示“正在检查 / 运行中 /
已停止”，运行中时可一键打开预览。相同终端和端口会复用已有预览；连续探测失败后，关联
预览会自动关闭。手动填写端口的入口仍然保留，供未输出标准地址的服务使用。

为保持根路径资源、前端路由和 HMR WebSocket 兼容，同时隔离不受信任的开发页面，生产
环境必须使用独立泛域名，例如：

```text
*.preview.metakroma.com -> 101.43.103.46
PREVIEW_PUBLIC_ORIGIN=https://preview.metakroma.com
```

泛域名证书通常需要 DNS-01 验证。可参考 `deploy/preview.nginx.example` 配置 Nginx。
每个预览使用独立子域名和短时访问票据，不会收到 AgentServer 主站的登录 Cookie；隧道
在关闭预览、删除设备或超过 `PREVIEW_IDLE_TIMEOUT` 无访问时自动回收。

当前 `*.preview.metakroma.com` 证书通过手动 DNS-01 签发，不能无人值守自动续期。到期前
需要更新 `_acme-challenge.preview.metakroma.com` TXT 记录并重新执行 ACME 验证。

## 关键环境变量

| 变量 | 说明 |
| --- | --- |
| `ENVIRONMENT` | 生产部署设为 `production` |
| `DATA_DIR` | SQLite、会话密钥和运行数据目录 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 首次创建管理账户 |
| `FRPS_DASHBOARD_URL` | frps Dashboard，例如 `http://127.0.0.1:7500` |
| `FRPS_DASHBOARD_USER/PASSWORD` | Dashboard Basic Auth |
| `FRPS_SYNC_INTERVAL` | 状态同步周期，默认 15 秒 |
| `FRPS_AUTO_DISCOVER` | 是否自动保存未知 TCP 代理 |
| `FRP_PROXY_HOST` | AgentServer 访问代理端口的主机，通常是 `127.0.0.1` |
| `SSH_PRIVATE_KEY` | 用于登录设备的私钥 |
| `SSH_KNOWN_HOSTS` | 独立主机密钥库 |
| `SSH_STRICT_HOST_KEY` | 默认 `accept-new`；完成首次固定后可改为 `yes` |
| `SSH_CONNECT_TIMEOUT` | SSH 终端工作区建立 SFTP 连接的超时秒数，默认 10 |
| `SFTP_OPERATION_TIMEOUT` | SFTP stat/list/read 单次远端操作超时秒数，默认 15；超时后断开并允许重连 |
| `ENABLE_LOCAL_TERMINALS` | 集中部署建议设为 `0` |
| `TERMINAL_BACKEND` | `tmux` 可跨 AgentServer 部署保存进程；`direct` 为开发兼容模式 |
| `TMUX_SOCKET` | 独立 tmux 服务的 socket，生产默认 `/var/lib/agentserver/tmux/agentserver.sock` |
| `FILE_GRANT_TTL` | 文件解析授权的有效秒数，默认 120 |
| `MAX_WORKSPACE_FILE_BYTES` | 可生成预览授权的最大文件字节数，默认 32 MiB |
| `MAX_WORKSPACE_READ_BYTES` | 单次完整或 Range 响应的最大字节数，默认 32 MiB |
| `MAX_WORKSPACE_LIST_ENTRIES` | 单次目录列表最大条目数，默认 1000 |
| `MAX_IMAGE_ATTACHMENT_BYTES` | `read-image` 可持久化的最大图片字节数，默认 5 MiB |
| `MAX_IMAGE_ATTACHMENT_PIXELS` | 图片允许的最大像素总数，默认 4000 万 |
| `ARTIFACT_INGEST_QUEUE_SIZE` | 终端输出 Artifact 待持久化队列上限，默认 1024 |
| `COOKIE_SECURE` | HTTPS 部署设为 `1` |
| `PREVIEW_PUBLIC_ORIGIN` | 预览泛域名基础 Origin，生产为 `https://preview.metakroma.com` |
| `PREVIEW_IDLE_TIMEOUT` | 预览无访问后的自动回收秒数，默认 1800 |
| `SERVICE_PROBE_INTERVAL` | 自动发现服务的探活间隔秒数，默认 10，最小 2 |
| `SERVICE_PROBE_TIMEOUT` | 单次 SSH/HTTP 探活最长秒数，默认 6 |
| `SERVICE_PROBE_FAILURES` | 连续失败多少次后标记停止并回收预览，默认 2 |
| `SERVICE_PROBE_CONCURRENCY` | 同时执行的服务探活数，默认 3 |
| `SERVICE_PROCESS_SCAN_INTERVAL` | 远端监听进程扫描间隔秒数，默认 10 |
| `SERVICE_PROCESS_SCAN_TIMEOUT` | 单台设备监听扫描超时秒数，默认 5 |
| `SERVICE_PROCESS_SCAN_CONCURRENCY` | 同时扫描的设备数，默认 3 |
| `SERVICE_PROCESS_PROBE_TIMEOUT` | 进程来源候选的单协议 HTTP 探测秒数，默认 2 |
| `SERVICE_PROCESS_MISSING_SCANS` | 连续多少轮未监听后自动下线，默认 2 |
| `SERVICE_PROCESS_MIN_PORT` | 自动发现的最低监听端口，默认 1024 |

## API

- `GET /api/health`
- `GET/POST /api/devices`
- `PUT/DELETE /api/devices/{id}`
- `POST /api/devices/sync`
- `POST /api/devices/{id}/probe`
- `POST /api/devices/{id}/terminals`（可选 `workspace_root` 指定远端 SFTP 根）
- `POST /api/devices/{id}/previews`
- `GET/POST /api/terminals`（创建本地终端时可选 `workspace_root`，且必须位于 `TERMINAL_CWD` 内）
- `DELETE /api/terminals/{id}`
- `GET /api/terminals/{id}/workspace?path={relative_path}`
- `POST /api/terminals/{id}/files/resolve`，请求体为 `{"path":"relative/file.png"}`
- `GET /api/files/{grant_id}/content?terminal_id={id}`（支持 `Range` 与 `If-None-Match`）
- `GET/POST /api/terminals/{id}/artifacts`（GET 可选 `after_sequence`）
- `POST /api/terminals/{id}/read-image`，请求体为 `{"path":"relative/image.png"}`
- `GET /api/terminals/{id}/attachments/{sha256_id}`
- `GET /api/previews`
- `POST /api/previews/{id}/ticket`
- `DELETE /api/previews/{id}`
- `WS /ws/terminal/{id}`
- `WS /ws/events/{id}`
- `GET /downloads/install-frpc-ssh.sh`
- `GET /downloads/install-frpc-ssh.ps1`
- `GET /downloads/frpc.example.toml`
- `GET /downloads/agentserver-ssh-key.pub`

除健康检查与登录外，设备、下载、终端和 WebSocket 接口都要求有效登录 Cookie。
文件授权和附件读取还会校验 Cookie 所属用户与终端所有者，越权查询统一表现为不存在。

HTTP Agent 适配器可用以下请求登记 Artifact；服务端会补充事件 ID、单调递增的
`sequence`、`created_at` 和 `schema_version`：

```http
POST /api/terminals/TERMINAL_ID/artifacts
Content-Type: application/json

{
  "type": "created",
  "path": "output/chart.png",
  "name": "chart.png",
  "media_type": "image/png",
  "size": 12345,
  "kind": "file",
  "version": "agent-defined-version",
  "source": "my-agent"
}
```

`read-image` 的成功响应同时包含持久事件、工作区文件引用、不可变附件引用，以及用于浏览器
回放的 `content`。另有 `model_content_format: "openai-responses"` 和可直接提交给 Responses
适配器的 `model_content`；其 `input_image.image_url` 内含本次响应限定的图片数据。普通浏览
预览仍使用短时 `FileGrant`，不会产生模型图片输入成本或把 base64 写进 SQLite。

## 验证

```bash
python -m unittest discover -s tests -v
npm --prefix frontend run build
bash -n scripts/*.sh
```

GitHub Actions 会在每次 push 和 pull request 上执行这些检查，并运行一个浏览器契约回归：
契约会创建真实本地终端，验证工作区列表、短时授权、带 `terminal_id` 的 Range 内容读取、
Artifact 写入与持久快照、`read-image` 模型块和不可变附件，并在真实页面中打开文本、图片与
Artifact 面板；同时模拟旧后端省略终端的 `services` 字段，终端页不得出现未捕获 JavaScript
错误。
仓库设置中应将 `CI / verify` 配置为目标分支的 required status check，并禁止绕过保护规则。

`main` 的 push 和手动触发还会在 `verify` 通过后构建唯一的 release artifact，并通过
GitHub `production` Environment 的专用受限 SSH key 部署到生产服务器。该 key 在服务器
的 `authorized_keys` 中绑定 `scripts/github_deploy_receiver.sh` forced command，不能打开
交互式 root shell。服务器会再次校验 SHA-256 和 commit SHA，再执行原子切换、登录态 smoke
和失败回滚。服务器 smoke 直接走 `https://agent.metakroma.com`，因此 Nginx、TLS、版本、
登录或终端 API 任一失败都会触发回滚；GitHub runner 最后再从公网 `/api/version` 独立
确认生产版本。

为适应 GitHub Runner 到生产服务器的跨境链路，artifact 会拆成 1 MiB 分块，通过 6 条
受限 SSH 连接并发上传。每个分块、重组后的传输包和内部 release 都分别校验 SHA-256；
只有全部分块到齐且 commit SHA 一致时才允许 finalize 获取部署锁并切换版本。

生产 release 还会打包与服务器 Python 3.10 兼容的 wheelhouse，部署时使用 `--no-index`
离线安装；同一个 commit 不会因为 PyPI 上游版本或网络变化得到不同依赖。

Environment 需要以下配置：`PROD_HOST`、`PROD_SSH_PORT`、`PROD_SSH_USER`、
`PROD_BASE_URL` variables，以及 `PROD_SSH_KEY`、`PROD_KNOWN_HOSTS` secrets。生产服务
只监听 `127.0.0.1:18100`，公网流量继续由 Nginx HTTPS 入口代理。

真实配置、token、数据库、日志、PID、SSH 密钥和前端依赖均被 `.gitignore` 排除。
