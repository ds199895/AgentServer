#!/usr/bin/env bash
set -euo pipefail

# Forced-command receiver for the dedicated GitHub Actions deploy key.
# Supported commands:
#   chunk <commit-sha> <index> <chunk-sha256>
#   finalize <commit-sha> <chunk-count> <archive-sha256>
umask 077
ulimit -f 524288

COMMAND="${SSH_ORIGINAL_COMMAND:-}"
SESSION_ROOT=/var/lib/agentserver/deploy-incoming
install -d -m 0700 "$SESSION_ROOT"

if [[ "$COMMAND" =~ ^chunk\ ([0-9a-f]{40})\ ([0-9]{6})\ ([0-9a-f]{64})$ ]]; then
  BUILD_SHA="${BASH_REMATCH[1]}"
  CHUNK_INDEX="${BASH_REMATCH[2]}"
  EXPECTED_CHUNK_SHA="${BASH_REMATCH[3]}"
  SESSION_DIR="$SESSION_ROOT/$BUILD_SHA"
  install -d -m 0700 "$SESSION_DIR"
  TEMP_CHUNK="$(mktemp "$SESSION_DIR/.chunk-${CHUNK_INDEX}.XXXXXX")"
  cleanup_chunk() { rm -f "$TEMP_CHUNK"; }
  trap cleanup_chunk EXIT
  cat > "$TEMP_CHUNK"
  ACTUAL_CHUNK_SHA="$(sha256sum "$TEMP_CHUNK" | awk '{print $1}')"
  if [ "$ACTUAL_CHUNK_SHA" != "$EXPECTED_CHUNK_SHA" ]; then
    echo "Chunk $CHUNK_INDEX checksum mismatch" >&2
    exit 2
  fi
  mv -f "$TEMP_CHUNK" "$SESSION_DIR/chunk-$CHUNK_INDEX"
  trap - EXIT
  exit 0
fi

if [[ "$COMMAND" =~ ^finalize\ ([0-9a-f]{40})\ ([0-9]{1,6})\ ([0-9a-f]{64})$ ]]; then
  BUILD_SHA="${BASH_REMATCH[1]}"
  CHUNK_COUNT=$((10#${BASH_REMATCH[2]}))
  EXPECTED_ARCHIVE_SHA="${BASH_REMATCH[3]}"
else
  echo "Only chunk/finalize deployment commands are allowed" >&2
  exit 2
fi

if [ "$CHUNK_COUNT" -lt 1 ] || [ "$CHUNK_COUNT" -gt 512 ]; then
  echo "Invalid chunk count" >&2
  exit 2
fi

SESSION_DIR="$SESSION_ROOT/$BUILD_SHA"
LOCK_FILE=/var/lib/agentserver/deploy.lock
exec 9>"$LOCK_FILE"
flock -x 9
if [ ! -d "$SESSION_DIR" ]; then
  echo "No upload session for $BUILD_SHA" >&2
  exit 2
fi

STREAM_FILE="$(mktemp /tmp/agentserver-github-stream.XXXXXX.tar.gz)"
INCOMING_DIR="$(mktemp -d /tmp/agentserver-github-release.XXXXXX)"
cleanup() {
  rm -f "$STREAM_FILE"
  rm -rf "$INCOMING_DIR" "$SESSION_DIR"
}
trap cleanup EXIT

for index in $(seq 0 $((CHUNK_COUNT - 1))); do
  printf -v chunk_name 'chunk-%06d' "$index"
  if [ ! -f "$SESSION_DIR/$chunk_name" ]; then
    echo "Missing $chunk_name" >&2
    exit 2
  fi
  cat "$SESSION_DIR/$chunk_name" >> "$STREAM_FILE"
done

ACTUAL_ARCHIVE_SHA="$(sha256sum "$STREAM_FILE" | awk '{print $1}')"
if [ "$ACTUAL_ARCHIVE_SHA" != "$EXPECTED_ARCHIVE_SHA" ]; then
  echo "Deployment archive checksum mismatch" >&2
  exit 2
fi

mapfile -t entries < <(tar -tzf "$STREAM_FILE")
SHORT_SHA="${BUILD_SHA:0:12}"
ARTIFACT_NAME="agentserver-${SHORT_SHA}.tar.gz"
if [ "${#entries[@]}" -ne 3 ]; then
  echo "Unexpected deployment stream contents" >&2
  exit 2
fi
for required in "./" "./$ARTIFACT_NAME" "./$ARTIFACT_NAME.sha256"; do
  found=0
  for entry in "${entries[@]}"; do
    if [ "$entry" = "$required" ]; then found=1; break; fi
  done
  if [ "$found" -ne 1 ]; then
    echo "Deployment stream is missing $required" >&2
    exit 2
  fi
done

tar --no-same-owner --no-same-permissions -xzf "$STREAM_FILE" -C "$INCOMING_DIR"
ARTIFACT="$INCOMING_DIR/$ARTIFACT_NAME"
(
  cd "$INCOMING_DIR"
  sha256sum -c "$ARTIFACT_NAME.sha256"
)

ARTIFACT_BUILD_SHA="$(tar -xOzf "$ARTIFACT" ./BUILD_SHA | tr -d '\r\n')"
if [ "$ARTIFACT_BUILD_SHA" != "$BUILD_SHA" ]; then
  echo "Artifact commit $ARTIFACT_BUILD_SHA does not match requested $BUILD_SHA" >&2
  exit 2
fi

DEPLOY_SCRIPT="$INCOMING_DIR/deploy_release.sh"
tar -xOzf "$ARTIFACT" ./scripts/deploy_release.sh > "$DEPLOY_SCRIPT"
chmod 0700 "$DEPLOY_SCRIPT"
DEPLOY_SMOKE_URL=https://agent.metakroma.com "$DEPLOY_SCRIPT" "$ARTIFACT"
