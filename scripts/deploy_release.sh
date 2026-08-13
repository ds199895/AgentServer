#!/usr/bin/env bash
set -euo pipefail
# The forced-command receiver uses 077 for upload secrecy. Release files contain
# no runtime secrets and must be readable/executable by the service account.
umask 022

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
# The restricted SSH receiver intentionally runs with umask 077. Explicitly
# keep the release path traversable by the unprivileged service account.
install -d -m 0755 "$APP_ROOT" "$RELEASES_DIR"
INCOMING_DIR="$(mktemp -d "$RELEASES_DIR/.incoming.XXXXXX")"
if [ -L "$CURRENT_LINK" ]; then
  PREVIOUS_RELEASE="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
elif [ -e "$CURRENT_LINK" ]; then
  echo "错误: current 存在但不是符号链接: $CURRENT_LINK" >&2
  exit 2
elif [ -x "$APP_ROOT/.venv/bin/python" ] && [ -d "$APP_ROOT/app" ]; then
  # Before the first immutable release, the live application is APP_ROOT.
  PREVIOUS_RELEASE="$APP_ROOT"
else
  PREVIOUS_RELEASE=""
fi
NEW_RELEASE=""
RELEASE_ACTIVATED=0
SERVICE_UNIT=/etc/systemd/system/agentserver.service
SERVICE_BACKUP="$RELEASES_DIR/.agentserver.service.previous.$$"
LEGACY_DROPIN=/etc/systemd/system/agentserver.service.d/https-local-listen.conf
DROPIN_BACKUP="$RELEASES_DIR/.agentserver.dropin.previous.$$"
HAD_LEGACY_DROPIN=0

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
  rm -f "$DROPIN_BACKUP"
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
  if [ "$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)" = "$NEW_RELEASE" ]; then
    rm -rf "$INCOMING_DIR"
    INCOMING_DIR=""
    RELEASE_ACTIVATED=1
    "$NEW_RELEASE/.venv/bin/python" "$NEW_RELEASE/scripts/smoke_release.py" \
      --base-url "${DEPLOY_SMOKE_URL:-http://127.0.0.1:18100}" \
      --expected-sha "$BUILD_SHA" \
      --env-file /etc/agentserver/agentserver.env
    echo "版本已部署，smoke 复核通过: $BUILD_SHA"
    exit 0
  fi
  echo "错误: 非当前发布目录已存在，拒绝覆盖: $NEW_RELEASE" >&2
  exit 2
fi

mv "$INCOMING_DIR" "$NEW_RELEASE"
INCOMING_DIR=""
# The restricted SSH receiver uses umask 077. The application service runs as
# the unprivileged agentserver user and must be able to traverse the release.
chmod 0755 "$NEW_RELEASE"
python3 -m venv "$NEW_RELEASE/.venv"
if [ -d "$NEW_RELEASE/wheelhouse" ]; then
  "$NEW_RELEASE/.venv/bin/pip" install -q \
    --no-index \
    --find-links "$NEW_RELEASE/wheelhouse" \
    -r "$NEW_RELEASE/requirements.txt"
else
  "$NEW_RELEASE/.venv/bin/pip" install -q -r "$NEW_RELEASE/requirements.txt"
fi

if ! runuser -u agentserver -- sh -c 'cd "$1" && test -x .venv/bin/python' sh "$NEW_RELEASE"; then
  echo "错误: agentserver 用户无法进入或执行新 release" >&2
  exit 2
fi

if [ -f "$SERVICE_UNIT" ]; then cp -a "$SERVICE_UNIT" "$SERVICE_BACKUP"; fi
if [ -f "$LEGACY_DROPIN" ]; then
  cp -a "$LEGACY_DROPIN" "$DROPIN_BACKUP"
  HAD_LEGACY_DROPIN=1
  rm -f "$LEGACY_DROPIN"
fi
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
  if [ "$HAD_LEGACY_DROPIN" -eq 1 ]; then
    install -d -m 0755 "$(dirname "$LEGACY_DROPIN")"
    cp -a "$DROPIN_BACKUP" "$LEGACY_DROPIN"
  fi
  systemctl daemon-reload || true
  systemctl restart agentserver.service || true
  echo "部署失败，已回滚到 ${PREVIOUS_RELEASE:-旧版服务配置}" >&2
}

resolved_working_directory="$(systemctl show agentserver.service -p WorkingDirectory --value)"
resolved_exec_start="$(systemctl show agentserver.service -p ExecStart --value)"
if [ "$resolved_working_directory" != "$CURRENT_LINK" ] ||
   [[ "$resolved_exec_start" != *"$CURRENT_LINK/.venv/bin/python"* ]]; then
  echo "错误: systemd 最终配置未指向 current release" >&2
  rollback
  exit 2
fi

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
