#!/usr/bin/env sh
set -eu

FRP_VERSION="${FRP_VERSION:-0.69.0}"
FRP_SERVER="${FRP_SERVER:-101.43.103.46}"
FRP_SERVER_PORT="${FRP_SERVER_PORT:-7000}"
DEVICE_ID="${DEVICE_ID:-}"
REMOTE_PORT="${FRP_SSH_REMOTE_PORT:-}"
SSH_USER_NAME="${SSH_USER_NAME:-${SUDO_USER:-root}}"
DRY_RUN=0
MERGE_CONFIG=""
EXISTING_PID=""
EXISTING_OWNER=""
EXISTING_GROUP=""
EXISTING_CWD=""
EXISTING_BIN=""
AGENTSERVER_PUBLIC_KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAntuN6lYuNHu8i69zyvGFUlRPm+QL/Ek9ntvubJLqyM agentserver-fleet'

usage() {
  cat <<'EOF'
安装 Linux/macOS 的 frpc SSH 穿透服务。

用法：
  sudo sh install_frpc_ssh.sh --device-id DEVICE --remote-port PORT [选项]

必填：
  --device-id ID       唯一设备 ID，2-64 位字母、数字、点、下划线或连字符
  --remote-port PORT   服务器端唯一端口，范围 20000-29999

选项：
  --ssh-user USER      AgentServer 登录此设备使用的本地用户
  --server HOST        frps 地址，默认 101.43.103.46
  --server-port PORT   frps 控制端口，默认 7000
  --version VERSION    frpc 版本，默认 0.69.0
  --dry-run            下载、校验并验证配置，但不修改系统
  --merge-existing FILE
                       备份并合并到正在运行的现有 frpc 配置
  --help               显示帮助

FRP token 会在终端中隐藏输入，也可通过 FRP_TOKEN 环境变量传入。
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --device-id) DEVICE_ID="${2:-}"; shift 2 ;;
    --remote-port) REMOTE_PORT="${2:-}"; shift 2 ;;
    --ssh-user) SSH_USER_NAME="${2:-}"; shift 2 ;;
    --server) FRP_SERVER="${2:-}"; shift 2 ;;
    --server-port) FRP_SERVER_PORT="${2:-}"; shift 2 ;;
    --version) FRP_VERSION="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --merge-existing) MERGE_CONFIG="${2:-}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 2 ;;
  esac
done

if [ "$DRY_RUN" -ne 1 ] && [ "$(id -u)" -ne 0 ]; then
  echo "请使用 sudo 或 root 运行此脚本"
  exit 1
fi
if [ -n "$MERGE_CONFIG" ]; then
  case "$MERGE_CONFIG" in
    /*) ;;
    *) MERGE_CONFIG="$(pwd)/$MERGE_CONFIG" ;;
  esac
  if [ ! -f "$MERGE_CONFIG" ]; then
    echo "现有配置不存在: $MERGE_CONFIG"
    exit 2
  fi
  MERGE_CONFIG="$(cd "$(dirname "$MERGE_CONFIG")" && pwd -P)/$(basename "$MERGE_CONFIG")"
fi

if ! printf '%s' "$DEVICE_ID" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$'; then
  echo "--device-id 格式无效"
  exit 2
fi
if ! printf '%s' "$SSH_USER_NAME" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'; then
  echo "--ssh-user 格式无效"
  exit 2
fi
if ! printf '%s' "$FRP_SERVER" | grep -Eq '^[A-Za-z0-9._:-]+$'; then
  echo "--server 格式无效"
  exit 2
fi
if [ "$FRP_VERSION" != 0.69.0 ]; then
  echo "此安装器只包含 frp 0.69.0 的官方校验值"
  exit 2
fi
case "$REMOTE_PORT" in
  ''|*[!0-9]*) echo "--remote-port 必须是数字"; exit 2 ;;
esac
if [ "$REMOTE_PORT" -lt 20000 ] || [ "$REMOTE_PORT" -gt 29999 ]; then
  echo "--remote-port 必须位于 20000-29999"
  exit 2
fi
case "$FRP_SERVER_PORT" in
  ''|*[!0-9]*) echo "--server-port 必须是数字"; exit 2 ;;
esac

OS_NAME=$(uname -s)
MACHINE_ARCH=$(uname -m)
case "$OS_NAME" in
  Linux) FRP_OS=linux; CONFIG_DIR=/etc/frp ;;
  Darwin) FRP_OS=darwin; CONFIG_DIR=/usr/local/etc/frp ;;
  *) echo "不支持的系统: $OS_NAME"; exit 3 ;;
esac

FOUND_EXISTING=0
if [ -n "$MERGE_CONFIG" ] && [ "$OS_NAME" = Linux ] && systemctl cat frpc-agentserver.service >/dev/null 2>&1; then
  UNIT_FILE=$(systemctl show frpc-agentserver.service -p FragmentPath --value)
  EXISTING_OWNER=$(sed -n 's/^User=//p' "$UNIT_FILE" | head -n 1)
  EXISTING_CWD=$(sed -n 's/^WorkingDirectory=//p' "$UNIT_FILE" | head -n 1)
  EXISTING_BIN=$(sed -nE 's|^ExecStart=([^[:space:]]+).*|\1|p' "$UNIT_FILE" | head -n 1)
  PROCESS_CONFIG_ABS=$(sed -nE 's|^ExecStart=.*[[:space:]]-c[[:space:]]+([^[:space:]]+).*|\1|p' "$UNIT_FILE" | head -n 1)
  EXISTING_PID=$(systemctl show frpc-agentserver.service -p MainPID --value)
  if [ "$EXISTING_PID" = 0 ]; then EXISTING_PID=""; fi
  if [ -z "$EXISTING_OWNER" ] || [ -z "$EXISTING_CWD" ] || [ -z "$EXISTING_BIN" ] || [ -z "$PROCESS_CONFIG_ABS" ]; then
    echo "现有 frpc-agentserver.service 信息不完整，无法自动修复"
    exit 6
  fi
  EXISTING_GROUP=$(id -gn "$EXISTING_OWNER")
  PROCESS_CONFIG_ABS="$(cd "$(dirname "$PROCESS_CONFIG_ABS")" && pwd -P)/$(basename "$PROCESS_CONFIG_ABS")"
  if [ "$PROCESS_CONFIG_ABS" != "$MERGE_CONFIG" ]; then
    echo "systemd 单元配置与传入路径不一致: $PROCESS_CONFIG_ABS"
    exit 6
  fi
  FOUND_EXISTING=1
  echo "检测到已有 frpc-agentserver.service，将修复并升级它"
fi

if [ "$FOUND_EXISTING" -eq 0 ] && command -v pgrep >/dev/null 2>&1 && pgrep -x frpc >/dev/null 2>&1; then
  FRPC_PIDS=$(pgrep -x frpc)
  PID_COUNT=$(printf '%s\n' "$FRPC_PIDS" | wc -l | tr -d ' ')
  if [ "$PID_COUNT" -ne 1 ]; then
    echo "检测到多个 frpc 进程，无法安全自动合并"
    exit 6
  fi
  EXISTING_PID=$FRPC_PIDS
  MANAGED=0
  if [ "$OS_NAME" = Linux ] && systemctl is-active --quiet frpc-agentserver.service 2>/dev/null; then MANAGED=1; fi
  if [ "$OS_NAME" = Darwin ] && launchctl print system/com.agentserver.frpc >/dev/null 2>&1; then MANAGED=1; fi
  if [ "$MANAGED" -ne 1 ]; then
    if [ -n "$MERGE_CONFIG" ]; then
      EXISTING_OWNER=$(ps -p "$EXISTING_PID" -o user= | awk '{print $1}')
      EXISTING_GROUP=$(id -gn "$EXISTING_OWNER")
      PROCESS_COMMAND=$(ps -ww -p "$EXISTING_PID" -o command=)
      PROCESS_CONFIG=$(printf '%s\n' "$PROCESS_COMMAND" | sed -nE 's/.*[[:space:]]-c[[:space:]]+([^[:space:]]+).*/\1/p')
      if [ "$OS_NAME" = Linux ]; then
        EXISTING_CWD=$(readlink "/proc/$EXISTING_PID/cwd")
        EXISTING_BIN=$(readlink "/proc/$EXISTING_PID/exe")
      else
        EXISTING_CWD=$(lsof -a -p "$EXISTING_PID" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)
        EXISTING_BIN=$(lsof -a -p "$EXISTING_PID" -d txt -Fn 2>/dev/null | sed -n 's/^n//p' | grep '/frpc$' | head -n 1)
      fi
      if [ -z "$EXISTING_OWNER" ] || [ -z "$EXISTING_CWD" ] || [ -z "$EXISTING_BIN" ] || [ -z "$PROCESS_CONFIG" ]; then
        echo "无法解析现有 frpc 进程，请手动合并配置"
        exit 6
      fi
      case "$PROCESS_CONFIG" in
        /*) PROCESS_CONFIG_ABS=$PROCESS_CONFIG ;;
        *) PROCESS_CONFIG_ABS="$EXISTING_CWD/$PROCESS_CONFIG" ;;
      esac
      PROCESS_CONFIG_ABS="$(cd "$(dirname "$PROCESS_CONFIG_ABS")" && pwd -P)/$(basename "$PROCESS_CONFIG_ABS")"
      if [ "$PROCESS_CONFIG_ABS" != "$MERGE_CONFIG" ]; then
        echo "传入配置与运行中进程不一致: $PROCESS_CONFIG_ABS"
        exit 6
      fi
      echo "将合并到现有 frpc: $MERGE_CONFIG"
    elif [ "$DRY_RUN" -eq 1 ]; then
      echo "检测到已有 frpc；dry-run 将继续验证，但不会修改或重启它。"
    else
      echo "检测到已有 frpc 进程。为保证每台设备只有一个 frpc，本脚本不会创建第二个服务。"
      echo "请把 SSH proxy 合并到现有配置，或先停止旧 frpc 后重新运行。"
      exit 6
    fi
  fi
  FOUND_EXISTING=1
elif [ "$FOUND_EXISTING" -eq 0 ] && [ -n "$MERGE_CONFIG" ]; then
  echo "--merge-existing 要求对应 frpc 正在运行"
  exit 6
fi

if [ -z "$MERGE_CONFIG" ]; then
  if [ -z "${FRP_TOKEN:-}" ]; then
    if [ ! -t 0 ]; then
      echo "非交互运行时必须通过 FRP_TOKEN 提供 token"
      exit 2
    fi
    printf '请输入 FRP token（输入不会显示）: '
    trap 'stty echo 2>/dev/null || true' EXIT INT TERM
    stty -echo
    IFS= read -r FRP_TOKEN
    stty echo
    trap - EXIT INT TERM
    printf '\n'
  fi
  if [ -z "$FRP_TOKEN" ]; then
    echo "FRP token 不能为空"
    exit 2
  fi
fi
case "$MACHINE_ARCH" in
  x86_64|amd64) FRP_ARCH=amd64 ;;
  arm64|aarch64) FRP_ARCH=arm64 ;;
  armv7l|armv7*) FRP_ARCH=arm_hf ;;
  armv6l|armv6*) FRP_ARCH=arm ;;
  riscv64) FRP_ARCH=riscv64 ;;
  loongarch64) FRP_ARCH=loong64 ;;
  *) echo "不支持的 CPU 架构: $MACHINE_ARCH"; exit 3 ;;
esac

ARCHIVE="frp_${FRP_VERSION}_${FRP_OS}_${FRP_ARCH}.tar.gz"
case "$ARCHIVE" in
  frp_0.69.0_darwin_amd64.tar.gz) EXPECTED_SHA=3bb1df7aa716a80ddd0b0f108b4e6487bc1e9dae60b22bb67fff6c890bfcc182 ;;
  frp_0.69.0_darwin_arm64.tar.gz) EXPECTED_SHA=07663f5fa71330f074b25e32cc8bc4ae5ed40d9c2ee1690cbd981774475997a2 ;;
  frp_0.69.0_linux_amd64.tar.gz) EXPECTED_SHA=6b90d1cd28fc661f170c0de90dde03d2c63e4fd7ce0ae2da2ca1c28014b8146e ;;
  frp_0.69.0_linux_arm.tar.gz) EXPECTED_SHA=8ee99ad9b09eafe5f77fea7cbd9db15deb056dc2857955477972ccb31a74e708 ;;
  frp_0.69.0_linux_arm64.tar.gz) EXPECTED_SHA=24a4fc82b4c041835103419685ea124c4d6a7dbf83d0425481c5831b4ce4b3a4 ;;
  frp_0.69.0_linux_arm_hf.tar.gz) EXPECTED_SHA=a42b004d1d56255e1c63b74223449165ac93fdca8bafdb60e5317282c05e71c6 ;;
  frp_0.69.0_linux_loong64.tar.gz) EXPECTED_SHA=c136cd4170b44e905ba2c88e0a5900be9fe5baf64f55befb516b18bce02b63a6 ;;
  frp_0.69.0_linux_riscv64.tar.gz) EXPECTED_SHA=27e7b0eea947c2c1b17b2eb62b093e8f4d96185edf245da4699655c047d4a6d0 ;;
  *) echo "版本 $FRP_VERSION 没有内置校验值，请更新安装脚本"; exit 3 ;;
esac

TEMP_DIR=$(mktemp -d)
cleanup() { rm -rf "$TEMP_DIR"; }
trap cleanup EXIT INT TERM
DOWNLOAD_URL="https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/${ARCHIVE}"
echo "下载 $DOWNLOAD_URL"
if command -v curl >/dev/null 2>&1; then
  curl -fL --retry 3 --connect-timeout 15 -o "$TEMP_DIR/$ARCHIVE" "$DOWNLOAD_URL"
elif command -v wget >/dev/null 2>&1; then
  wget -O "$TEMP_DIR/$ARCHIVE" "$DOWNLOAD_URL"
else
  echo "需要 curl 或 wget"
  exit 4
fi

if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL_SHA=$(sha256sum "$TEMP_DIR/$ARCHIVE" | awk '{print $1}')
else
  ACTUAL_SHA=$(shasum -a 256 "$TEMP_DIR/$ARCHIVE" | awk '{print $1}')
fi
if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
  echo "SHA-256 校验失败"
  exit 4
fi

tar -xzf "$TEMP_DIR/$ARCHIVE" -C "$TEMP_DIR"
DOWNLOADED_FRPC="$TEMP_DIR/frp_${FRP_VERSION}_${FRP_OS}_${FRP_ARCH}/frpc"
if [ -n "$MERGE_CONFIG" ]; then
  FRPC_BIN="$DOWNLOADED_FRPC"
  MERGED_CONFIG="$TEMP_DIR/frpc-merged.toml"
  if grep -Eq '^[[:space:]]*clientID[[:space:]]*=' "$MERGE_CONFIG"; then
    cp "$MERGE_CONFIG" "$MERGED_CONFIG"
  else
    {
      printf 'clientID = "%s"\n' "$DEVICE_ID"
      cat "$MERGE_CONFIG"
    } > "$MERGED_CONFIG"
  fi

  if grep -Eq "^[[:space:]]*name[[:space:]]*=[[:space:]]*\"$DEVICE_ID\\.ssh\"" "$MERGED_CONFIG"; then
    echo "SSH proxy $DEVICE_ID.ssh 已存在，不重复追加"
  else
    if grep -Eq "^[[:space:]]*remotePort[[:space:]]*=[[:space:]]*$REMOTE_PORT([[:space:]]*)$" "$MERGED_CONFIG"; then
      echo "远端端口 $REMOTE_PORT 已被现有 proxy 使用"
      exit 6
    fi
    cat >> "$MERGED_CONFIG" <<EOF

[[proxies]]
name = "$DEVICE_ID.ssh"
type = "tcp"
localIP = "127.0.0.1"
localPort = 22
remotePort = $REMOTE_PORT

[proxies.annotations]
device_id = "$DEVICE_ID"
ssh_user = "$SSH_USER_NAME"
service = "ssh"
EOF
  fi
  "$FRPC_BIN" verify -c "$MERGED_CONFIG"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo
    echo "merge dry-run 通过：原配置保持不变，合并后的配置校验成功。"
    echo "将保留现有 proxy，并新增: $DEVICE_ID.ssh"
    echo "预定入口: $FRP_SERVER:$REMOTE_PORT"
    exit 0
  fi
  BACKUP_CONFIG="$MERGE_CONFIG.backup.$(date +%Y%m%d%H%M%S)"
  cp -p "$MERGE_CONFIG" "$BACKUP_CONFIG"
  EXISTING_VERSION=$("$EXISTING_BIN" --version 2>/dev/null || echo unknown)
  if [ "$EXISTING_VERSION" != "$FRP_VERSION" ]; then
    BACKUP_BIN="$EXISTING_BIN.backup.$EXISTING_VERSION.$(date +%Y%m%d%H%M%S)"
    cp -p "$EXISTING_BIN" "$BACKUP_BIN"
    UPGRADE_BIN="$EXISTING_BIN.upgrade.$$"
    install -m 0755 -o "$EXISTING_OWNER" -g "$EXISTING_GROUP" "$DOWNLOADED_FRPC" "$UPGRADE_BIN"
    mv "$UPGRADE_BIN" "$EXISTING_BIN"
    echo "已升级 frpc: $EXISTING_VERSION -> $FRP_VERSION"
    echo "旧二进制备份: $BACKUP_BIN"
  fi
  cat "$MERGED_CONFIG" > "$MERGE_CONFIG"
  echo "已备份原配置: $BACKUP_CONFIG"
elif [ "$DRY_RUN" -eq 1 ]; then
  CONFIG_DIR="$TEMP_DIR/config"
  FRPC_BIN="$TEMP_DIR/frpc"
  install -m 0755 "$DOWNLOADED_FRPC" "$FRPC_BIN"
else
  FRPC_BIN=/usr/local/bin/frpc
  install -m 0755 "$DOWNLOADED_FRPC" "$FRPC_BIN"
fi

if [ -z "$MERGE_CONFIG" ]; then
  install -d -m 0750 "$CONFIG_DIR"
  printf '%s\n' "$FRP_TOKEN" > "$CONFIG_DIR/token"
  chmod 0600 "$CONFIG_DIR/token"
  cat > "$CONFIG_DIR/frpc.toml" <<EOF
clientID = "$DEVICE_ID"
user = "$DEVICE_ID"
serverAddr = "$FRP_SERVER"
serverPort = $FRP_SERVER_PORT
loginFailExit = false

auth.method = "token"
auth.tokenSource.type = "file"
auth.tokenSource.file.path = "$CONFIG_DIR/token"

transport.tls.enable = true

[[proxies]]
name = "ssh"
type = "tcp"
localIP = "127.0.0.1"
localPort = 22
remotePort = $REMOTE_PORT

[proxies.annotations]
device_id = "$DEVICE_ID"
ssh_user = "$SSH_USER_NAME"
service = "ssh"
EOF
  chmod 0600 "$CONFIG_DIR/frpc.toml"
  "$FRPC_BIN" verify -c "$CONFIG_DIR/frpc.toml"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo
  echo "dry-run 通过：下载、SHA-256 和 frpc 配置校验全部成功。"
  echo "检测平台: $FRP_OS/$FRP_ARCH"
  echo "代理名称: $DEVICE_ID.ssh"
  echo "预定入口: $FRP_SERVER:$REMOTE_PORT"
  exit 0
fi

install_authorized_key() {
  if [ "$OS_NAME" = Linux ]; then
    USER_HOME=$(getent passwd "$SSH_USER_NAME" | awk -F: '{print $6}')
  else
    USER_HOME=$(dscl . -read "/Users/$SSH_USER_NAME" NFSHomeDirectory 2>/dev/null | awk '{print $2}')
  fi
  if [ -z "${USER_HOME:-}" ] || [ ! -d "$USER_HOME" ]; then
    echo "找不到 SSH 用户 $SSH_USER_NAME 的主目录"
    exit 5
  fi
  install -d -m 0700 "$USER_HOME/.ssh"
  touch "$USER_HOME/.ssh/authorized_keys"
  chmod 0600 "$USER_HOME/.ssh/authorized_keys"
  if ! grep -Fq "$AGENTSERVER_PUBLIC_KEY" "$USER_HOME/.ssh/authorized_keys"; then
    printf '%s\n' "$AGENTSERVER_PUBLIC_KEY" >> "$USER_HOME/.ssh/authorized_keys"
  fi
  chown -R "$SSH_USER_NAME" "$USER_HOME/.ssh"
}
install_authorized_key

enable_linux_ssh() {
  SSH_SERVICE_ID=""
  for candidate in ssh.service sshd.service; do
    if [ "$(systemctl show "$candidate" -p LoadState --value 2>/dev/null || true)" = loaded ]; then
      SSH_SERVICE_ID=$(systemctl show "$candidate" -p Id --value)
      break
    fi
  done
  if [ -z "$SSH_SERVICE_ID" ]; then
    echo "未找到 OpenSSH Server，请先安装 openssh-server"
    return 1
  fi
  echo "启用 OpenSSH 服务: $SSH_SERVICE_ID"
  systemctl enable --now "$SSH_SERVICE_ID"
}

require_stable_systemd_service() {
  service_name=$1
  for check_step in 1 2 3 4 5; do
    sleep 1
    if systemctl is-active --quiet "$service_name"; then
      # It must remain active for a second check, not merely pass through activating.
      sleep 1
      if systemctl is-active --quiet "$service_name"; then return 0; fi
    fi
  done
  echo "$service_name 未能稳定运行，最近日志："
  journalctl -u "$service_name" -n 30 --no-pager || true
  return 1
}

if [ -n "$MERGE_CONFIG" ]; then
  if [ "$OS_NAME" = Linux ]; then
    if ! enable_linux_ssh; then
      cat "$BACKUP_CONFIG" > "$MERGE_CONFIG"
      exit 5
    fi
    cat > /etc/systemd/system/frpc-agentserver.service <<EOF
[Unit]
Description=AgentServer FRP service with SSH tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$EXISTING_OWNER
WorkingDirectory=$EXISTING_CWD
ExecStart=$EXISTING_BIN -c $MERGE_CONFIG
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
      kill "$EXISTING_PID"
    fi
    systemctl daemon-reload
    systemctl enable --now frpc-agentserver.service
    systemctl restart frpc-agentserver.service
    if ! require_stable_systemd_service frpc-agentserver.service; then
      exit 7
    fi
    systemctl --no-pager --full status frpc-agentserver.service | sed -n '1,16p'
  else
    if ! /usr/sbin/systemsetup -setremotelogin on >/dev/null; then
      echo "无法启用 Remote Login，已恢复原 frpc 配置。请给终端 Full Disk Access 后重试。"
      cat "$BACKUP_CONFIG" > "$MERGE_CONFIG"
      exit 5
    fi
    case "$EXISTING_BIN$EXISTING_CWD$MERGE_CONFIG" in
      *'&'*|*'<'*|*'>'*) echo "现有路径包含无法写入 launchd plist 的字符"; exit 6 ;;
    esac
    MERGE_LOG="$USER_HOME/frpc-agentserver.log"
    MERGE_PLIST=/Library/LaunchDaemons/com.agentserver.frpc.merged.plist
    cat > "$MERGE_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.agentserver.frpc.merged</string>
  <key>UserName</key><string>$EXISTING_OWNER</string>
  <key>WorkingDirectory</key><string>$EXISTING_CWD</string>
  <key>ProgramArguments</key><array><string>$EXISTING_BIN</string><string>-c</string><string>$MERGE_CONFIG</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$MERGE_LOG</string>
  <key>StandardErrorPath</key><string>$MERGE_LOG</string>
</dict></plist>
EOF
    chmod 0644 "$MERGE_PLIST"
    plutil -lint "$MERGE_PLIST" >/dev/null
    launchctl bootout system/com.agentserver.frpc.merged >/dev/null 2>&1 || true
    kill "$EXISTING_PID"
    for wait_step in 1 2 3 4 5; do
      if ! kill -0 "$EXISTING_PID" 2>/dev/null; then break; fi
      sleep 1
    done
    if kill -0 "$EXISTING_PID" 2>/dev/null; then
      kill -KILL "$EXISTING_PID"
    fi
    if ! launchctl bootstrap system "$MERGE_PLIST"; then
      echo "launchd 启动失败，恢复原配置和进程"
      cat "$BACKUP_CONFIG" > "$MERGE_CONFIG"
      /usr/bin/sudo -u "$EXISTING_OWNER" sh -c 'cd "$1"; nohup "$2" -c "$3" >> "$4" 2>&1 &' sh "$EXISTING_CWD" "$EXISTING_BIN" "$MERGE_CONFIG" "$MERGE_LOG"
      exit 7
    fi
    launchctl enable system/com.agentserver.frpc.merged
    sleep 2
    launchctl print system/com.agentserver.frpc.merged | sed -n '1,22p'
  fi
  echo
  echo "合并安装完成"
  echo "保留原配置和 proxy，并新增: $DEVICE_ID.ssh"
  echo "SSH 用户: $SSH_USER_NAME"
  echo "服务器入口: $FRP_SERVER:$REMOTE_PORT"
  echo "返回 AgentServer 页面，等待约 15 秒后点击“同步 FRP”。"
  exit 0
fi

if [ "$OS_NAME" = Linux ]; then
  if ! enable_linux_ssh; then
    exit 5
  fi
  cat > /etc/systemd/system/frpc-agentserver.service <<EOF
[Unit]
Description=AgentServer FRP SSH tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/frpc -c $CONFIG_DIR/frpc.toml
Restart=always
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now frpc-agentserver.service
  systemctl restart frpc-agentserver.service
  if ! require_stable_systemd_service frpc-agentserver.service; then
    exit 7
  fi
  systemctl --no-pager --full status frpc-agentserver.service | sed -n '1,16p'
else
  /usr/sbin/systemsetup -setremotelogin on >/dev/null
  cat > /Library/LaunchDaemons/com.agentserver.frpc.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.agentserver.frpc</string>
  <key>ProgramArguments</key><array><string>/usr/local/bin/frpc</string><string>-c</string><string>$CONFIG_DIR/frpc.toml</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/var/log/frpc-agentserver.log</string>
  <key>StandardErrorPath</key><string>/var/log/frpc-agentserver.log</string>
</dict></plist>
EOF
  chmod 0644 /Library/LaunchDaemons/com.agentserver.frpc.plist
  launchctl bootout system/com.agentserver.frpc >/dev/null 2>&1 || true
  launchctl bootstrap system /Library/LaunchDaemons/com.agentserver.frpc.plist
  launchctl enable system/com.agentserver.frpc
fi

echo
echo "安装完成"
echo "设备 ID: $DEVICE_ID"
echo "代理名称: $DEVICE_ID.ssh"
echo "SSH 用户: $SSH_USER_NAME"
echo "服务器入口: $FRP_SERVER:$REMOTE_PORT"
echo "返回 AgentServer 页面，等待约 15 秒后点击“同步 FRP”。"
