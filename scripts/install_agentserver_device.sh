#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "$0")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FRP_INSTALLER="$ROOT_DIR/scripts/install_frpc_ssh.sh"
RUNTIME_INSTALLER="$ROOT_DIR/scripts/install_agentserver_runtime.sh"
DEVICE_ID=""
BASE_URL=""
REMOTE_PORT=""
SSH_USER=""
RUNTIME_USER=""
FRP_SERVER="${FRP_SERVER:-101.43.103.46}"
FRP_SERVER_PORT="${FRP_SERVER_PORT:-7000}"
FRP_VERSION="${FRP_VERSION:-0.69.0}"
FRP_TOKEN_FILE=""
FRP_ROTATE_TOKEN=0
MERGE_CONFIG=""
ENROLLMENT_TOKEN_FILE=""
STATE_DIR=""
CODEX_BIN=""
NODE_BIN=""
BWRAP_BIN=""
RUNTIME_ONLY=0
REENROLL=0
TOKEN_STAGE_DIR=""
VENV_STAGE_DIR=""
OLD_VENV_DIR=""
VENV_RESTORE_PENDING=0
RUNTIME_BUILD_SHA="${AGENTSERVER_RUNTIME_BUILD_SHA:-}"
RUNTIME_BUNDLE_BASE_URL="${AGENTSERVER_RUNTIME_BUNDLE_BASE_URL:-}"
RUNTIME_BUNDLE_READY="${AGENTSERVER_RUNTIME_BUNDLE_READY:-0}"
# Runtime bundles are immutable.  The bootstrapper points this at a private
# per-build environment outside the extracted release; checkout installs keep
# the historical .venv default for local development.
RUNTIME_VENV_DIR="${AGENTSERVER_RUNTIME_VENV_DIR:-$ROOT_DIR/.venv}"
RUNTIME_VENV_PARENT=""
RUNTIME_PYTHON_BIN=""

usage() {
  cat >&2 <<'EOF'
Install a complete AgentServer Linux device: SSH/FRP plus the outbound Runtime Host.

Usage:
  install_agentserver_device.sh \
    --device-id ID --base-url URL --remote-port PORT [options]

Required for a new device:
  --device-id ID                 Existing AgentServer device ID
  --base-url URL                 AgentServer HTTPS URL
  --remote-port PORT             Unique FRP SSH port, 20000-29999

Options:
  --ssh-user USER                SSH login user; defaults to the Runtime user
  --runtime-user USER            Non-root user holding workspaces and Codex login
  --frp-server HOST              frps host
  --frp-server-port PORT         frps control port
  --frp-version VERSION          frpc version supported by the child installer
  --frp-token-file FILE          Private mode-0600 FRP token file
  --rotate-frp-token             Explicitly replace a matching managed FRP token
  --merge-existing FILE          Merge SSH proxy into an existing frpc config
  --enrollment-token-file FILE   Private mode-0600 one-time enrollment file
  --state-dir DIR                Runtime state directory for the Runtime user
  --codex-binary PATH            Absolute Codex CLI path
  --node-binary PATH             Absolute Node.js path
  --bubblewrap-binary PATH       Absolute bubblewrap path
  --runtime-build-sha SHA        Pin the downloaded Runtime bundle build
  --runtime-bundle-url URL       Runtime bundle manifest base URL
  --runtime-only                 Keep an existing SSH/FRP installation
  --reenroll                     Explicitly replace an existing Runtime credential
  --help                         Show this help

Omitted secrets are requested with hidden terminal prompts. Secret values are
never accepted as command-line arguments or persisted in a systemd unit.
EOF
}

die() {
  echo "agentserver-device-install: $*" >&2
  exit 2
}

cleanup() {
  status=$?
  if [ -n "$TOKEN_STAGE_DIR" ] && [ -d "$TOKEN_STAGE_DIR" ]; then
    rm -rf -- "$TOKEN_STAGE_DIR"
  fi
  if [ -n "$VENV_STAGE_DIR" ] && [ -d "$VENV_STAGE_DIR" ]; then
    rm -rf -- "$VENV_STAGE_DIR"
  fi
  if [ "$status" -ne 0 ] && [ "$VENV_RESTORE_PENDING" -eq 1 ] \
    && [ -n "$OLD_VENV_DIR" ] && [ -d "$OLD_VENV_DIR" ] \
    && [ ! -e "$RUNTIME_VENV_DIR" ] && [ ! -L "$RUNTIME_VENV_DIR" ]; then
    # Preserve a known-good environment if activation failed after it was
    # staged.  Cleanup must never delete the only remaining interpreter.
    if mv -- "$OLD_VENV_DIR" "$RUNTIME_VENV_DIR"; then
      VENV_RESTORE_PENDING=0
    fi
  fi
  if [ "$VENV_RESTORE_PENDING" -eq 0 ] && [ -n "$OLD_VENV_DIR" ] && [ -d "$OLD_VENV_DIR" ]; then
    rm -rf -- "$OLD_VENV_DIR"
  fi
  trap - EXIT
  exit "$status"
}
trap cleanup EXIT

while [ "$#" -gt 0 ]; do
  case "$1" in
    --device-id) DEVICE_ID="${2:-}"; shift 2 ;;
    --base-url) BASE_URL="${2:-}"; shift 2 ;;
    --remote-port) REMOTE_PORT="${2:-}"; shift 2 ;;
    --ssh-user) SSH_USER="${2:-}"; shift 2 ;;
    --runtime-user) RUNTIME_USER="${2:-}"; shift 2 ;;
    --frp-server) FRP_SERVER="${2:-}"; shift 2 ;;
    --frp-server-port) FRP_SERVER_PORT="${2:-}"; shift 2 ;;
    --frp-version) FRP_VERSION="${2:-}"; shift 2 ;;
    --frp-token-file) FRP_TOKEN_FILE="${2:-}"; shift 2 ;;
    --rotate-frp-token) FRP_ROTATE_TOKEN=1; shift ;;
    --merge-existing) MERGE_CONFIG="${2:-}"; shift 2 ;;
    --enrollment-token-file) ENROLLMENT_TOKEN_FILE="${2:-}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    --codex-binary) CODEX_BIN="${2:-}"; shift 2 ;;
    --node-binary) NODE_BIN="${2:-}"; shift 2 ;;
    --bubblewrap-binary) BWRAP_BIN="${2:-}"; shift 2 ;;
    --runtime-build-sha) RUNTIME_BUILD_SHA="${2:-}"; shift 2 ;;
    --runtime-bundle-url) RUNTIME_BUNDLE_BASE_URL="${2:-}"; shift 2 ;;
    --runtime-only) RUNTIME_ONLY=1; shift ;;
    --reenroll) REENROLL=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage; die "unknown argument: $1" ;;
  esac
done

[ "$(uname -s)" = Linux ] || die "the managed Runtime Host currently supports Linux only"
[[ "$DEVICE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$ ]] || die "invalid --device-id"
if [[ ! "$BASE_URL" =~ ^https://[^[:space:]]+$ && ! "$BASE_URL" =~ ^http://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?(/[^[:space:]]*)?$ ]]; then
  die "--base-url must use HTTPS (or loopback HTTP for development)"
fi
if [ "$RUNTIME_ONLY" -ne 1 ]; then
  [[ "$REMOTE_PORT" =~ ^[0-9]+$ ]] || die "--remote-port is required for a new device"
  [ "$REMOTE_PORT" -ge 20000 ] && [ "$REMOTE_PORT" -le 29999 ] || die "--remote-port must be between 20000 and 29999"
  [[ "$FRP_SERVER" =~ ^[A-Za-z0-9._:-]+$ ]] || die "invalid --frp-server"
  [[ "$FRP_SERVER_PORT" =~ ^[0-9]+$ ]] \
    && [ "$FRP_SERVER_PORT" -ge 1 ] && [ "$FRP_SERVER_PORT" -le 65535 ] \
    || die "--frp-server-port must be between 1 and 65535"
  [ "$FRP_VERSION" = 0.69.0 ] || die "unsupported --frp-version"
fi
[ "$RUNTIME_ONLY" -ne 1 ] || [ -z "$MERGE_CONFIG" ] \
  || die "--merge-existing cannot be combined with --runtime-only"
[ "$RUNTIME_ONLY" -ne 1 ] || [ "$FRP_ROTATE_TOKEN" -eq 0 ] \
  || die "--rotate-frp-token cannot be combined with --runtime-only"
if [ -n "$MERGE_CONFIG" ]; then
  case "$MERGE_CONFIG" in
    /*) ;;
    *) MERGE_CONFIG="$(pwd -P)/$MERGE_CONFIG" ;;
  esac
  [ -f "$MERGE_CONFIG" ] && [ ! -L "$MERGE_CONFIG" ] \
    || die "--merge-existing must reference a regular file, not a symlink"
fi
CURRENT_UID="$(id -u)"
CURRENT_USER="$(id -un)"
if [ -z "$RUNTIME_USER" ]; then
  if [ "$CURRENT_UID" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    RUNTIME_USER="$SUDO_USER"
  else
    RUNTIME_USER="$CURRENT_USER"
  fi
fi
printf '%s' "$RUNTIME_USER" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$' || die "invalid --runtime-user"
RUNTIME_PASSWD="$(getent passwd "$RUNTIME_USER" || true)"
[ -n "$RUNTIME_PASSWD" ] || die "Runtime user does not exist: $RUNTIME_USER"
RUNTIME_UID="$(printf '%s\n' "$RUNTIME_PASSWD" | awk -F: '{print $3}')"
RUNTIME_GID="$(printf '%s\n' "$RUNTIME_PASSWD" | awk -F: '{print $4}')"
RUNTIME_HOME="$(printf '%s\n' "$RUNTIME_PASSWD" | awk -F: '{print $6}')"
[ "$RUNTIME_UID" -ne 0 ] || die "Runtime Host must not use root's Codex login or HOME"
[ -d "$RUNTIME_HOME" ] || die "Runtime user HOME does not exist: $RUNTIME_HOME"
if [ "$CURRENT_UID" -ne 0 ] && [ "$CURRENT_UID" -ne "$RUNTIME_UID" ]; then
  die "run as $RUNTIME_USER, or run as root with --runtime-user $RUNTIME_USER"
fi
if [ -z "$SSH_USER" ]; then SSH_USER="$RUNTIME_USER"; fi
printf '%s' "$SSH_USER" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$' || die "invalid --ssh-user"
getent passwd "$SSH_USER" >/dev/null || die "SSH user does not exist: $SSH_USER"

if [ -z "$STATE_DIR" ]; then
  STATE_DIR="$RUNTIME_HOME/.local/state/agentserver-runtime"
elif [[ "$STATE_DIR" != /* ]]; then
  die "--state-dir must be absolute in the combined installer"
fi
RUNTIME_XDG_RUNTIME_DIR="/run/user/$RUNTIME_UID"
RUNTIME_PATH="$RUNTIME_HOME/.local/bin:$RUNTIME_HOME/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

runtime_sources_ready() {
  [ -x "$RUNTIME_INSTALLER" ] \
    && [ -f "$FRP_INSTALLER" ] \
    && [ -d "$ROOT_DIR/app/execution" ] \
    && { [ -f "$ROOT_DIR/requirements-runtime.lock" ] || [ -f "$ROOT_DIR/requirements.txt" ]; }
}

run_root() {
  if [ "$CURRENT_UID" -eq 0 ]; then
    "$@"
  else
    command -v sudo >/dev/null 2>&1 || die "sudo is required for SSH/FRP and user-systemd setup"
    sudo -- "$@"
  fi
}

run_runtime() {
  local runtime_environment=(
    "HOME=$RUNTIME_HOME"
    "USER=$RUNTIME_USER"
    "LOGNAME=$RUNTIME_USER"
    "PATH=$RUNTIME_PATH"
    "XDG_CONFIG_HOME=$RUNTIME_HOME/.config"
    "XDG_STATE_HOME=$RUNTIME_HOME/.local/state"
    "XDG_CACHE_HOME=$RUNTIME_HOME/.cache"
    "XDG_RUNTIME_DIR=$RUNTIME_XDG_RUNTIME_DIR"
    "DBUS_SESSION_BUS_ADDRESS=unix:path=$RUNTIME_XDG_RUNTIME_DIR/bus"
    "LANG=${LANG:-C.UTF-8}"
  )
  if [ "$CURRENT_UID" -eq "$RUNTIME_UID" ]; then
    env -i "${runtime_environment[@]}" "$@"
  else
    runuser -u "$RUNTIME_USER" -- env -i "${runtime_environment[@]}" "$@"
  fi
}

resolve_runtime_binary() {
  local explicit=$1
  local name=$2
  local candidate=""
  if [ -n "$explicit" ]; then
    candidate=$explicit
  elif [ "$CURRENT_UID" -eq "$RUNTIME_UID" ]; then
    candidate="$(command -v "$name" || true)"
  else
    for path in \
      "$RUNTIME_HOME/.local/bin/$name" \
      "$RUNTIME_HOME/.npm-global/bin/$name" \
      /usr/local/bin/"$name" /usr/bin/"$name"; do
      if [ -x "$path" ]; then candidate=$path; break; fi
    done
    if [ -z "$candidate" ]; then
      for path in "$RUNTIME_HOME"/.nvm/versions/node/*/bin/"$name"; do
        if [ -x "$path" ]; then candidate=$path; fi
      done
    fi
  fi
  [ -n "$candidate" ] && [[ "$candidate" == /* ]] && [ -x "$candidate" ] || return 1
  readlink -f "$candidate"
}

read_private_enrollment_file() {
  local token_path=$1 token_metadata opened_metadata path_metadata token_mode token_owner token_size token_value
  [ -f "$token_path" ] && [ ! -L "$token_path" ] || die "enrollment token must be a regular file, not a symlink"
  token_metadata="$(stat -c '%d:%i:%a:%u:%s' "$token_path" 2>/dev/null || true)"
  token_mode="$(printf '%s' "$token_metadata" | cut -d: -f3)"
  token_owner="$(printf '%s' "$token_metadata" | cut -d: -f4)"
  token_size="$(printf '%s' "$token_metadata" | cut -d: -f5)"
  [ "$token_mode" = 600 ] || die "enrollment token file mode must be exactly 0600"
  if [ "$token_owner" != "$CURRENT_UID" ] \
    && [ "$token_owner" != "$RUNTIME_UID" ] \
    && [ "$token_owner" != "${SUDO_UID:-}" ]; then
    die "enrollment token file must be owned by the caller or Runtime user"
  fi
  [[ "$token_size" =~ ^[0-9]+$ ]] && [ "$token_size" -ge 1 ] && [ "$token_size" -le 4096 ] \
    || die "enrollment token file must contain 1-4096 bytes"
  exec 7< "$token_path" || die "unable to open enrollment token file"
  opened_metadata="$(stat -Lc '%d:%i:%a:%u:%s' /dev/fd/7 2>/dev/null || true)"
  if [ "$opened_metadata" != "$token_metadata" ]; then
    exec 7<&-
    die "enrollment token file changed during validation"
  fi
  token_value="$(dd bs=4097 count=1 2>/dev/null <&7)"
  opened_metadata="$(stat -Lc '%d:%i:%a:%u:%s' /dev/fd/7 2>/dev/null || true)"
  path_metadata="$(stat -c '%d:%i:%a:%u:%s' "$token_path" 2>/dev/null || true)"
  exec 7<&-
  [ "$opened_metadata" = "$token_metadata" ] && [ "$path_metadata" = "$token_metadata" ] \
    || die "enrollment token file changed while being read"
  case "$token_value" in
    ''|*[[:space:]]*) die "enrollment token file contains invalid whitespace" ;;
  esac
  printf '%s' "$token_value"
}

CODEX_BIN="$(resolve_runtime_binary "$CODEX_BIN" codex || true)"
[ -n "$CODEX_BIN" ] || die "Codex CLI is unavailable for $RUNTIME_USER; install/login first or pass --codex-binary"
if [ -z "$NODE_BIN" ] && [ -x "$(dirname "$CODEX_BIN")/node" ]; then
  NODE_BIN="$(dirname "$CODEX_BIN")/node"
fi
NODE_BIN="$(resolve_runtime_binary "$NODE_BIN" node || true)"
[ -n "$NODE_BIN" ] || die "Node.js is unavailable for $RUNTIME_USER; pass --node-binary for NVM installations"
BWRAP_BIN="$(resolve_runtime_binary "$BWRAP_BIN" bwrap || true)"
[ -n "$BWRAP_BIN" ] || die "bubblewrap is unavailable; install it before enrollment"

for binary_dir in "$(dirname "$CODEX_BIN")" "$(dirname "$NODE_BIN")" "$(dirname "$BWRAP_BIN")"; do
  case ":$RUNTIME_PATH:" in
    *":$binary_dir:"*) ;;
    *) RUNTIME_PATH="$binary_dir:$RUNTIME_PATH" ;;
  esac
done

if [ -L "$STATE_DIR" ] || { [ -e "$STATE_DIR" ] && [ ! -d "$STATE_DIR" ]; }; then
  die "Runtime state directory is not a safe directory"
fi
if [ "$CURRENT_UID" -eq 0 ]; then
  install -d -m 0700 -o "$RUNTIME_UID" -g "$RUNTIME_GID" "$STATE_DIR"
  GLOBAL_LOCK_PATH="/run/lock/agentserver-device-install.lock"
  [ -d /run/lock ] || GLOBAL_LOCK_PATH="$STATE_DIR/.global-install.lock"
  if [ -d /run/lock ]; then
    DEVICE_LOCK_PATH="/run/lock/agentserver-device-${RUNTIME_UID}-${DEVICE_ID}.lock"
  else
    DEVICE_LOCK_PATH="$STATE_DIR/.device-install.lock"
  fi
else
  install -d -m 0700 "$STATE_DIR"
  GLOBAL_LOCK_PATH="$STATE_DIR/.global-install.lock"
  DEVICE_LOCK_PATH="$STATE_DIR/.device-install.lock"
fi
[ ! -L "$GLOBAL_LOCK_PATH" ] || die "global installation lock path is unsafe"
[ ! -L "$DEVICE_LOCK_PATH" ] || die "device installation lock path is unsafe"
exec 8>"$GLOBAL_LOCK_PATH"
flock -n 8 || die "another AgentServer device installation is modifying system services"
exec 9>"$DEVICE_LOCK_PATH"
flock -n 9 || die "another installation for this device/user is running"

RUNTIME_REQUIREMENTS="$ROOT_DIR/requirements-runtime.lock"
if [ ! -f "$RUNTIME_REQUIREMENTS" ]; then RUNTIME_REQUIREMENTS="$ROOT_DIR/requirements.txt"; fi
runtime_sources_ready || die "run this script from an AgentServer checkout/release, or use the bootstrap installer"

[[ "$RUNTIME_VENV_DIR" == /* ]] || die "Runtime Python environment path must be absolute"
if [ -L "$RUNTIME_VENV_DIR" ] || { [ -e "$RUNTIME_VENV_DIR" ] && [ ! -d "$RUNTIME_VENV_DIR" ]; }; then
  die "Runtime Python environment path is not a safe directory"
fi
RUNTIME_VENV_PARENT="$(dirname -- "$RUNTIME_VENV_DIR")"
if [ -L "$RUNTIME_VENV_PARENT" ] || { [ -e "$RUNTIME_VENV_PARENT" ] && [ ! -d "$RUNTIME_VENV_PARENT" ]; }; then
  die "Runtime Python environment parent is not a safe directory"
fi
run_runtime mkdir -p -- "$RUNTIME_VENV_PARENT" || die "unable to create Runtime Python environment parent"
if [ "$(stat -c '%u' "$RUNTIME_VENV_PARENT" 2>/dev/null || true)" != "$RUNTIME_UID" ]; then
  die "Runtime Python environment parent must be owned by the Runtime user"
fi
runtime_python_ready() {
  [ -d "$RUNTIME_VENV_DIR" ] || return 1
  [ ! -L "$RUNTIME_VENV_DIR" ] || return 1
  [ -x "$RUNTIME_VENV_DIR/bin/python" ] || return 1
  # A real venv always has pyvenv.cfg.  Older test/release layouts may only
  # provide a managed interpreter path; preserve those while validating any
  # actual venv so a failed pip install cannot be mistaken for a good one.
  if [ ! -f "$RUNTIME_VENV_DIR/pyvenv.cfg" ]; then return 0; fi
  run_runtime "$RUNTIME_VENV_DIR/bin/python" -m pip check >/dev/null 2>&1
}

if ! runtime_python_ready; then
  echo "[1/5] Creating the Runtime Python environment"
  VENV_STAGE_DIR="$RUNTIME_VENV_DIR.install.$$"
  OLD_VENV_DIR="$RUNTIME_VENV_DIR.invalid.$$"
  [ ! -e "$VENV_STAGE_DIR" ] && [ ! -L "$VENV_STAGE_DIR" ] || die "temporary Runtime Python environment path is unsafe"
  [ ! -e "$OLD_VENV_DIR" ] && [ ! -L "$OLD_VENV_DIR" ] || die "old Runtime Python environment path is unsafe"
  run_runtime python3 -m venv "$VENV_STAGE_DIR" || die "unable to create Runtime Python environment"
  if [ -d "$ROOT_DIR/wheelhouse" ]; then
    run_runtime "$VENV_STAGE_DIR/bin/pip" install --no-index --find-links "$ROOT_DIR/wheelhouse" -r "$RUNTIME_REQUIREMENTS" \
      || die "unable to install Runtime Python dependencies"
  else
    run_runtime "$VENV_STAGE_DIR/bin/pip" install -r "$RUNTIME_REQUIREMENTS" \
      || die "unable to install Runtime Python dependencies"
  fi
  if [ -e "$RUNTIME_VENV_DIR" ] || [ -L "$RUNTIME_VENV_DIR" ]; then
    mv -- "$RUNTIME_VENV_DIR" "$OLD_VENV_DIR" || die "unable to stage the previous Runtime Python environment"
    VENV_RESTORE_PENDING=1
  fi
  if ! mv -- "$VENV_STAGE_DIR" "$RUNTIME_VENV_DIR"; then
    if [ -e "$OLD_VENV_DIR" ] || [ -L "$OLD_VENV_DIR" ]; then
      if mv -- "$OLD_VENV_DIR" "$RUNTIME_VENV_DIR"; then
        VENV_RESTORE_PENDING=0
      fi
    fi
    die "unable to activate the new Runtime Python environment"
  fi
  VENV_RESTORE_PENDING=0
  VENV_STAGE_DIR=""
  rm -rf -- "$OLD_VENV_DIR"
  OLD_VENV_DIR=""
fi
RUNTIME_PYTHON_BIN="$RUNTIME_VENV_DIR/bin/python"
[ -x "$RUNTIME_PYTHON_BIN" ] || die "Runtime Python environment is missing its interpreter"

runtime_common=(
  --device-id "$DEVICE_ID"
  --base-url "$BASE_URL"
  --state-dir "$STATE_DIR"
  --codex-binary "$CODEX_BIN"
  --node-binary "$NODE_BIN"
  --bubblewrap-binary "$BWRAP_BIN"
  --python-binary "$RUNTIME_PYTHON_BIN"
)

echo "[2/5] Checking Codex, Node.js, Python and bubblewrap as $RUNTIME_USER"
run_runtime bash "$RUNTIME_INSTALLER" "${runtime_common[@]}" --preflight-only

echo "[3/5] Enabling the persistent user service manager for $RUNTIME_USER"
command -v loginctl >/dev/null 2>&1 || die "loginctl is required for persistent user services"
command -v systemctl >/dev/null 2>&1 || die "systemctl is required for persistent user services"
run_root loginctl enable-linger "$RUNTIME_USER"
run_root systemctl start "user@$RUNTIME_UID.service"
run_runtime systemctl --user show-environment >/dev/null

if [ "$RUNTIME_ONLY" -ne 1 ]; then
  echo "[4/5] Installing OpenSSH and the FRP tunnel"
  frp_arguments=(
    --device-id "$DEVICE_ID"
    --remote-port "$REMOTE_PORT"
    --ssh-user "$SSH_USER"
    --server "$FRP_SERVER"
    --server-port "$FRP_SERVER_PORT"
    --version "$FRP_VERSION"
  )
  if [ -n "$FRP_TOKEN_FILE" ]; then
    frp_arguments+=(--token-file "$FRP_TOKEN_FILE")
  fi
  if [ "$FRP_ROTATE_TOKEN" -eq 1 ]; then
    frp_arguments+=(--rotate-token)
  fi
  if [ -n "$MERGE_CONFIG" ]; then
    frp_arguments+=(--merge-existing "$MERGE_CONFIG")
  fi
  run_root sh "$FRP_INSTALLER" "${frp_arguments[@]}"
else
  echo "[4/5] Keeping the existing OpenSSH/FRP installation"
fi

credential_path="$STATE_DIR/device.credential"
needs_enrollment=0
if [ ! -f "$credential_path" ] || [ "$REENROLL" -eq 1 ]; then needs_enrollment=1; fi
runtime_arguments=("${runtime_common[@]}" --require-systemd)
if [ "$needs_enrollment" -eq 1 ]; then
  TOKEN_STAGE_DIR="$(mktemp -d "$STATE_DIR/.enrollment.XXXXXX")"
  chmod 700 "$TOKEN_STAGE_DIR"
  staged_token="$TOKEN_STAGE_DIR/enrollment-token"
  if [ -n "$ENROLLMENT_TOKEN_FILE" ]; then
    enrollment_token="$(read_private_enrollment_file "$ENROLLMENT_TOKEN_FILE")"
  else
    [ -t 0 ] || die "non-interactive first enrollment requires --enrollment-token-file"
    read -r -s -p 'AgentServer enrollment token: ' enrollment_token
    printf '\n'
    [ -n "$enrollment_token" ] || die "enrollment token cannot be empty"
  fi
  printf '%s\n' "$enrollment_token" > "$staged_token"
  unset enrollment_token
  chmod 600 "$staged_token"
  if [ "$CURRENT_UID" -eq 0 ]; then
    chown "$RUNTIME_UID:$RUNTIME_GID" "$staged_token"
    chown "$RUNTIME_UID:$RUNTIME_GID" "$TOKEN_STAGE_DIR"
  fi
  runtime_arguments+=(--enrollment-token-file "$staged_token")
fi
if [ "$REENROLL" -eq 1 ]; then runtime_arguments+=(--reenroll); fi

echo "[5/5] Pairing and starting the outbound Runtime Host"
run_runtime bash "$RUNTIME_INSTALLER" "${runtime_arguments[@]}"
run_runtime systemctl --user is-active --quiet agentserver-runtime.service

echo
echo "AgentServer device installation completed"
echo "Device ID: $DEVICE_ID"
echo "SSH user: $SSH_USER"
echo "Runtime user: $RUNTIME_USER"
echo "Runtime service: agentserver-runtime.service"
if [ "$RUNTIME_ONLY" -ne 1 ]; then echo "FRP endpoint: $FRP_SERVER:$REMOTE_PORT"; fi
echo "Return to AgentServer and refresh the device; Runtime heartbeat may take a few seconds."
