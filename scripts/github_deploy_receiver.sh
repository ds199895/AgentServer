#!/usr/bin/env bash
set -euo pipefail

# Forced-command receiver for the dedicated GitHub Actions deploy key.
# The key cannot open a shell; it can only stream a release artifact for the
# exact commit SHA named in SSH_ORIGINAL_COMMAND.
umask 077
ulimit -f 262144

if [[ "${SSH_ORIGINAL_COMMAND:-}" =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
  EXPECTED_SHA="${BASH_REMATCH[1]}"
else
  echo "Only 'deploy <40-character commit SHA>' is allowed" >&2
  exit 2
fi

STREAM_FILE="$(mktemp /tmp/agentserver-github-stream.XXXXXX.tar.gz)"
INCOMING_DIR="$(mktemp -d /tmp/agentserver-github-release.XXXXXX)"
cleanup() {
  rm -f "$STREAM_FILE"
  rm -rf "$INCOMING_DIR"
}
trap cleanup EXIT

cat > "$STREAM_FILE"
mapfile -t entries < <(tar -tzf "$STREAM_FILE")
if [ "${#entries[@]}" -ne 3 ]; then
  echo "Unexpected deployment stream contents" >&2
  exit 2
fi

SHORT_SHA="${EXPECTED_SHA:0:12}"
ARTIFACT_NAME="agentserver-${SHORT_SHA}.tar.gz"
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

BUILD_SHA="$(tar -xOzf "$ARTIFACT" ./BUILD_SHA | tr -d '\r\n')"
if [ "$BUILD_SHA" != "$EXPECTED_SHA" ]; then
  echo "Artifact commit $BUILD_SHA does not match requested $EXPECTED_SHA" >&2
  exit 2
fi

DEPLOY_SCRIPT="$INCOMING_DIR/deploy_release.sh"
tar -xOzf "$ARTIFACT" ./scripts/deploy_release.sh > "$DEPLOY_SCRIPT"
chmod 0700 "$DEPLOY_SCRIPT"
DEPLOY_SMOKE_URL=https://agent.metakroma.com "$DEPLOY_SCRIPT" "$ARTIFACT"
