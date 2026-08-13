#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "请以 root 运行此脚本"
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR=/opt/agentserver
CONFIG_DIR=/etc/agentserver
STATE_DIR=/var/lib/agentserver

if [ "$ROOT_DIR" != "$APP_DIR" ]; then
  echo "代码应位于 $APP_DIR，当前为 $ROOT_DIR"
  exit 1
fi

if ! id agentserver >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin agentserver
fi

install -d -m 0750 -o agentserver -g agentserver "$STATE_DIR" "$STATE_DIR/ssh"
install -d -m 0700 -o agentserver -g agentserver "$STATE_DIR/tmux"
install -d -m 0750 "$CONFIG_DIR"

if ! command -v tmux >/dev/null 2>&1; then
  apt-get update
  apt-get install -y tmux
fi

if [ ! -f "$CONFIG_DIR/agentserver.env" ]; then
  install -m 0600 deploy/agentserver.env.example "$CONFIG_DIR/agentserver.env"
  echo "已创建 $CONFIG_DIR/agentserver.env，请填写 CHANGE_ME 后重新运行"
  exit 2
fi

if grep -q 'CHANGE_ME' "$CONFIG_DIR/agentserver.env"; then
  echo "$CONFIG_DIR/agentserver.env 仍包含 CHANGE_ME，拒绝启动"
  exit 2
fi

if ! grep -q '^TERMINAL_BACKEND=' "$CONFIG_DIR/agentserver.env"; then
  echo "TERMINAL_BACKEND=tmux" >> "$CONFIG_DIR/agentserver.env"
fi
if ! grep -q '^TMUX_SOCKET=' "$CONFIG_DIR/agentserver.env"; then
  echo "TMUX_SOCKET=/var/lib/agentserver/tmux/agentserver.sock" >> "$CONFIG_DIR/agentserver.env"
fi

if [ ! -f "$STATE_DIR/ssh/id_ed25519" ]; then
  ssh-keygen -q -t ed25519 -N '' -C agentserver-fleet -f "$STATE_DIR/ssh/id_ed25519"
fi
chown -R agentserver:agentserver "$STATE_DIR"
chmod 0600 "$STATE_DIR/ssh/id_ed25519"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# Keep source-based first installs working with the versioned service path.
# Normal updates must use build_release.sh + deploy_release.sh instead.
if [ ! -e "$APP_DIR/current" ]; then
  ln -s . "$APP_DIR/current"
fi
if command -v git >/dev/null 2>&1 && git -C "$APP_DIR" rev-parse HEAD >/dev/null 2>&1; then
  BUILD_SHA="$(git -C "$APP_DIR" rev-parse HEAD)"
  if ! command -v npm >/dev/null 2>&1; then
    echo "错误: 首次安装需要 Node.js/npm 来构建带版本标识的前端" >&2
    exit 2
  fi
  npm --prefix "$APP_DIR/frontend" ci
  AGENTSERVER_BUILD_SHA="$BUILD_SHA" npm --prefix "$APP_DIR/frontend" run build
  rm -rf "$APP_DIR/web_dist"
  mv "$APP_DIR/frontend/dist" "$APP_DIR/web_dist"
  printf '%s\n' "$BUILD_SHA" > "$APP_DIR/BUILD_SHA"
  printf '{"build_sha":"%s"}\n' "$BUILD_SHA" > "$APP_DIR/web_dist/build.json"
fi

install -m 0644 deploy/agentserver.service /etc/systemd/system/agentserver.service
install -m 0644 deploy/agentserver-tmux.service /etc/systemd/system/agentserver-tmux.service
systemctl daemon-reload
systemctl enable --now agentserver-tmux.service
systemctl enable agentserver.service
systemctl restart agentserver.service
systemctl --no-pager --full status agentserver.service

echo "SSH 公钥（加入每台设备的 authorized_keys）："
cat "$STATE_DIR/ssh/id_ed25519.pub"
