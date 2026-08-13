#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "错误: 请以 root 运行发布脚本" >&2
  exit 2
fi
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
  echo "用法: $0 /path/to/agentserver-<sha>.tar.gz" >&2
  exit 2
fi

ARTIFACT="$(realpath "$1")"
APP_ROOT="${AGENTSERVER_APP_ROOT:-/opt/agentserver}"
RELEASES_DIR="$APP_ROOT/releases"
CURRENT_LINK="$APP_ROOT/current"
mkdir -p "$RELEASES_DIR"
INCOMING_DIR="$(mktemp -d "$RELEASES_DIR/.incoming.XXXXXX")"
PREVIOUS_RELEASE="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
NEW_RELEASE=""
RELEASE_ACTIVATED=0
SERVICE_UNIT=/etc/systemd/system/agentserver.service
SERVICE_BACKUP="$RELEASES_DIR/.agentserver.service.previous.$$"

if [ -f "$ARTIFACT.sha256" ]; then
  expected="$(awk 'NR == 1 {print $1}' "$ARTIFACT.sha256")"
  actual="$(sha256sum "$ARTIFACT" | awk '{print $1}')"
  if [ "$actual" != "$expected" ]; then
    echo "错误: 发布制品 SHA-256 校验失败" >&2
    exit 2
  fi
fi

if tar -tzf "$ARTIFACT" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  echo "错误: 发布制品包含不安全路径" >&2
  exit 2
fi

cleanup() {
  if [ -n "$INCOMING_DIR" ] && [ -d "$INCOMING_DIR" ]; then rm -rf "$INCOMING_DIR"; fi
  if [ "$RELEASE_ACTIVATED" -eq 0 ] && [ -n "$NEW_RELEASE" ] && [ -d "$NEW_RELEASE" ]; then
    rm -rf "$NEW_RELEASE"
  fi
  rm -f "$SERVICE_BACKUP"
}
trap cleanup EXIT

tar -xzf "$ARTIFACT" -C "$INCOMING_DIR"
BUILD_SHA="$(tr -d '\r\n' < "$INCOMING_DIR/BUILD_SHA")"
if ! [[ "$BUILD_SHA" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]]; then
  echo "错误: 发布制品缺少有效 BUILD_SHA" >&2
  exit 2
fi
python3 - "$INCOMING_DIR/web_dist/build.json" "$BUILD_SHA" <<'PY'
import json, pathlib, sys
manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if manifest.get("build_sha") != sys.argv[2]:
    raise SystemExit("frontend build manifest does not match BUILD_SHA")
PY

NEW_RELEASE="$RELEASES_DIR/$BUILD_SHA"
if [ -e "$NEW_RELEASE" ]; then
  echo "错误: 发布版本已存在: $NEW_RELEASE" >&2
  exit 2
fi

mv "$INCOMING_DIR" "$NEW_RELEASE"
INCOMING_DIR=""
python3 -m venv "$NEW_RELEASE/.venv"
"$NEW_RELEASE/.venv/bin/pip" install -q --upgrade pip
"$NEW_RELEASE/.venv/bin/pip" install -q -r "$NEW_RELEASE/requirements.txt"

if [ -f "$SERVICE_UNIT" ]; then cp -a "$SERVICE_UNIT" "$SERVICE_BACKUP"; fi
install -m 0644 "$NEW_RELEASE/deploy/agentserver.service" "$SERVICE_UNIT"
install -m 0644 "$NEW_RELEASE/deploy/agentserver-tmux.service" /etc/systemd/system/agentserver-tmux.service
rm -f "$APP_ROOT/.current.new"
ln -s "$NEW_RELEASE" "$APP_ROOT/.current.new"
mv -Tf "$APP_ROOT/.current.new" "$CURRENT_LINK"
systemctl daemon-reload

rollback() {
  if [ "$PREVIOUS_RELEASE" = "$APP_ROOT" ]; then
    # Legacy installs used /opt/agentserver itself as the live tree. The
    # restored legacy unit does not need a current symlink.
    rm -f "$CURRENT_LINK"
  elif [ -n "$PREVIOUS_RELEASE" ] && [ -d "$PREVIOUS_RELEASE" ]; then
    rm -f "$APP_ROOT/.current.rollback"
    ln -s "$PREVIOUS_RELEASE" "$APP_ROOT/.current.rollback"
    mv -Tf "$APP_ROOT/.current.rollback" "$CURRENT_LINK"
  else
    rm -f "$CURRENT_LINK"
  fi
  if [ -f "$SERVICE_BACKUP" ]; then cp -a "$SERVICE_BACKUP" "$SERVICE_UNIT"; fi
  systemctl daemon-reload || true
  systemctl restart agentserver.service || true
  echo "部署失败，已回滚到 ${PREVIOUS_RELEASE:-旧版服务配置}" >&2
}

if ! systemctl restart agentserver.service; then
  rollback
  exit 1
fi
if ! "$NEW_RELEASE/.venv/bin/python" "$NEW_RELEASE/scripts/smoke_release.py" \
  --base-url "${DEPLOY_SMOKE_URL:-http://127.0.0.1:18100}" \
  --expected-sha "$BUILD_SHA" \
  --env-file /etc/agentserver/agentserver.env; then
  rollback
  exit 1
fi

RELEASE_ACTIVATED=1
echo "部署成功: $BUILD_SHA"
