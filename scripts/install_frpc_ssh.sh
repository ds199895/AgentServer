#!/usr/bin/env sh
set -eu

FRP_VERSION="${FRP_VERSION:-0.69.0}"
FRP_SERVER="${FRP_SERVER:-101.43.103.46}"
FRP_SERVER_PORT="${FRP_SERVER_PORT:-7000}"
DEVICE_ID="${DEVICE_ID:-}"
REMOTE_PORT="${FRP_SSH_REMOTE_PORT:-}"
SSH_USER_NAME="${SSH_USER_NAME:-${SUDO_USER:-root}}"
FRP_TOKEN_FILE="${FRP_TOKEN_FILE:-}"
DRY_RUN=0
ROTATE_TOKEN=0
MERGE_CONFIG=""
EXISTING_PID=""
EXISTING_OWNER=""
EXISTING_GROUP=""
EXISTING_CWD=""
EXISTING_BIN=""
EXISTING_UNIT_FILE=""
EXISTING_LAUNCHD_LABEL=""
MERGE_UNIT_LABEL="com.agentserver.frpc.merged"
MERGE_SERVICE_MANAGED=0
MERGE_SERVICE_ACTIVE=0
MERGE_SERVICE_ENABLED=0
MERGE_UNIT_PATH=/etc/systemd/system/frpc-agentserver.service
MERGE_TXN_DIR=""
MERGE_TXN_CONFIG=""
MERGE_TXN_BIN=""
MERGE_TXN_UNIT=""
MERGE_UNIT_EXISTED=0
MERGE_BIN_SNAPSHOTTED=0
MERGE_CONFIG_MUTATED=0
MERGE_BIN_MUTATED=0
MERGE_UNIT_MUTATED=0
MERGE_PROCESS_STOPPED=0
MERGE_ROLLBACK_NEEDED=0
MERGE_ROLLBACK_RUNNING=0
TOKEN_PATH=""
TOKEN_SNAPSHOT=""
TOKEN_PREEXISTING=0
TOKEN_MUTATED=0
TOKEN_ROLLBACK_NEEDED=0
CONFIG_PATH=""
CONFIG_SNAPSHOT=""
CONFIG_PREEXISTING=0
CONFIG_MUTATED=0
CONFIG_ROLLBACK_NEEDED=0
FRPC_BINARY_PATH="/usr/local/bin/frpc"
FRPC_BINARY_SNAPSHOT=""
FRPC_BINARY_PREEXISTING=0
FRPC_BINARY_ROLLBACK_NEEDED=0
FRP_UNIT_PATH=""
FRP_UNIT_SNAPSHOT=""
FRP_UNIT_PREEXISTING=0
FRP_UNIT_ROLLBACK_NEEDED=0
FRP_SERVICE_PREEXISTING=0
FRP_SERVICE_ACTIVE=0
FRP_SERVICE_ENABLED=0
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
  --token-file FILE    从权限恰好为 0600 的私有文件读取 FRP token
  --rotate-token       显式替换匹配的 AgentServer 受管配置中的 token
  --dry-run            下载、校验并验证配置，但不修改系统
  --merge-existing FILE
                       备份并合并到正在运行的现有 frpc 配置
  --help               显示帮助

FRP token 会在终端中隐藏输入，也可通过私有文件或 FRP_TOKEN 环境变量传入。
EOF
}

die() {
  echo "agentserver-frpc: $*" >&2
  exit 7
}

is_safe_account_name() {
  # Use shell pattern matching so an embedded newline cannot make grep accept
  # only the first line of an otherwise unsafe value.
  case "$1" in
    [A-Za-z0-9]*) ;;
    *) return 1 ;;
  esac
  case "$1" in
    *[!A-Za-z0-9_.-]*) return 1 ;;
  esac
  [ "$(printf '%s' "$1" | wc -c | tr -d '[:space:]')" -le 64 ]
}

is_safe_device_id() {
  is_safe_account_name "$1" || return 1
  [ "$(printf '%s' "$1" | wc -c | tr -d '[:space:]')" -ge 2 ]
}

is_safe_server_name() {
  case "$1" in
    ''|*[!A-Za-z0-9._:-]*) return 1 ;;
  esac
  [ "$(printf '%s' "$1" | wc -c | tr -d '[:space:]')" -le 255 ]
}

require_safe_systemd_path() {
  path_label=$1
  path_value=$2
  case "$path_value" in
    /*) ;;
    *) echo "$path_label 必须是绝对路径，无法安全写入 systemd unit" >&2; return 1 ;;
  esac
  case "$path_value" in
    ''|*[!A-Za-z0-9_./:@+-]*)
      echo "$path_label 包含空白或 systemd 特殊字符，无法自动合并" >&2
      return 1
      ;;
  esac
}

require_regular_merge_config() {
  if [ -L "$MERGE_CONFIG" ] || [ ! -f "$MERGE_CONFIG" ]; then
    echo "现有配置必须是普通文件且不能是符号链接" >&2
    return 1
  fi
}

require_safe_launchd_value() {
  launchd_label=$1
  launchd_value=$2
  launchd_lines=$(printf '%s\n' "$launchd_value" | wc -l | tr -d '[:space:]')
  case "$launchd_value" in
    ''|*'&'*|*'<'*|*'>'*|*"$(printf '\r')"*)
      echo "$launchd_label 包含无法安全写入 launchd plist 的字符" >&2
      return 1
      ;;
  esac
  if [ "$launchd_lines" -ne 1 ]; then
    echo "$launchd_label 包含换行，无法安全写入 launchd plist" >&2
    return 1
  fi
}

write_private_file_atomic() {
  atomic_target=$1
  atomic_value=$2
  atomic_directory=$(dirname "$atomic_target")
  atomic_temporary=$(umask 077; mktemp "$atomic_directory/.agentserver-private.XXXXXX") || return 1
  if ! (umask 077; printf '%s\n' "$atomic_value" > "$atomic_temporary") \
    || ! chmod 0600 "$atomic_temporary" \
    || ! mv -f "$atomic_temporary" "$atomic_target"; then
    rm -f "$atomic_temporary"
    return 1
  fi
}

write_private_file_atomic_from_stdin() {
  atomic_target=$1
  atomic_directory=$(dirname "$atomic_target")
  atomic_temporary=$(umask 077; mktemp "$atomic_directory/.agentserver-private.XXXXXX") || return 1
  if ! (umask 077; cat > "$atomic_temporary") \
    || ! chmod 0600 "$atomic_temporary" \
    || ! mv -f "$atomic_temporary" "$atomic_target"; then
    rm -f "$atomic_temporary"
    return 1
  fi
}

write_unit_atomic_from_stdin() {
  atomic_target=$1
  atomic_mode=$2
  atomic_directory=$(dirname "$atomic_target")
  atomic_temporary=$(umask 077; mktemp "$atomic_directory/.agentserver-unit.XXXXXX") || return 1
  if ! (umask 077; cat > "$atomic_temporary") \
    || ! chmod "$atomic_mode" "$atomic_temporary" \
    || ! mv -f "$atomic_temporary" "$atomic_target"; then
    rm -f "$atomic_temporary"
    return 1
  fi
}

rollback_private_files() {
  set +e
  if [ "$TOKEN_ROLLBACK_NEEDED" -eq 1 ]; then
    if [ "$TOKEN_PREEXISTING" -eq 1 ] && [ -f "$TOKEN_SNAPSHOT" ]; then
      restore_snapshot "$TOKEN_PATH" "$TOKEN_SNAPSHOT" || echo "FRP token 回滚失败: $TOKEN_PATH" >&2
    else
      rm -f -- "$TOKEN_PATH" || echo "FRP token 临时文件清理失败: $TOKEN_PATH" >&2
    fi
    TOKEN_ROLLBACK_NEEDED=0
  fi
  if [ "$CONFIG_ROLLBACK_NEEDED" -eq 1 ]; then
    if [ "$CONFIG_PREEXISTING" -eq 1 ] && [ -f "$CONFIG_SNAPSHOT" ]; then
      restore_snapshot "$CONFIG_PATH" "$CONFIG_SNAPSHOT" || echo "FRP 配置回滚失败: $CONFIG_PATH" >&2
    else
      rm -f -- "$CONFIG_PATH" || echo "FRP 配置临时文件清理失败: $CONFIG_PATH" >&2
    fi
    CONFIG_ROLLBACK_NEEDED=0
  fi
  if [ "$FRPC_BINARY_ROLLBACK_NEEDED" -eq 1 ]; then
    if [ "$FRPC_BINARY_PREEXISTING" -eq 1 ] && [ -f "$FRPC_BINARY_SNAPSHOT" ]; then
      restore_snapshot "$FRPC_BINARY_PATH" "$FRPC_BINARY_SNAPSHOT" \
        || echo "frpc 二进制回滚失败: $FRPC_BINARY_PATH" >&2
    else
      rm -f -- "$FRPC_BINARY_PATH" || echo "frpc 二进制临时文件清理失败: $FRPC_BINARY_PATH" >&2
    fi
    FRPC_BINARY_ROLLBACK_NEEDED=0
  fi
}

rollback_normal_service() {
  [ "$FRP_UNIT_ROLLBACK_NEEDED" -eq 1 ] || return 0
  set +e
  if [ "$OS_NAME" = Linux ]; then
    systemctl stop frpc-agentserver.service >/dev/null 2>&1 || true
  elif [ "$OS_NAME" = Darwin ]; then
    bootout_launchd_job com.agentserver.frpc || true
  fi
  if [ "$FRP_UNIT_PREEXISTING" -eq 1 ] && [ -f "$FRP_UNIT_SNAPSHOT" ]; then
    restore_snapshot "$FRP_UNIT_PATH" "$FRP_UNIT_SNAPSHOT" \
      || echo "FRP service unit rollback failed: $FRP_UNIT_PATH" >&2
  else
    rm -f -- "$FRP_UNIT_PATH" || echo "FRP service unit cleanup failed: $FRP_UNIT_PATH" >&2
  fi
  if [ "$OS_NAME" = Linux ]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    if [ "$FRP_SERVICE_PREEXISTING" -eq 1 ]; then
      if [ "$FRP_SERVICE_ACTIVE" -eq 1 ]; then
        systemctl restart frpc-agentserver.service >/dev/null 2>&1 || true
      fi
      if [ "$FRP_SERVICE_ENABLED" -eq 1 ]; then
        systemctl enable frpc-agentserver.service >/dev/null 2>&1 || true
      else
        systemctl disable frpc-agentserver.service >/dev/null 2>&1 || true
      fi
    else
      systemctl disable frpc-agentserver.service >/dev/null 2>&1 || true
    fi
  elif [ "$OS_NAME" = Darwin ] && [ "$FRP_UNIT_PREEXISTING" -eq 1 ]; then
    launchctl bootstrap system "$FRP_UNIT_PATH" >/dev/null 2>&1 || true
    if [ "$FRP_SERVICE_ENABLED" -eq 1 ]; then
      launchctl enable system/com.agentserver.frpc >/dev/null 2>&1 || true
    else
      launchctl disable system/com.agentserver.frpc >/dev/null 2>&1 || true
    fi
  fi
  FRP_UNIT_ROLLBACK_NEEDED=0
}

require_stable_launchd_job() {
  launchd_job_label=$1
  for launchd_check in 1 2 3 4 5; do
    if launchctl print "system/$launchd_job_label" >/dev/null 2>&1; then
      sleep 1
      if launchctl print "system/$launchd_job_label" >/dev/null 2>&1; then
        return 0
      fi
    fi
    sleep 1
  done
  echo "launchd 服务 system/$launchd_job_label 未能稳定加载" >&2
  return 1
}

bootout_launchd_job() {
  launchd_job_label=$1
  if launchctl print "system/$launchd_job_label" >/dev/null 2>&1; then
    launchctl bootout "system/$launchd_job_label" >/dev/null 2>&1
  fi
}

path_exists_or_link() {
  [ -e "$1" ] || [ -L "$1" ]
}

restore_snapshot() {
  restore_target=$1
  restore_source=$2
  restore_tmp="${restore_target}.restore.$$"
  rm -f "$restore_tmp"
  cp -p "$restore_source" "$restore_tmp" || return 1
  mv -f "$restore_tmp" "$restore_target" || {
    rm -f "$restore_tmp"
    return 1
  }
}

rollback_merge() {
  rollback_status=${1:-$?}
  [ "$MERGE_ROLLBACK_NEEDED" -eq 1 ] || return 0
  [ "$MERGE_ROLLBACK_RUNNING" -eq 0 ] || return 0
  MERGE_ROLLBACK_RUNNING=1
  # The EXIT trap must preserve the original failure even if a best-effort
  # recovery command is unavailable or fails.
  set +e
  merge_has_mutation=0
  if [ "$MERGE_CONFIG_MUTATED" -eq 1 ] || [ "$MERGE_BIN_MUTATED" -eq 1 ] \
    || [ "$MERGE_UNIT_MUTATED" -eq 1 ] || [ "$MERGE_PROCESS_STOPPED" -eq 1 ]; then
    merge_has_mutation=1
  fi
  if [ "$OS_NAME" = Linux ]; then
    if [ "$merge_has_mutation" -eq 1 ] && { [ "$MERGE_SERVICE_MANAGED" -eq 1 ] || [ "$MERGE_UNIT_MUTATED" -eq 1 ]; }; then
      systemctl stop frpc-agentserver.service >/dev/null 2>&1 || true
    fi
  elif [ "$OS_NAME" = Darwin ] && [ "$MERGE_UNIT_MUTATED" -eq 1 ]; then
    bootout_launchd_job "$MERGE_UNIT_LABEL" || true
  fi

  if [ "$MERGE_CONFIG_MUTATED" -eq 1 ] && [ -f "$MERGE_TXN_CONFIG" ]; then
    restore_snapshot "$MERGE_CONFIG" "$MERGE_TXN_CONFIG" || true
  fi
  if [ "$MERGE_BIN_MUTATED" -eq 1 ] && [ "$MERGE_BIN_SNAPSHOTTED" -eq 1 ] \
    && [ -f "$MERGE_TXN_BIN" ]; then
    restore_snapshot "$EXISTING_BIN" "$MERGE_TXN_BIN" || true
  fi
  if [ "$MERGE_UNIT_MUTATED" -eq 1 ]; then
    if [ "$MERGE_UNIT_EXISTED" -eq 1 ] && [ -f "$MERGE_TXN_UNIT" ]; then
      restore_snapshot "$MERGE_UNIT_PATH" "$MERGE_TXN_UNIT" || true
    else
      rm -f "$MERGE_UNIT_PATH" || true
    fi
  fi

  if [ "$OS_NAME" = Linux ]; then
    if [ "$merge_has_mutation" -eq 1 ] && { [ "$MERGE_SERVICE_MANAGED" -eq 1 ] || [ "$MERGE_UNIT_MUTATED" -eq 1 ]; }; then
      systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    if [ "$MERGE_SERVICE_MANAGED" -eq 1 ]; then
      if [ "$MERGE_SERVICE_ACTIVE" -eq 1 ]; then
        systemctl restart frpc-agentserver.service >/dev/null 2>&1 || true
      fi
      if [ "$MERGE_SERVICE_ENABLED" -eq 1 ]; then
        systemctl enable frpc-agentserver.service >/dev/null 2>&1 || true
      else
        systemctl disable frpc-agentserver.service >/dev/null 2>&1 || true
      fi
    else
      if [ "$MERGE_UNIT_MUTATED" -eq 1 ]; then
        systemctl disable frpc-agentserver.service >/dev/null 2>&1 || true
      fi
      if [ "$MERGE_PROCESS_STOPPED" -eq 1 ] && [ -n "$EXISTING_BIN" ] \
      && [ -n "$EXISTING_CWD" ] && [ -n "$EXISTING_OWNER" ]; then
        if command -v runuser >/dev/null 2>&1; then
          runuser -u "$EXISTING_OWNER" -- sh -c \
            'cd "$1" && nohup "$2" -c "$3" >/dev/null 2>&1 &' \
            sh "$EXISTING_CWD" "$EXISTING_BIN" "$MERGE_CONFIG" || true
        fi
      fi
    fi
  elif [ "$OS_NAME" = Darwin ]; then
    restored_launchd=0
    if [ "$MERGE_SERVICE_MANAGED" -eq 1 ] && [ "$MERGE_UNIT_MUTATED" -eq 1 ] \
      && [ "$MERGE_UNIT_EXISTED" -eq 1 ] \
      && [ -f "$MERGE_UNIT_PATH" ] && plutil -lint "$MERGE_UNIT_PATH" >/dev/null 2>&1 \
      && launchctl bootstrap system "$MERGE_UNIT_PATH" >/dev/null 2>&1; then
      if [ "$MERGE_SERVICE_ENABLED" -eq 1 ]; then
        launchctl enable "system/$MERGE_UNIT_LABEL" >/dev/null 2>&1 || true
      else
        launchctl disable "system/$MERGE_UNIT_LABEL" >/dev/null 2>&1 || true
      fi
      restored_launchd=1
    fi
    # An unmanaged process has no plist to restore.  Keep the old direct
    # restart fallback, but never start a second process after restoring a
    # launchd job.
    if [ "$MERGE_PROCESS_STOPPED" -eq 1 ] && [ "$restored_launchd" -eq 0 ] \
      && [ "$MERGE_SERVICE_MANAGED" -eq 0 ] && [ -n "$EXISTING_BIN" ] \
      && [ -n "$EXISTING_CWD" ] && [ -n "$EXISTING_OWNER" ]; then
      /usr/bin/sudo -u "$EXISTING_OWNER" -- sh -c \
        'cd "$1" && nohup "$2" -c "$3" >/dev/null 2>&1 &' \
        sh "$EXISTING_CWD" "$EXISTING_BIN" "$MERGE_CONFIG" || true
    fi
  fi
  MERGE_ROLLBACK_NEEDED=0
  if [ "$#" -gt 0 ]; then
    echo "合并安装失败，已尽力恢复原 frpc 配置、unit、可执行文件和服务；请检查 systemd 日志" >&2
    exit "$rollback_status"
  fi
  return 0
}

prepare_merge_transaction() {
  MERGE_TXN_DIR="$TEMP_DIR/merge-transaction"
  if ! mkdir -m 0700 "$MERGE_TXN_DIR"; then
    return 1
  fi
  MERGE_TXN_CONFIG="$MERGE_TXN_DIR/config"
  if ! cp -p "$MERGE_CONFIG" "$MERGE_TXN_CONFIG"; then
    return 1
  fi
  if [ -f "$EXISTING_BIN" ]; then
    MERGE_TXN_BIN="$MERGE_TXN_DIR/frpc"
    if ! cp -p "$EXISTING_BIN" "$MERGE_TXN_BIN"; then
      return 1
    fi
    MERGE_BIN_SNAPSHOTTED=1
  fi
  if path_exists_or_link "$MERGE_UNIT_PATH"; then
    if [ -L "$MERGE_UNIT_PATH" ] || [ ! -f "$MERGE_UNIT_PATH" ]; then
      echo "现有 frpc systemd unit 必须是普通文件且不能是符号链接" >&2
      return 1
    fi
    MERGE_TXN_UNIT="$MERGE_TXN_DIR/unit"
    if ! cp -p "$MERGE_UNIT_PATH" "$MERGE_TXN_UNIT"; then
      return 1
    fi
    MERGE_UNIT_EXISTED=1
  fi
  MERGE_ROLLBACK_NEEDED=1
  MERGE_ROLLBACK_RUNNING=0
}

replace_merge_config() {
  merge_config_tmp="${MERGE_CONFIG}.tmp.$$"
  if ! rm -f "$merge_config_tmp"; then
    return 1
  fi
  if ! cp -p "$MERGE_CONFIG" "$merge_config_tmp"; then
    rm -f "$merge_config_tmp"
    return 1
  fi
  if ! cat "$MERGED_CONFIG" > "$merge_config_tmp"; then
    rm -f "$merge_config_tmp"
    return 1
  fi
  if ! mv -f "$merge_config_tmp" "$MERGE_CONFIG"; then
    rm -f "$merge_config_tmp"
    return 1
  fi
  MERGE_CONFIG_MUTATED=1
}

replace_merge_systemd_unit() {
  merge_unit_tmp="${MERGE_UNIT_PATH}.tmp.$$"
  if ! rm -f "$merge_unit_tmp"; then
    return 1
  fi
  if ! cat > "$merge_unit_tmp" <<EOF
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
  then
    rm -f "$merge_unit_tmp"
    return 1
  fi
  if ! chmod 0644 "$merge_unit_tmp"; then
    rm -f "$merge_unit_tmp"
    return 1
  fi
  if ! mv -f "$merge_unit_tmp" "$MERGE_UNIT_PATH"; then
    rm -f "$merge_unit_tmp"
    return 1
  fi
  MERGE_UNIT_MUTATED=1
}

replace_merge_launchd_plist() {
  merge_plist_tmp="${MERGE_UNIT_PATH}.tmp.$$"
  if ! rm -f "$merge_plist_tmp"; then
    return 1
  fi
  if ! cat > "$merge_plist_tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$MERGE_UNIT_LABEL</string>
  <key>UserName</key><string>$EXISTING_OWNER</string>
  <key>WorkingDirectory</key><string>$EXISTING_CWD</string>
  <key>ProgramArguments</key><array><string>$EXISTING_BIN</string><string>-c</string><string>$MERGE_CONFIG</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$MERGE_LOG</string>
  <key>StandardErrorPath</key><string>$MERGE_LOG</string>
</dict></plist>
EOF
  then
    rm -f "$merge_plist_tmp"
    return 1
  fi
  if ! chmod 0644 "$merge_plist_tmp"; then
    rm -f "$merge_plist_tmp"
    return 1
  fi
  if ! mv -f "$merge_plist_tmp" "$MERGE_UNIT_PATH"; then
    rm -f "$merge_plist_tmp"
    return 1
  fi
  MERGE_UNIT_MUTATED=1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --device-id) DEVICE_ID="${2:-}"; shift 2 ;;
    --remote-port) REMOTE_PORT="${2:-}"; shift 2 ;;
    --ssh-user) SSH_USER_NAME="${2:-}"; shift 2 ;;
    --server) FRP_SERVER="${2:-}"; shift 2 ;;
    --server-port) FRP_SERVER_PORT="${2:-}"; shift 2 ;;
    --version) FRP_VERSION="${2:-}"; shift 2 ;;
    --token-file|--frp-token-file) FRP_TOKEN_FILE="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --rotate-token) ROTATE_TOKEN=1; shift ;;
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
  if ! require_regular_merge_config; then
    exit 2
  fi
  MERGE_CONFIG="$(cd "$(dirname "$MERGE_CONFIG")" && pwd -P)/$(basename "$MERGE_CONFIG")"
fi
[ -z "$MERGE_CONFIG" ] || [ "$ROTATE_TOKEN" -eq 0 ] || {
  echo "--rotate-token 只能用于 AgentServer 受管配置，不能用于 --merge-existing" >&2
  exit 2
}

if ! is_safe_device_id "$DEVICE_ID"; then
  echo "--device-id 格式无效"
  exit 2
fi
if ! is_safe_account_name "$SSH_USER_NAME"; then
  echo "--ssh-user 格式无效"
  exit 2
fi
if ! is_safe_server_name "$FRP_SERVER"; then
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
if [ "$FRP_SERVER_PORT" -lt 1 ] || [ "$FRP_SERVER_PORT" -gt 65535 ]; then
  echo "--server-port 必须位于 1-65535"
  exit 2
fi

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
  EXISTING_UNIT_FILE=$UNIT_FILE
  MERGE_UNIT_PATH=$UNIT_FILE
  MERGE_SERVICE_MANAGED=1
  if systemctl is-active --quiet frpc-agentserver.service 2>/dev/null; then
    MERGE_SERVICE_ACTIVE=1
  fi
  if systemctl is-enabled --quiet frpc-agentserver.service 2>/dev/null; then
    MERGE_SERVICE_ENABLED=1
  fi
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
  if ! is_safe_account_name "$EXISTING_OWNER"; then
    echo "现有 frpc 用户名无法安全写入 systemd unit" >&2
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
  if [ "$OS_NAME" = Linux ] && systemctl is-active --quiet frpc-agentserver.service 2>/dev/null; then
    MANAGED=1
    MERGE_SERVICE_MANAGED=1
    MERGE_SERVICE_ACTIVE=1
  fi
  if [ "$OS_NAME" = Darwin ]; then
    if launchctl print system/com.agentserver.frpc >/dev/null 2>&1; then
      MANAGED=1
      MERGE_SERVICE_MANAGED=1
      MERGE_SERVICE_ACTIVE=1
      EXISTING_LAUNCHD_LABEL=com.agentserver.frpc
    elif launchctl print system/com.agentserver.frpc.merged >/dev/null 2>&1; then
      MANAGED=1
      MERGE_SERVICE_MANAGED=1
      MERGE_SERVICE_ACTIVE=1
      EXISTING_LAUNCHD_LABEL=com.agentserver.frpc.merged
    fi
    if [ "$MANAGED" -eq 1 ]; then
      MERGE_SERVICE_ENABLED=1
      disabled_pattern=$(printf '"%s"[[:space:]]*=>[[:space:]]*true' "$EXISTING_LAUNCHD_LABEL")
      if launchctl print-disabled system 2>/dev/null \
        | grep -Eq "$disabled_pattern"; then
        MERGE_SERVICE_ENABLED=0
      fi
    fi
  fi
  if [ -n "$MERGE_CONFIG" ]; then
    if [ "$OS_NAME" = Darwin ] && [ "$MANAGED" -ne 1 ]; then
      echo "无法确认现有 frpc 的 launchd supervisor，拒绝自动合并以避免产生两个 frpc" >&2
      exit 6
    fi
    EXISTING_OWNER=$(ps -p "$EXISTING_PID" -o user= | awk '{print $1}')
    if ! is_safe_account_name "$EXISTING_OWNER"; then
      echo "现有 frpc 用户名无法安全写入服务定义" >&2
      exit 6
    fi
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
  elif [ "$MANAGED" -ne 1 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
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

if [ -n "$MERGE_CONFIG" ] && [ "$OS_NAME" = Linux ]; then
  require_regular_merge_config || exit 6
  require_safe_systemd_path "现有 frpc systemd unit" "$MERGE_UNIT_PATH" || exit 6
  require_safe_systemd_path "现有 frpc 工作目录" "$EXISTING_CWD" || exit 6
  require_safe_systemd_path "现有 frpc 可执行文件" "$EXISTING_BIN" || exit 6
  require_safe_systemd_path "现有 frpc 配置" "$MERGE_CONFIG" || exit 6
  [ -d "$EXISTING_CWD" ] || { echo "现有 frpc 工作目录不存在" >&2; exit 6; }
  [ -f "$EXISTING_BIN" ] && [ -x "$EXISTING_BIN" ] || {
    echo "现有 frpc 可执行文件不存在或不可执行" >&2
    exit 6
  }
fi

read_private_token_file() {
  token_path=$1
  if [ ! -f "$token_path" ] || [ -L "$token_path" ]; then
    echo "FRP token 文件必须是普通文件且不能是符号链接" >&2
    return 1
  fi
  private_metadata() {
    stat -c '%d:%i:%a:%u:%s' "$1" 2>/dev/null || stat -f '%d:%i:%Lp:%u:%z' "$1" 2>/dev/null
  }
  private_fd_metadata() {
    stat -Lc '%d:%i:%a:%u:%s' "$1" 2>/dev/null || stat -Lf '%d:%i:%Lp:%u:%z' "$1" 2>/dev/null
  }
  token_metadata=$(private_metadata "$token_path" || true)
  token_mode=$(printf '%s' "$token_metadata" | cut -d: -f3)
  token_owner=$(printf '%s' "$token_metadata" | cut -d: -f4)
  token_size=$(printf '%s' "$token_metadata" | cut -d: -f5)
  if [ "$token_mode" != 600 ]; then
    echo "FRP token 文件权限必须恰好为 0600" >&2
    return 1
  fi
  if [ "$token_owner" != "$(id -u)" ] && [ "$token_owner" != "${SUDO_UID:-}" ]; then
    echo "FRP token 文件必须由当前用户或 sudo 调用者所有" >&2
    return 1
  fi
  case "$token_size" in
    ''|*[!0-9]*) echo "无法验证 FRP token 文件大小" >&2; return 1 ;;
  esac
  if [ "$token_size" -lt 1 ] || [ "$token_size" -gt 4096 ]; then
    echo "FRP token 文件为空或超过 4096 字节" >&2
    return 1
  fi
  if ! exec 7< "$token_path"; then
    echo "无法打开 FRP token 文件" >&2
    return 1
  fi
  opened_metadata=$(private_fd_metadata /dev/fd/7 || true)
  if [ ! -f /dev/fd/7 ] || [ "$opened_metadata" != "$token_metadata" ]; then
    exec 7<&-
    echo "FRP token 文件在验证期间发生变化" >&2
    return 1
  fi
  FRP_TOKEN=$(dd bs=4097 count=1 2>/dev/null <&7)
  opened_metadata_after_read=$(private_fd_metadata /dev/fd/7 || true)
  path_metadata_after_read=$(private_metadata "$token_path" || true)
  exec 7<&-
  if [ "$opened_metadata_after_read" != "$token_metadata" ] || [ "$path_metadata_after_read" != "$token_metadata" ]; then
    echo "FRP token 文件在读取期间发生变化" >&2
    return 1
  fi
  token_bytes=$(printf '%s' "$FRP_TOKEN" | wc -c | tr -d '[:space:]')
  if [ "$token_bytes" -lt 1 ] || [ "$token_bytes" -gt 4096 ]; then
    echo "FRP token 文件为空或超过 4096 字节" >&2
    return 1
  fi
  case "$FRP_TOKEN" in
    ''|*[[:space:]]*)
      echo "FRP token 文件包含无效空白" >&2
      return 1
      ;;
  esac
}

managed_config_matches_request() {
  managed_config="$CONFIG_DIR/frpc.toml"
  managed_token="$CONFIG_DIR/token"
  [ -f "$managed_config" ] && [ ! -L "$managed_config" ] \
    && [ -f "$managed_token" ] && [ ! -L "$managed_token" ] \
    && [ "$(stat -c '%a' "$managed_config" 2>/dev/null || stat -f '%Lp' "$managed_config" 2>/dev/null)" = 600 ] \
    && [ "$(stat -c '%a' "$managed_token" 2>/dev/null || stat -f '%Lp' "$managed_token" 2>/dev/null)" = 600 ] \
    && managed_file_owner_is_trusted "$managed_config" \
    && managed_file_owner_is_trusted "$managed_token" \
    && cmp -s "$managed_config" - <<EOF
clientID = "$DEVICE_ID"
user = "$DEVICE_ID"
serverAddr = "$FRP_SERVER"
serverPort = $FRP_SERVER_PORT
loginFailExit = false

auth.method = "token"
auth.tokenSource.type = "file"
auth.tokenSource.file.path = "$managed_token"

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
}

managed_file_owner_is_trusted() {
  managed_owner="$(stat -c '%u' "$1" 2>/dev/null || stat -f '%u' "$1" 2>/dev/null)"
  [ "$managed_owner" = 0 ] || { [ "$DRY_RUN" -eq 1 ] && [ "$managed_owner" = "$(id -u)" ]; }
}

managed_config_dir_is_safe() {
  if [ -L "$CONFIG_DIR" ] || { [ -e "$CONFIG_DIR" ] && [ ! -d "$CONFIG_DIR" ]; }; then
    return 1
  fi
  [ -d "$CONFIG_DIR" ] || return 0
  managed_dir_owner="$(stat -c '%u' "$CONFIG_DIR" 2>/dev/null || stat -f '%u' "$CONFIG_DIR" 2>/dev/null)"
  managed_dir_mode="$(stat -c '%a' "$CONFIG_DIR" 2>/dev/null || stat -f '%Lp' "$CONFIG_DIR" 2>/dev/null)"
  case "$managed_dir_mode" in
    ''|*[!0-7]*) return 1 ;;
  esac
  { [ "$managed_dir_owner" = 0 ] || { [ "$DRY_RUN" -eq 1 ] && [ "$managed_dir_owner" = "$(id -u)" ]; }; } \
    && [ "$((0$managed_dir_mode & 0022))" -eq 0 ]
}

managed_config_present() {
  [ -e "$CONFIG_DIR/frpc.toml" ] || [ -L "$CONFIG_DIR/frpc.toml" ] \
    || [ -e "$CONFIG_DIR/token" ] || [ -L "$CONFIG_DIR/token" ]
}

if [ -z "$MERGE_CONFIG" ]; then
  # A dry-run by an ordinary user may validate a fresh installation before
  # root creates /etc/frp.  Enforce ownership/mode as soon as an existing
  # managed config is present (or before a real write), but let token
  # validation run first when the target directory is only a system default.
  if [ "$DRY_RUN" -eq 0 ] || managed_config_present; then
    if ! managed_config_dir_is_safe; then
      echo "AgentServer FRP 配置目录必须是可信用户所有、不可写的普通目录且不能是符号链接" >&2
      exit 6
    fi
  fi
  if managed_config_present && ! managed_config_matches_request; then
    echo "检测到已有 AgentServer FRP 配置，但参数与本次请求不一致；为避免覆盖现有隧道，请使用 --merge-existing 或先人工迁移配置" >&2
    exit 6
  fi
  if managed_config_present && managed_config_matches_request; then
    if [ "$ROTATE_TOKEN" -eq 1 ]; then
      echo "已明确请求轮换 AgentServer FRP token"
    elif [ -n "$FRP_TOKEN_FILE" ] || [ -n "${FRP_TOKEN:-}" ]; then
      echo "已有受管 FRP 配置；替换 token 必须显式传入 --rotate-token" >&2
      exit 6
    else
      FRP_TOKEN_FILE="$CONFIG_DIR/token"
      echo "检测到参数一致的 AgentServer FRP 配置，将复用现有 token"
    fi
  elif [ "$ROTATE_TOKEN" -eq 1 ]; then
    echo "--rotate-token 要求已有且参数完全匹配的 AgentServer 受管配置" >&2
    exit 6
  fi
  if [ -n "$FRP_TOKEN_FILE" ]; then
    read_private_token_file "$FRP_TOKEN_FILE"
  elif [ -z "${FRP_TOKEN:-}" ]; then
    if [ ! -t 0 ]; then
      echo "非交互运行时必须通过 FRP_TOKEN 提供 token"
      exit 2
    fi
    printf '请输入 FRP token（输入不会显示）: '
    trap 'stty echo 2>/dev/null || true' 0 INT TERM
    stty -echo
    IFS= read -r FRP_TOKEN
    stty echo
    trap on_exit 0 INT TERM
    printf '\n'
  fi
  if [ -z "$FRP_TOKEN" ]; then
    echo "FRP token 不能为空"
    exit 2
  fi
  token_bytes=$(printf '%s' "$FRP_TOKEN" | wc -c | tr -d '[:space:]')
  if [ "$token_bytes" -lt 1 ] || [ "$token_bytes" -gt 4096 ]; then
    echo "FRP token 必须包含 1-4096 字节" >&2
    exit 2
  fi
  case "$FRP_TOKEN" in
    *[[:space:]]*) echo "FRP token 不能包含空白" >&2; exit 2 ;;
  esac
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
on_exit() {
  exit_status=$?
  trap - 0 INT TERM
  rollback_merge || true
  rollback_normal_service || true
  rollback_private_files
  cleanup
  exit "$exit_status"
}
trap on_exit 0 INT TERM
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

  validate_requested_proxy() {
    awk -v expected_name="$DEVICE_ID.ssh" \
        -v expected_port="$REMOTE_PORT" \
        -v expected_device="$DEVICE_ID" \
        -v expected_user="$SSH_USER_NAME" '
      function trim(text) {
        sub(/^[[:space:]]+/, "", text)
        sub(/[[:space:]]+$/, "", text)
        return text
      }
      function value(line, text, idx, char, quoted, escaped) {
        text = line
        sub(/^[^=]*=[[:space:]]*/, "", text)
        text = trim(text)
        quoted = 0
        escaped = 0
        for (idx = 1; idx <= length(text); idx++) {
          char = substr(text, idx, 1)
          if (escaped) escaped = 0
          else if (char == "\\") escaped = 1
          else if (char == "\"") quoted = !quoted
          else if (char == "#" && !quoted) { text = substr(text, 1, idx - 1); break }
        }
        return trim(text)
      }
      function is_target_name(text) {
        return text == "\"" expected_name "\"" || text == "\047" expected_name "\047"
      }
      function finish() {
        if (!found) return
        matches++
        if (name != "\"" expected_name "\"" || name_count != 1 ||
            type_count != 1 || local_ip_count != 1 || local_port_count != 1 ||
            remote_port_count != 1 || device_id_count != 1 || ssh_user_count != 1 ||
            service_count != 1 || type != "\"tcp\"" ||
            local_ip != "\"127.0.0.1\"" || local_port != "22" ||
            remote_port != expected_port || device_id != "\"" expected_device "\"" ||
            ssh_user != "\"" expected_user "\"" || service != "\"ssh\"") invalid=1
      }
      /^[[:space:]]*\[\[proxies\]\][[:space:]]*(#.*)?$/ {
        finish(); in_proxy=1; section="proxy"; found=0
        name_count=type_count=local_ip_count=local_port_count=remote_port_count=0
        device_id_count=ssh_user_count=service_count=0
        name=type=local_ip=local_port=remote_port=device_id=ssh_user=service=""
        next
      }
      in_proxy && /^[[:space:]]*\[proxies\.annotations\][[:space:]]*(#.*)?$/ { section="annotations"; next }
      in_proxy && /^[[:space:]]*\[/ { finish(); in_proxy=0; section=""; next }
      in_proxy && /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=/ {
        key=$0; sub(/^[[:space:]]*/, "", key); sub(/[[:space:]=].*$/, "", key)
        val=value($0)
        if (section == "proxy") {
          if (key == "name") { name_count++; name=val; if (is_target_name(val)) found=1 }
          else if (key == "type") { type_count++; type=val }
          else if (key == "localIP") { local_ip_count++; local_ip=val }
          else if (key == "localPort") { local_port_count++; local_port=val }
          else if (key == "remotePort") { remote_port_count++; remote_port=val }
        } else if (section == "annotations") {
          if (key == "device_id") { device_id_count++; device_id=val }
          else if (key == "ssh_user") { ssh_user_count++; ssh_user=val }
          else if (key == "service") { service_count++; service=val }
        }
      }
      END {
        finish()
        if (matches == 0) exit 1
        if (invalid || matches != 1) exit 2
        exit 0
      }
    ' "$MERGED_CONFIG"
  }
  if validate_requested_proxy; then
    echo "SSH proxy $DEVICE_ID.ssh 已存在且配置完全匹配，不重复追加"
  else
    proxy_status=$?
    if [ "$proxy_status" -eq 2 ]; then
      echo "现有 SSH proxy $DEVICE_ID.ssh 的 type、端口、地址或 annotations 与本次请求不一致" >&2
      exit 6
    fi
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
  if [ "$OS_NAME" = Darwin ]; then
    if [ -n "$EXISTING_LAUNCHD_LABEL" ]; then
      MERGE_UNIT_LABEL="$EXISTING_LAUNCHD_LABEL"
      MERGE_UNIT_PATH="/Library/LaunchDaemons/$EXISTING_LAUNCHD_LABEL.plist"
    else
      MERGE_UNIT_LABEL=com.agentserver.frpc.merged
      MERGE_UNIT_PATH=/Library/LaunchDaemons/com.agentserver.frpc.merged.plist
    fi
    if [ "$MERGE_SERVICE_MANAGED" -eq 1 ] \
      && { [ -L "$MERGE_UNIT_PATH" ] || [ ! -f "$MERGE_UNIT_PATH" ]; }; then
      echo "现有 launchd frpc plist 缺失、不是普通文件或是符号链接" >&2
      exit 6
    fi
  fi
  if ! prepare_merge_transaction; then
    echo "无法创建 merge 事务快照，未修改现有 frpc" >&2
    exit 6
  fi
  EXISTING_VERSION=$("$EXISTING_BIN" --version 2>/dev/null || echo unknown)
  if [ "$EXISTING_VERSION" != "$FRP_VERSION" ]; then
    BACKUP_BIN="$EXISTING_BIN.backup.$EXISTING_VERSION.$(date +%Y%m%d%H%M%S)"
    cp -p "$EXISTING_BIN" "$BACKUP_BIN"
    UPGRADE_BIN="$EXISTING_BIN.upgrade.$$"
    if ! install -m 0755 -o "$EXISTING_OWNER" -g "$EXISTING_GROUP" "$DOWNLOADED_FRPC" "$UPGRADE_BIN"; then rollback_merge 7; fi
    if ! mv "$UPGRADE_BIN" "$EXISTING_BIN"; then rollback_merge 7; fi
    MERGE_BIN_MUTATED=1
    echo "已升级 frpc: $EXISTING_VERSION -> $FRP_VERSION"
    echo "旧二进制备份: $BACKUP_BIN"
  fi
  if ! replace_merge_config; then
    rollback_merge 7
  fi
  echo "已备份原配置: $BACKUP_CONFIG"
elif [ "$DRY_RUN" -eq 1 ]; then
  CONFIG_DIR="$TEMP_DIR/config"
  FRPC_BIN="$TEMP_DIR/frpc"
  install -m 0755 "$DOWNLOADED_FRPC" "$FRPC_BIN"
else
  FRPC_BIN="$DOWNLOADED_FRPC"
fi

if [ -z "$MERGE_CONFIG" ]; then
  install -d -m 0750 "$CONFIG_DIR"
  TOKEN_PATH="$CONFIG_DIR/token"
  TOKEN_SNAPSHOT="$TEMP_DIR/original-frp-token"
  CONFIG_PATH="$CONFIG_DIR/frpc.toml"
  CONFIG_SNAPSHOT="$TEMP_DIR/original-frpc-config"
  if [ -e "$TOKEN_PATH" ] || [ -L "$TOKEN_PATH" ]; then
    [ ! -L "$TOKEN_PATH" ] && [ -f "$TOKEN_PATH" ] || die "FRP token path must be a regular file, not a symlink"
    cp -p "$TOKEN_PATH" "$TOKEN_SNAPSHOT" || die "unable to snapshot the existing FRP token"
    TOKEN_PREEXISTING=1
  fi
  if [ -e "$CONFIG_PATH" ] || [ -L "$CONFIG_PATH" ]; then
    [ ! -L "$CONFIG_PATH" ] && [ -f "$CONFIG_PATH" ] || die "FRP config path must be a regular file, not a symlink"
    cp -p "$CONFIG_PATH" "$CONFIG_SNAPSHOT" || die "unable to snapshot the existing FRP config"
    CONFIG_PREEXISTING=1
  fi
  TOKEN_ROLLBACK_NEEDED=1
  CONFIG_ROLLBACK_NEEDED=1
  if ! write_private_file_atomic "$TOKEN_PATH" "$FRP_TOKEN"; then
    die "unable to write FRP token atomically"
  fi
  TOKEN_MUTATED=1
  if ! write_private_file_atomic_from_stdin "$CONFIG_PATH" <<EOF
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
  then
    die "unable to write FRP config atomically"
  fi
  CONFIG_MUTATED=1
  "$FRPC_BIN" verify -c "$CONFIG_PATH"
fi

if [ "$DRY_RUN" -eq 0 ] && [ -z "$MERGE_CONFIG" ]; then
  prepare_normal_service_transaction
  FRPC_BINARY_SNAPSHOT="$TEMP_DIR/original-frpc"
  if [ -e "$FRPC_BINARY_PATH" ] || [ -L "$FRPC_BINARY_PATH" ]; then
    [ ! -L "$FRPC_BINARY_PATH" ] && [ -f "$FRPC_BINARY_PATH" ] || die "frpc binary path must be a regular file, not a symlink"
    cp -p "$FRPC_BINARY_PATH" "$FRPC_BINARY_SNAPSHOT" || die "unable to snapshot the existing frpc binary"
    FRPC_BINARY_PREEXISTING=1
  fi
  FRPC_BINARY_ROLLBACK_NEEDED=1
  FRPC_BINARY_TEMP="$TEMP_DIR/frpc-installed"
  if ! install -m 0755 "$DOWNLOADED_FRPC" "$FRPC_BINARY_TEMP" \
    || ! mv -f "$FRPC_BINARY_TEMP" "$FRPC_BINARY_PATH"; then
    die "unable to install frpc atomically"
  fi
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
  # The home directory and its SSH files are user-controlled.  Perform every
  # mutation as that user so a symlink cannot make a root installer write to an
  # unrelated privileged path.  The helper also rejects links/non-regular
  # files before touching either path.
  authorized_key_script='
    set -eu
    user_home=$1
    public_key=$2
    ssh_dir=$user_home/.ssh
    authorized=$ssh_dir/authorized_keys
    if [ -L "$ssh_dir" ] || { [ -e "$ssh_dir" ] && [ ! -d "$ssh_dir" ]; }; then
      echo "SSH .ssh 必须是目录且不能是符号链接" >&2
      exit 5
    fi
    if [ ! -e "$ssh_dir" ]; then
      umask 077
      mkdir "$ssh_dir"
    fi
    chmod 0700 "$ssh_dir"
    if [ -L "$authorized" ] || { [ -e "$authorized" ] && [ ! -f "$authorized" ]; }; then
      echo "SSH authorized_keys 必须是普通文件且不能是符号链接" >&2
      exit 5
    fi
    if [ ! -e "$authorized" ]; then
      umask 077
      : > "$authorized"
    fi
    chmod 0600 "$authorized"
    if ! grep -Fq -- "$public_key" "$authorized"; then
      printf "%s\\n" "$public_key" >> "$authorized"
    fi
  '
  if [ "$OS_NAME" = Linux ]; then
    runuser -u "$SSH_USER_NAME" -- sh -c "$authorized_key_script" sh "$USER_HOME" "$AGENTSERVER_PUBLIC_KEY"
  else
    /usr/bin/sudo -u "$SSH_USER_NAME" -- sh -c "$authorized_key_script" sh "$USER_HOME" "$AGENTSERVER_PUBLIC_KEY"
  fi
}
install_authorized_key

find_linux_ssh_service() {
  SSH_SERVICE_ID=""
  for candidate in ssh.service sshd.service; do
    if [ "$(systemctl show "$candidate" -p LoadState --value 2>/dev/null || true)" = loaded ]; then
      SSH_SERVICE_ID=$(systemctl show "$candidate" -p Id --value)
      break
    fi
  done
}

install_linux_ssh_server() {
  echo "未找到 OpenSSH Server，正在自动安装..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y openssh-server
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y openssh-server
  elif command -v yum >/dev/null 2>&1; then
    yum install -y openssh-server
  else
    echo "无法识别可用的包管理器，请先手动安装 OpenSSH Server"
    return 1
  fi
}

enable_linux_ssh() {
  find_linux_ssh_service
  if [ -z "$SSH_SERVICE_ID" ]; then
    if ! install_linux_ssh_server; then return 1; fi
    if ! systemctl daemon-reload; then return 1; fi
    find_linux_ssh_service
    if [ -z "$SSH_SERVICE_ID" ]; then
      echo "OpenSSH Server 已安装，但未找到 ssh.service 或 sshd.service"
      return 1
    fi
  fi
  echo "启用 OpenSSH 服务: $SSH_SERVICE_ID"
  systemctl enable --now "$SSH_SERVICE_ID" || return 1
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

prepare_normal_service_transaction() {
  if [ "$OS_NAME" = Linux ]; then
    FRP_UNIT_PATH=/etc/systemd/system/frpc-agentserver.service
    if systemctl cat frpc-agentserver.service >/dev/null 2>&1; then
      FRP_SERVICE_PREEXISTING=1
      systemctl is-active --quiet frpc-agentserver.service 2>/dev/null && FRP_SERVICE_ACTIVE=1 || true
      systemctl is-enabled --quiet frpc-agentserver.service 2>/dev/null && FRP_SERVICE_ENABLED=1 || true
    fi
  else
    FRP_UNIT_PATH=/Library/LaunchDaemons/com.agentserver.frpc.plist
    if launchctl print system/com.agentserver.frpc >/dev/null 2>&1; then
      FRP_SERVICE_PREEXISTING=1
      FRP_SERVICE_ACTIVE=1
      FRP_SERVICE_ENABLED=1
      if launchctl print-disabled system 2>/dev/null \
        | grep -Eq '"com.agentserver.frpc"[[:space:]]*=>[[:space:]]*true'; then
        FRP_SERVICE_ENABLED=0
      fi
    fi
  fi
  FRP_UNIT_SNAPSHOT="$TEMP_DIR/original-frpc-unit"
  if [ -e "$FRP_UNIT_PATH" ] || [ -L "$FRP_UNIT_PATH" ]; then
    [ ! -L "$FRP_UNIT_PATH" ] && [ -f "$FRP_UNIT_PATH" ] || die "FRP service unit must be a regular file, not a symlink"
    cp -p "$FRP_UNIT_PATH" "$FRP_UNIT_SNAPSHOT" || die "unable to snapshot the existing FRP service unit"
    FRP_UNIT_PREEXISTING=1
  fi
  FRP_UNIT_ROLLBACK_NEEDED=1
}

if [ -n "$MERGE_CONFIG" ]; then
  if [ "$OS_NAME" = Linux ]; then
    if ! enable_linux_ssh; then rollback_merge 5; fi
    if ! replace_merge_systemd_unit; then rollback_merge 7; fi
    if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
      MERGE_PROCESS_STOPPED=1
      if ! kill "$EXISTING_PID" 2>/dev/null && kill -0 "$EXISTING_PID" 2>/dev/null; then
        rollback_merge 7
      fi
    fi
    if ! systemctl daemon-reload; then rollback_merge 7; fi
    if ! systemctl enable --now frpc-agentserver.service; then rollback_merge 7; fi
    if ! systemctl restart frpc-agentserver.service; then rollback_merge 7; fi
    if ! require_stable_systemd_service frpc-agentserver.service; then rollback_merge 7; fi
    systemctl --no-pager --full status frpc-agentserver.service | sed -n '1,16p'
  else
    if ! /usr/sbin/systemsetup -setremotelogin on >/dev/null; then
      echo "无法启用 Remote Login。请给终端 Full Disk Access 后重试。" >&2
      rollback_merge 5
    fi
    MERGE_LOG="$USER_HOME/frpc-agentserver.log"
    MERGE_PLIST="$MERGE_UNIT_PATH"
    require_safe_launchd_value "现有 frpc 工作目录" "$EXISTING_CWD" || rollback_merge 6
    require_safe_launchd_value "现有 frpc 可执行文件" "$EXISTING_BIN" || rollback_merge 6
    require_safe_launchd_value "现有 frpc 配置" "$MERGE_CONFIG" || rollback_merge 6
    require_safe_launchd_value "frpc 日志路径" "$MERGE_LOG" || rollback_merge 6
    if ! replace_merge_launchd_plist; then rollback_merge 7; fi
    if ! plutil -lint "$MERGE_PLIST" >/dev/null; then rollback_merge 7; fi
    if ! bootout_launchd_job "$MERGE_UNIT_LABEL"; then rollback_merge 7; fi
    if kill -0 "$EXISTING_PID" 2>/dev/null; then
      MERGE_PROCESS_STOPPED=1
      if ! kill "$EXISTING_PID" 2>/dev/null && kill -0 "$EXISTING_PID" 2>/dev/null; then
        rollback_merge 7
      fi
    fi
    for wait_step in 1 2 3 4 5; do
      if ! kill -0 "$EXISTING_PID" 2>/dev/null; then break; fi
      sleep 1
    done
    if kill -0 "$EXISTING_PID" 2>/dev/null; then
      kill -KILL "$EXISTING_PID"
    fi
    if ! launchctl bootstrap system "$MERGE_PLIST"; then rollback_merge 7; fi
    if [ "$MERGE_SERVICE_ENABLED" -eq 1 ]; then
      if ! launchctl enable "system/$MERGE_UNIT_LABEL"; then rollback_merge 7; fi
    else
      if ! launchctl disable "system/$MERGE_UNIT_LABEL"; then rollback_merge 7; fi
    fi
    if ! require_stable_launchd_job "$MERGE_UNIT_LABEL"; then rollback_merge 7; fi
    launchctl print "system/$MERGE_UNIT_LABEL" | sed -n '1,22p' || rollback_merge 7
  fi
  echo
  echo "合并安装完成"
  echo "保留原配置和 proxy，并新增: $DEVICE_ID.ssh"
  echo "SSH 用户: $SSH_USER_NAME"
  echo "服务器入口: $FRP_SERVER:$REMOTE_PORT"
  echo "返回 AgentServer 页面，等待约 15 秒后点击“同步 FRP”。"
  MERGE_ROLLBACK_NEEDED=0
  exit 0
fi

if [ "$OS_NAME" = Linux ]; then
  if ! enable_linux_ssh; then
    exit 5
  fi
  if ! write_unit_atomic_from_stdin /etc/systemd/system/frpc-agentserver.service 0644 <<EOF
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
  then
    exit 7
  fi
  systemctl daemon-reload
  systemctl enable --now frpc-agentserver.service
  systemctl restart frpc-agentserver.service
  if ! require_stable_systemd_service frpc-agentserver.service; then
    exit 7
  fi
  systemctl --no-pager --full status frpc-agentserver.service | sed -n '1,16p'
  FRP_UNIT_ROLLBACK_NEEDED=0
else
  /usr/sbin/systemsetup -setremotelogin on >/dev/null
  if ! write_unit_atomic_from_stdin /Library/LaunchDaemons/com.agentserver.frpc.plist 0644 <<EOF
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
  then
    exit 7
  fi
  chmod 0644 /Library/LaunchDaemons/com.agentserver.frpc.plist
  bootout_launchd_job com.agentserver.frpc || exit 7
  launchctl bootstrap system /Library/LaunchDaemons/com.agentserver.frpc.plist || exit 7
  launchctl enable system/com.agentserver.frpc || exit 7
  require_stable_launchd_job com.agentserver.frpc || exit 7
  FRP_UNIT_ROLLBACK_NEEDED=0
fi

TOKEN_ROLLBACK_NEEDED=0
CONFIG_ROLLBACK_NEEDED=0
FRPC_BINARY_ROLLBACK_NEEDED=0

echo
echo "安装完成"
echo "设备 ID: $DEVICE_ID"
echo "代理名称: $DEVICE_ID.ssh"
echo "SSH 用户: $SSH_USER_NAME"
echo "服务器入口: $FRP_SERVER:$REMOTE_PORT"
echo "返回 AgentServer 页面，等待约 15 秒后点击“同步 FRP”。"
