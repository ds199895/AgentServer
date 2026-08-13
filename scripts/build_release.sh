#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if ! git diff --quiet || ! git diff --cached --quiet || [ -n "$(git ls-files --others --exclude-standard)" ]; then
  echo "错误: 只能从干净的 Git 工作区构建发布制品" >&2
  exit 2
fi

BUILD_SHA="$(git rev-parse HEAD)"
if ! git cat-file -e "$BUILD_SHA^{commit}"; then
  echo "错误: 无法解析发布提交" >&2
  exit 2
fi

OUTPUT_DIR="${RELEASE_OUTPUT_DIR:-$ROOT_DIR/dist}"
mkdir -p "$OUTPUT_DIR"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
SOURCE_DIR="$TEMP_DIR/source"
mkdir -p "$SOURCE_DIR"

git archive "$BUILD_SHA" | COPYFILE_DISABLE=1 tar -x -C "$SOURCE_DIR"
printf '%s\n' "$BUILD_SHA" > "$SOURCE_DIR/BUILD_SHA"

npm --prefix "$SOURCE_DIR/frontend" ci
AGENTSERVER_BUILD_SHA="$BUILD_SHA" npm --prefix "$SOURCE_DIR/frontend" run build
rm -rf "$SOURCE_DIR/web_dist"
mv "$SOURCE_DIR/frontend/dist" "$SOURCE_DIR/web_dist"
printf '{"build_sha":"%s"}\n' "$BUILD_SHA" > "$SOURCE_DIR/web_dist/build.json"
rm -rf "$SOURCE_DIR/frontend/node_modules"

if [ "${BUNDLE_PYTHON_WHEELS:-0}" = "1" ]; then
  python3 -m pip download --disable-pip-version-check \
    --dest "$SOURCE_DIR/wheelhouse" \
    --requirement "$SOURCE_DIR/requirements.txt"
fi

ARTIFACT="$OUTPUT_DIR/agentserver-${BUILD_SHA:0:12}.tar.gz"
COPYFILE_DISABLE=1 tar -czf "$ARTIFACT" -C "$SOURCE_DIR" .
python3 - "$ARTIFACT" <<'PY'
import hashlib, pathlib, sys
artifact = pathlib.Path(sys.argv[1])
digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
artifact.with_suffix(artifact.suffix + ".sha256").write_text(
    f"{digest}  {artifact.name}\n", encoding="utf-8"
)
PY
printf '%s\n' "$ARTIFACT"
