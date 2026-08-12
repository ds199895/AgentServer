#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "请以 root 运行此脚本"
  exit 1
fi

CONFIG_DIR=/etc/agentserver
ENV_FILE="$CONFIG_DIR/agentserver.env"
CREDENTIALS_FILE=/root/agentserver-initial-credentials.txt
FRPS_CONFIG="${FRPS_CONFIG:-/opt/frp/frps.toml}"

if [ -f "$ENV_FILE" ]; then
  echo "$ENV_FILE 已存在，未覆盖"
  exit 0
fi

dashboard_user=$(sed -nE 's/^[[:space:]]*webServer\.user[[:space:]]*=[[:space:]]*"([^"]*)".*/\1/p' "$FRPS_CONFIG")
dashboard_password=$(sed -nE 's/^[[:space:]]*webServer\.password[[:space:]]*=[[:space:]]*"([^"]*)".*/\1/p' "$FRPS_CONFIG")

if [ -z "$dashboard_user" ] || [ -z "$dashboard_password" ]; then
  echo "无法从 $FRPS_CONFIG 读取 Dashboard 凭据"
  exit 2
fi

case "$dashboard_user$dashboard_password" in
  *[!A-Za-z0-9._-]*)
    echo "Dashboard 凭据包含 EnvironmentFile 不支持的字符，请手动配置"
    exit 2
    ;;
esac

admin_password=$(openssl rand -hex 24)
session_secret=$(openssl rand -hex 48)
install -d -m 0750 "$CONFIG_DIR"
umask 077

{
  echo "DATA_DIR=/var/lib/agentserver"
  echo "WEB_DIST=/opt/agentserver/web_dist"
  echo "ENVIRONMENT=production"
  echo "ADMIN_USERNAME=admin"
  echo "ADMIN_PASSWORD=$admin_password"
  echo "SESSION_SECRET=$session_secret"
  echo "COOKIE_SECURE=0"
  echo "ENABLE_LOCAL_TERMINALS=0"
  echo "TERMINAL_CWD=/var/lib/agentserver"
  echo "TERMINAL_CMD="
  echo "TERMINAL_BACKEND=tmux"
  echo "TMUX_SOCKET=/var/lib/agentserver/tmux/agentserver.sock"
  echo "FRPS_DASHBOARD_URL=http://127.0.0.1:7500"
  echo "FRPS_DASHBOARD_USER=$dashboard_user"
  echo "FRPS_DASHBOARD_PASSWORD=$dashboard_password"
  echo "FRPS_SYNC_INTERVAL=15"
  echo "FRPS_AUTO_DISCOVER=1"
  echo "FRP_PROXY_HOST=127.0.0.1"
  echo "SSH_PRIVATE_KEY=/var/lib/agentserver/ssh/id_ed25519"
  echo "SSH_KNOWN_HOSTS=/var/lib/agentserver/ssh/known_hosts"
  echo "SSH_STRICT_HOST_KEY=accept-new"
} > "$ENV_FILE"
chmod 0600 "$ENV_FILE"

{
  echo "AgentServer 初始登录信息"
  echo "用户名: admin"
  echo "密码: $admin_password"
  echo "浏览器: http://101.43.103.46:18100"
  echo "安全隧道: ssh -L 18100:127.0.0.1:18100 root@101.43.103.46"
  echo "登录后请立即修改密码。"
} > "$CREDENTIALS_FILE"
chmod 0600 "$CREDENTIALS_FILE"

echo "已生成 $ENV_FILE"
echo "初始登录信息仅保存在 $CREDENTIALS_FILE"
